"""Bounded process-local upload and TaskLog queues (no networking/DB imports).

API contract for integration
----------------------------
``submit_upload(fn, *args, **kwargs)`` and ``submit_task_log(...)`` return a
``concurrent.futures.Future``. They replace thread-per-upload/log, not the
business function: keep the existing upload/error logging and DB transaction in
``fn``. A full queue BLOCKS the submitting thread until space is available;
there is no best-effort/drop path, even for logs. Accepted jobs execute once
unless the caller explicitly cancels their Future. Job exceptions are stored
in the Future and logged without payloads; workers continue running. This is
an in-memory queue, not crash-durable storage or a retry mechanism.

Uploads: ``APP_UPLOAD_WORKERS`` defaults to 1, clamped to 1..2;
``APP_UPLOAD_QUEUE_SIZE`` defaults to 16, clamped to 1..256.
TaskLog: exactly one writer; ``APP_TASK_LOG_QUEUE_SIZE`` defaults to 64, clamped
to 1..1024. Queue sizes count waiting jobs, excluding the fixed worker count.
Capture immutable/small account data and open a fresh DB Session inside each
job, not a request Session or an expiring ORM object.

Call ``start_background_jobs`` optionally during startup (otherwise lazy).
Stop task producers before shutdown, then call ``shutdown_background_jobs``
from the app lifespan finalizer; it blocks and drains uploads BEFORE closing
the TaskLog queue because an upload may itself submit a final TaskLog. It also
drains on normal interpreter exit, but no in-memory design survives a hard
kill. ``drain_background_jobs`` is a non-closing flush; use it only after
producers are quiescent for a reliable flush boundary. No threads are created
merely by importing this module, and submission after shutdown is rejected
explicitly, not silently lost. ``start_background_jobs`` can start a new
lifecycle after a completed shutdown; plain submit never reopens a closed one.

Never synchronously submit back into the same queue from one of its workers:
with a full queue that would deadlock. Such reentry is rejected explicitly.
Uploads may submit TaskLogs, but TaskLog writers must not submit uploads (the
shutdown drain order is uploads -> logs). Neither queue consumes account
worker slots or task-control skip requests. There is deliberately no control
argument on critical write submission: a stop/skip cannot discard a write
which the account worker already decided to persist.
"""

from __future__ import annotations

import atexit
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
import logging
import os
import threading
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)
_Result = TypeVar("_Result")


def _env_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


@dataclass
class _Job:
    future: Future
    fn: Callable
    args: tuple
    kwargs: dict


class BoundedBackgroundQueue:
    """A fixed number of workers plus a strictly bounded waiting deque.

    ``submit`` applies blocking backpressure. ``shutdown(wait=False)`` closes
    admission immediately but workers still drain accepted jobs. A later
    ``shutdown(wait=True)`` joins them. ``drain(timeout)`` returns False if the
    timeout expires, never cancels anything; no timeout means a complete flush.
    Do not call drain/shutdown from this queue's own worker.
    """

    def __init__(self, *, name: str, workers: int, queue_size: int):
        if not 1 <= workers <= 20 or not 1 <= queue_size <= 1024:
            raise ValueError("workers must be 1..20 and queue_size must be 1..1024")
        self.name = name
        self.workers = workers
        self.queue_size = queue_size
        self._condition = threading.Condition()
        self._items: deque[_Job] = deque()
        self._threads: list[threading.Thread] = []
        self._local = threading.local()
        self._accepting = True
        self._shutdown = False
        self._unfinished = 0

    @property
    def queued_count(self) -> int:
        with self._condition:
            return len(self._items)

    @property
    def pending_count(self) -> int:
        with self._condition:
            return self._unfinished

    def _start_locked(self) -> None:
        for index in range(len(self._threads), self.workers):
            thread = threading.Thread(
                target=self._work,
                name=f"{self.name}-{index + 1}",
                daemon=True,
            )
            thread.start()
            # A failed Thread.start must not leave an unjoinable thread in the
            # pool or admit a job that no worker can execute.
            self._threads.append(thread)

    def start(self) -> None:
        with self._condition:
            if not self._accepting:
                raise RuntimeError(f"{self.name} queue is shut down")
            self._start_locked()

    def submit(self, fn: Callable[..., _Result], /, *args, **kwargs) -> Future[_Result]:
        if not callable(fn):
            raise TypeError("Background job must be callable")
        if getattr(self._local, "is_worker", False):
            raise RuntimeError(f"Cannot submit recursively to the {self.name} queue")
        future: Future[_Result] = Future()
        with self._condition:
            while self._accepting and len(self._items) >= self.queue_size:
                self._condition.wait()
            if not self._accepting:
                raise RuntimeError(f"{self.name} queue is shut down")
            self._start_locked()
            self._items.append(_Job(future, fn, args, kwargs))
            self._unfinished += 1
            self._condition.notify_all()
        return future

    def _work(self) -> None:
        self._local.is_worker = True
        while True:
            with self._condition:
                while not self._items and not self._shutdown:
                    self._condition.wait()
                if not self._items:
                    return
                job = self._items.popleft()
                # Release a blocked producer as soon as a waiting slot opens.
                self._condition.notify_all()
            try:
                if job.future.set_running_or_notify_cancel():
                    try:
                        result = job.fn(*job.args, **job.kwargs)
                    except BaseException as exc:
                        job.future.set_exception(exc)
                        logger.error(
                            "Background job failed in %s (%s)", self.name, type(exc).__name__
                        )
                    else:
                        try:
                            job.future.set_result(result)
                        finally:
                            del result
            except BaseException as exc:
                # A Future callback can raise BaseException too. A bad callback
                # must not permanently remove a writer from this fixed pool.
                logger.error(
                    "Background completion failed in %s (%s)",
                    self.name,
                    type(exc).__name__,
                )
            finally:
                # Do not keep account snapshots/credentials in an idle worker.
                del job
                with self._condition:
                    self._unfinished -= 1
                    self._condition.notify_all()

    def drain(self, timeout: float | None = None) -> bool:
        if getattr(self._local, "is_worker", False):
            raise RuntimeError(f"Cannot drain the {self.name} queue from its worker")
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        with self._condition:
            while self._unfinished:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def shutdown(self, wait: bool = True) -> None:
        if getattr(self._local, "is_worker", False):
            raise RuntimeError(f"Cannot shut down the {self.name} queue from its worker")
        with self._condition:
            self._accepting = False
            self._shutdown = True
            threads = tuple(self._threads)
            self._condition.notify_all()
        if wait:
            for thread in threads:
                thread.join()


class BackgroundJobs:
    """Two independent budgets; slow network uploads cannot starve DB logs."""

    def __init__(self):
        self.uploads = BoundedBackgroundQueue(
            name="upload",
            workers=_env_int("APP_UPLOAD_WORKERS", 1, 2),
            queue_size=_env_int("APP_UPLOAD_QUEUE_SIZE", 16, 256),
        )
        self.task_logs = BoundedBackgroundQueue(
            name="task-log",
            workers=1,
            queue_size=_env_int("APP_TASK_LOG_QUEUE_SIZE", 64, 1024),
        )
        self._shutdown_lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        with self._shutdown_lock:
            if self._closed:
                raise RuntimeError("Background jobs are shut down")
            self.uploads.start()
            self.task_logs.start()

    def drain(self) -> None:
        if any(
            getattr(queue._local, "is_worker", False)
            for queue in (self.uploads, self.task_logs)
        ):
            raise RuntimeError("Cannot drain background jobs from their workers")
        # Upload jobs are allowed to submit their final log as they complete.
        self.uploads.drain()
        self.task_logs.drain()

    def shutdown(self) -> None:
        if any(
            getattr(queue._local, "is_worker", False)
            for queue in (self.uploads, self.task_logs)
        ):
            raise RuntimeError("Cannot shut down background jobs from their workers")
        with self._shutdown_lock:
            # shutdown is idempotent; explicitly call both on every pass so a
            # previously interrupted shutdown can finish joining its workers.
            self.uploads.shutdown(wait=True)
            self.task_logs.shutdown(wait=True)
            self._closed = True


_background_jobs: BackgroundJobs | None = None
_background_lock = threading.Lock()


def get_background_jobs() -> BackgroundJobs:
    """Get the current process lifecycle; a closed one is not auto-reopened."""
    global _background_jobs
    with _background_lock:
        if _background_jobs is None:
            _background_jobs = BackgroundJobs()
        return _background_jobs


def start_background_jobs() -> None:
    """Optionally prestart workers, or explicitly begin a new app lifespan."""
    global _background_jobs
    with _background_lock:
        if _background_jobs is None or _background_jobs.closed:
            _background_jobs = BackgroundJobs()
        jobs = _background_jobs
    jobs.start()


def submit_upload(fn: Callable[..., _Result], /, *args, **kwargs) -> Future[_Result]:
    return get_background_jobs().uploads.submit(fn, *args, **kwargs)


def submit_task_log(fn: Callable[..., _Result], /, *args, **kwargs) -> Future[_Result]:
    return get_background_jobs().task_logs.submit(fn, *args, **kwargs)


def drain_background_jobs() -> None:
    # Flushing an unused service should not start it.
    with _background_lock:
        jobs = _background_jobs
    if jobs is not None:
        jobs.drain()


def shutdown_background_jobs() -> None:
    with _background_lock:
        jobs = _background_jobs
    if jobs is not None:
        jobs.shutdown()


# Best effort for normal interpreter exit. The application lifespan must call
# shutdown explicitly before disposing its DB engine or other job dependencies.
atexit.register(shutdown_background_jobs)


__all__ = [
    "BackgroundJobs",
    "BoundedBackgroundQueue",
    "drain_background_jobs",
    "get_background_jobs",
    "shutdown_background_jobs",
    "start_background_jobs",
    "submit_task_log",
    "submit_upload",
]
