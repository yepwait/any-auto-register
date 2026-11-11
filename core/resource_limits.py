"""Small, process-local resource limits shared by all account task sources.

Integration contract
--------------------
* Set ``APP_TASK_WORKERS`` before the first call to ``get_account_limiter``;
  its default is 1, and values are clamped to 1..20. ``task_worker_count`` also
  clamps a request and its item count so a request for 20 does not start 20
  threads merely to wait for a single global slot.
* Use ``with account_slot(control, attempt_id):`` ONLY inside a real account
  worker (registration / RT backfill / inspection), never around a parent task
  which waits for child tasks. Slot identity is NOT the shared task control.
  Same-thread nested account operations reuse their slot; another thread using
  that control must still acquire its own. This cannot make a parent waiting
  for a child safe: the parent must not acquire an account slot in the first
  place.
* The caller owns ``start_attempt`` / ``finish_attempt`` and result accounting.
  Slot acquisition checks the original ``checkpoint(attempt_id=...)`` before
  waiting, every 0.25 seconds, and immediately after acquiring. Stop and skip
  exceptions propagate unchanged; the semaphore is always released.
* ``bounded_as_completed(pool, fn, items, workers=C, control=control)`` submits
  one item per call to ``fn`` and yields completed Futures (not results). It
  holds at most C Futures by default, or 2C with ``max_pending=2*C``. Retain
  neither the input in a list nor yielded Futures if constant memory is needed.
  A stopped control prevents new submissions and cancels not-yet-running work;
  already-running work is drained so the caller can count its results. The
  orchestration checkpoint never consumes skip. Closing the generator cancels
  queued work, but cannot interrupt running Python functions.
* ``rolling_map`` owns a request-sized executor and yields results in completion
  order. It does NOT implicitly acquire account slots or create attempts. Use
  ``contextlib.closing`` when breaking out early so cleanup is deterministic.
* ``get_top_level_limiter().acquire(task_id)`` returns a lease, or raises
  ``ActiveTaskLimitReached`` immediately. This is opt-in admission only: no
  existing API is changed. Acquire before accepting a top-level task and release
  in its finalizer (or if scheduling fails); do not acquire again for children.

These limits are per Python process, not distributed across multiple servers
or uvicorn workers. Environment changes require a process restart after lazy
initialization. Importing this module starts no threads and opens no resources.
"""

from __future__ import annotations

from concurrent.futures import Executor, FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
import os
import threading
from typing import Any, Callable, Iterable, Iterator, Protocol, TypeVar

from core.task_runtime import StopTaskRequested

DEFAULT_TASK_WORKERS = 1
MAX_TASK_WORKERS = 20
DEFAULT_MAX_ACTIVE_TASKS = 4
MAX_ACTIVE_TASKS = 64
CONTROL_POLL_SECONDS = 0.25

_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


class TaskControl(Protocol):
    def checkpoint(
        self, *, consume_skip: bool = True, attempt_id: int | None = None
    ) -> None: ...


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


class AccountLimiter:
    """A process-shared semaphore; reentry is local to the worker thread only."""

    def __init__(self, workers: int = DEFAULT_TASK_WORKERS):
        self.workers = _bounded_int(workers, DEFAULT_TASK_WORKERS, 1, MAX_TASK_WORKERS)
        self._semaphore = threading.BoundedSemaphore(self.workers)
        self._local = threading.local()

    @contextmanager
    def slot(
        self,
        control: TaskControl | None = None,
        attempt_id: int | None = None,
    ) -> Iterator[None]:
        def checkpoint() -> None:
            if control is not None:
                control.checkpoint(attempt_id=attempt_id)

        checkpoint()
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                checkpoint()
                yield
            finally:
                self._local.depth = depth
            return

        acquired = False
        try:
            while not acquired:
                checkpoint()
                acquired = self._semaphore.acquire(timeout=CONTROL_POLL_SECONDS)
            # A stop/skip may have arrived while acquire returned successfully.
            checkpoint()
            self._local.depth = 1
            yield
        finally:
            if acquired:
                self._local.depth = 0
                self._semaphore.release()


_account_limiter: AccountLimiter | None = None
_account_limiter_lock = threading.Lock()


def get_account_limiter() -> AccountLimiter:
    global _account_limiter
    with _account_limiter_lock:
        if _account_limiter is None:
            _account_limiter = AccountLimiter(os.environ.get("APP_TASK_WORKERS", "1"))
        return _account_limiter


def task_worker_count(requested: Any = None, total: Any = None) -> int:
    """Return a safe executor size, including 1 for an empty request.

    ``None`` requests the process cap; invalid explicit values use 1. The
    caller can skip constructing an executor when its total is zero.
    """
    cap = get_account_limiter().workers
    workers = cap if requested is None else _bounded_int(requested, 1, 1, cap)
    if total is not None:
        workers = min(workers, _bounded_int(total, 1, 1, MAX_TASK_WORKERS))
    return workers


def account_slot(
    control: TaskControl | None = None, attempt_id: int | None = None
):
    """Return the shared account-worker context manager; see module contract."""
    return get_account_limiter().slot(control, attempt_id)


def _orchestration_stopped(control: TaskControl | None) -> bool:
    if control is None:
        return False
    try:
        control.checkpoint(consume_skip=False)
    except StopTaskRequested:
        return True
    return False


def bounded_as_completed(
    executor: Executor,
    fn: Callable[[_Item], _Result],
    items: Iterable[_Item],
    *,
    workers: int,
    max_pending: int | None = None,
    control: TaskControl | None = None,
) -> Iterator[Future[_Result]]:
    """Lazily submit a C..2C window and yield completed Futures.

    ``workers`` must describe the executor's actual size; it is NOT silently
    lowered to the global cap here because the executor already exists. Use
    ``task_worker_count`` to size it. Results/exceptions stay in their Future,
    just like ``concurrent.futures.as_completed``. Input/submission errors
    propagate and cancel outstanding queued work. A generator ``close`` also
    cancels pending work; executor ownership remains with the caller.
    """
    worker_count = _bounded_int(workers, 1, 1, MAX_TASK_WORKERS)
    window = worker_count if max_pending is None else _bounded_int(
        max_pending, worker_count, 1, 2 * worker_count
    )
    source = iter(items)
    pending: set[Future[_Result]] = set()
    exhausted = False
    stopped = False
    try:
        while pending or not exhausted:
            stopped = stopped or _orchestration_stopped(control)
            if stopped:
                exhausted = True
                for future in pending:
                    future.cancel()
            while not exhausted and len(pending) < window:
                if _orchestration_stopped(control):
                    stopped = True
                    exhausted = True
                    for future in pending:
                        future.cancel()
                    break
                try:
                    item = next(source)
                except StopIteration:
                    exhausted = True
                    break
                pending.add(executor.submit(fn, item))
                # Do not retain an extra input payload while waiting/yielding.
                del item
            if not pending:
                break
            # A cancelled Future can be CANCELLED rather than
            # CANCELLED_AND_NOTIFIED until its executor dequeues it. Checking
            # done() avoids waiting on those cancellation states unnecessarily.
            ready = {future for future in pending if future.done()}
            if not ready:
                ready = wait(
                    pending, timeout=CONTROL_POLL_SECONDS, return_when=FIRST_COMPLETED
                ).done
            if ready:
                completed = ready.pop()
                ready.clear()
                pending.remove(completed)
                yield completed
                del completed
                # Refill immediately after each consumed completion rather
                # than leaving workers idle until a whole done batch is read.
    finally:
        for future in pending:
            future.cancel()
        pending.clear()


def rolling_map(
    fn: Callable[[_Item], _Result],
    items: Iterable[_Item],
    *,
    workers: Any = None,
    max_pending: int | None = None,
    control: TaskControl | None = None,
) -> Iterator[_Result]:
    """Yield results in completion order using only a bounded request executor.

    Task functions own slot acquisition, attempt lifetime and interruption
    conversion. A Future cancelled by stop is ignored; running task exceptions
    (including stop/skip) propagate from ``result()`` without modification.
    """
    count = task_worker_count(workers)
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="account") as pool:
        completions = bounded_as_completed(
            pool, fn, items, workers=count, max_pending=max_pending, control=control
        )
        try:
            for future in completions:
                if not future.cancelled():
                    yield future.result()
        finally:
            completions.close()


class ActiveTaskLimitReached(RuntimeError):
    """Opt-in admission rejected a new top-level task; typically map to HTTP 429."""


class TaskAdmission:
    """Idempotently releasable admission lease (also a context manager)."""

    def __init__(self, limiter: TopLevelTaskLimiter, task_id: str):
        self._limiter = limiter
        self.task_id = task_id
        self._released = False

    def release(self) -> None:
        # The limiter lock also makes concurrent release of this lease safe.
        with self._limiter._lock:
            if not self._released:
                self._limiter._active.discard(self.task_id)
                self._released = True

    def __enter__(self) -> TaskAdmission:
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.release()


class TopLevelTaskLimiter:
    """Immediate admission/rejection, independent from account-worker slots."""

    def __init__(self, limit: int = DEFAULT_MAX_ACTIVE_TASKS):
        self.limit = _bounded_int(limit, DEFAULT_MAX_ACTIVE_TASKS, 1, MAX_ACTIVE_TASKS)
        self._lock = threading.Lock()
        self._active: set[str] = set()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def acquire(self, task_id: str) -> TaskAdmission:
        if not task_id:
            raise ValueError("A non-empty top-level task id is required")
        with self._lock:
            if task_id in self._active:
                raise ValueError(f"Task already admitted: {task_id}")
            if len(self._active) >= self.limit:
                raise ActiveTaskLimitReached(
                    f"Active top-level task limit reached ({self.limit})"
                )
            self._active.add(task_id)
        return TaskAdmission(self, task_id)


_top_level_limiter: TopLevelTaskLimiter | None = None
_top_level_limiter_lock = threading.Lock()


def get_top_level_limiter() -> TopLevelTaskLimiter:
    global _top_level_limiter
    with _top_level_limiter_lock:
        if _top_level_limiter is None:
            _top_level_limiter = TopLevelTaskLimiter(
                os.environ.get("APP_MAX_ACTIVE_TASKS", str(DEFAULT_MAX_ACTIVE_TASKS))
            )
        return _top_level_limiter


__all__ = [
    "AccountLimiter",
    "ActiveTaskLimitReached",
    "TaskAdmission",
    "TopLevelTaskLimiter",
    "account_slot",
    "bounded_as_completed",
    "get_account_limiter",
    "get_top_level_limiter",
    "rolling_map",
    "task_worker_count",
]
