"""Resource-limit tests: local callables/mocks only; no network or real writes."""

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
import os
import threading
import unittest
from unittest import mock

import core.resource_limits as limits
from core.task_runtime import (
    RegisterTaskControl,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)
import services.background_jobs as background


class _ImmediateExecutor:
    """A deterministic executor that also measures retained in-flight results."""

    def __init__(self):
        self.submitted = []
        self.futures = []

    def submit(self, fn, item):
        self.submitted.append(item)
        future = Future()
        self.futures.append(future)
        try:
            future.set_result(fn(item))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class WorkerCountTests(unittest.TestCase):
    def setUp(self):
        self.singleton = mock.patch.object(limits, "_account_limiter", None)
        self.singleton.start()
        self.addCleanup(self.singleton.stop)

    def test_default_cap_is_one_and_env_is_frozen_after_first_use(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("APP_TASK_WORKERS", None)
            self.assertEqual(limits.task_worker_count(20, 1000), 1)
            os.environ["APP_TASK_WORKERS"] = "8"
            self.assertEqual(limits.task_worker_count(20), 1)

    def test_request_and_total_are_capped_before_starting_threads(self):
        with mock.patch.dict(os.environ, {"APP_TASK_WORKERS": "4"}):
            self.assertEqual(limits.task_worker_count(), 4)
            self.assertEqual(limits.task_worker_count(20, 1000), 4)
            self.assertEqual(limits.task_worker_count(2, 1000), 2)
            self.assertEqual(limits.task_worker_count(20, 2), 2)
            for invalid in (0, -1, "oops", "", 1.5):
                self.assertEqual(limits.task_worker_count(invalid), 1)
            self.assertEqual(limits.task_worker_count(20, 0), 1)

    def test_environment_values_have_safe_bounds_and_invalid_fallback(self):
        for value, expected in (("100000", 20), ("0", 1), ("-7", 1), ("bad", 1)):
            with self.subTest(value=value):
                with mock.patch.object(limits, "_account_limiter", None):
                    with mock.patch.dict(os.environ, {"APP_TASK_WORKERS": value}):
                        self.assertEqual(limits.task_worker_count(100000), expected)

    def test_all_callers_get_one_process_limiter(self):
        with mock.patch.dict(os.environ, {"APP_TASK_WORKERS": "2"}):
            with ThreadPoolExecutor(max_workers=4) as pool:
                instances = list(pool.map(lambda _: limits.get_account_limiter(), range(20)))
            self.assertTrue(all(instance is instances[0] for instance in instances))


class AccountSlotTests(unittest.TestCase):
    def test_release_on_business_failure_and_same_thread_nested_reentry(self):
        limiter = limits.AccountLimiter(1)
        semaphore = mock.Mock(wraps=threading.BoundedSemaphore(1))
        limiter._semaphore = semaphore
        control = RegisterTaskControl()
        with self.assertRaisesRegex(ValueError, "failure"):
            with limiter.slot(control):
                with limiter.slot(control):
                    raise ValueError("failure")
        self.assertEqual(semaphore.acquire.call_count, 1)
        self.assertEqual(semaphore.release.call_count, 1)
        with limiter.slot(control):
            pass
        self.assertEqual(semaphore.release.call_count, 2)

    def test_same_control_does_not_bypass_global_slot_from_another_thread(self):
        limiter = limits.AccountLimiter(1)
        control = RegisterTaskControl()
        waiting = threading.Event()
        entered = threading.Event()
        real_acquire = limiter._semaphore.acquire

        def acquire(*args, **kwargs):
            waiting.set()
            return real_acquire(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with limiter.slot(control):
                with mock.patch.object(limiter._semaphore, "acquire", side_effect=acquire):
                    def child():
                        with limiter.slot(control):
                            entered.set()
                    future = pool.submit(child)
                    self.assertTrue(waiting.wait(1))
                    self.assertFalse(entered.is_set())
            # Release before joining child. The parent orchestration in
            # production must never hold a slot while waiting for it.
            future.result(timeout=1)
        self.assertTrue(entered.is_set())

    def test_stop_before_acquire_does_not_touch_semaphore(self):
        limiter = limits.AccountLimiter(1)
        limiter._semaphore = mock.Mock()
        control = RegisterTaskControl()
        control.request_stop()
        with self.assertRaises(StopTaskRequested):
            with limiter.slot(control):
                self.fail("stopped task must not enter")
        limiter._semaphore.acquire.assert_not_called()
        limiter._semaphore.release.assert_not_called()

    def test_stop_and_targeted_skip_interrupt_wait_with_exact_exception(self):
        for exception_type in (StopTaskRequested, SkipCurrentAttemptRequested):
            with self.subTest(exception_type=exception_type):
                limiter = limits.AccountLimiter(1)
                control = RegisterTaskControl()
                attempt_id = control.start_attempt()
                interrupt = exception_type("original")
                checks = [None, None, interrupt]
                control.checkpoint = mock.Mock(side_effect=checks)
                limiter._semaphore = mock.Mock()
                limiter._semaphore.acquire.return_value = False
                with self.assertRaises(exception_type) as caught:
                    with limiter.slot(control, attempt_id):
                        self.fail("waiting worker must not enter")
                self.assertIs(caught.exception, interrupt)
                limiter._semaphore.acquire.assert_called_once_with(timeout=0.25)
                limiter._semaphore.release.assert_not_called()
                for checkpoint in control.checkpoint.call_args_list:
                    self.assertEqual(checkpoint.kwargs, {"attempt_id": attempt_id})

    def test_interrupt_racing_successful_acquire_releases_slot(self):
        for exception_type in (StopTaskRequested, SkipCurrentAttemptRequested):
            with self.subTest(exception_type=exception_type):
                limiter = limits.AccountLimiter(1)
                limiter._semaphore = mock.Mock()
                limiter._semaphore.acquire.return_value = True
                control = mock.Mock()
                control.checkpoint.side_effect = [None, None, exception_type()]
                with self.assertRaises(exception_type):
                    with limiter.slot(control, 7):
                        self.fail("post-acquire checkpoint must run")
                limiter._semaphore.release.assert_called_once_with()

    def test_real_control_targeted_skip_is_consumed_once_while_waiting(self):
        limiter = limits.AccountLimiter(1)
        control = RegisterTaskControl()
        attempt = control.start_attempt()
        def busy_acquire(*, timeout):
            self.assertEqual(timeout, 0.25)
            control.request_skip_current()
            return False
        with mock.patch.object(limiter._semaphore, "acquire", side_effect=busy_acquire):
            with self.assertRaises(SkipCurrentAttemptRequested):
                with limiter.slot(control, attempt):
                    pass
        control.checkpoint(attempt_id=attempt)
        control.finish_attempt(attempt)
        with limiter.slot(control):
            pass

    def test_shared_wrapper_and_parent_orchestration_do_not_consume_slots(self):
        limiter = limits.AccountLimiter(1)
        control = RegisterTaskControl()
        with mock.patch.object(limits, "get_account_limiter", return_value=limiter):
            with ThreadPoolExecutor(max_workers=1) as pool:
                def child(value):
                    with limits.account_slot(control):
                        return value
                # The parent only submits/waits; a worker sharing its control
                # acquires normally with the process cap of one.
                result = [f.result() for f in limits.bounded_as_completed(
                    pool, child, range(3), workers=1, control=control
                )]
        self.assertEqual(result, [0, 1, 2])

    def test_registration_backfill_and_inspection_share_one_concurrency_budget(self):
        limiter = limits.AccountLimiter(2)
        lock = threading.Lock()
        first_two = threading.Event()
        release = threading.Event()
        active = 0
        peak = 0
        def account_work(source):
            nonlocal active, peak
            with limiter.slot(RegisterTaskControl()):
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active == 2:
                        first_two.set()
                try:
                    self.assertTrue(release.wait(2))
                    return source
                finally:
                    with lock:
                        active -= 1
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(account_work, source) for source in
                       ("registration", "backfill", "inspection") * 2]
            try:
                self.assertTrue(first_two.wait(1))
                self.assertEqual(peak, 2)
            finally:
                release.set()
            self.assertEqual(len([future.result(timeout=2) for future in futures]), 6)
        self.assertEqual(peak, 2)


class RollingSubmissionTests(unittest.TestCase):
    def test_large_input_is_lazy_and_retains_only_worker_window(self):
        consumed = []
        def source():
            for item in range(10000):
                consumed.append(item)
                yield item
        executor = _ImmediateExecutor()
        with closing(limits.bounded_as_completed(executor, lambda x: x, source(), workers=3)) as results:
            next(results)
            self.assertEqual(len(consumed), 3)
            next(results)
            self.assertEqual(len(consumed), 4)
            next(results)
            self.assertEqual(len(consumed), 5)
            next(results)
            self.assertEqual(len(consumed), 6)
        self.assertEqual(len(executor.submitted), 6)

    def test_pending_window_is_never_larger_than_twice_workers(self):
        for requested in (4, 100000):
            with self.subTest(requested=requested):
                executor = _ImmediateExecutor()
                with closing(limits.bounded_as_completed(
                    executor, lambda x: x, range(10000), workers=2, max_pending=requested
                )) as results:
                    next(results)
                    self.assertEqual(len(executor.submitted), 4)

    def test_all_results_and_exceptions_are_preserved_in_futures(self):
        def worker(item):
            if item == 2:
                raise ValueError("failed-item")
            return item * 2
        executor = _ImmediateExecutor()
        futures = list(limits.bounded_as_completed(executor, worker, range(5), workers=2))
        values, errors = [], []
        for future in futures:
            try:
                values.append(future.result())
            except ValueError as exc:
                errors.append(str(exc))
        self.assertCountEqual(values, [0, 2, 6, 8])
        self.assertEqual(errors, ["failed-item"])

    def test_orchestration_does_not_consume_pending_skip(self):
        control = RegisterTaskControl()
        control.request_skip_current()
        executor = _ImmediateExecutor()
        results = list(limits.bounded_as_completed(
            executor, lambda x: x, range(3), workers=1, control=control
        ))
        self.assertEqual([future.result() for future in results], [0, 1, 2])
        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint()

    def test_stopped_before_submission_does_not_consume_input(self):
        control = RegisterTaskControl()
        control.request_stop()
        source = mock.Mock()
        source.__iter__ = mock.Mock(return_value=iter([1, 2, 3]))
        executor = _ImmediateExecutor()
        self.assertEqual(list(limits.bounded_as_completed(
            executor, lambda x: x, source, workers=1, control=control
        )), [])
        self.assertEqual(executor.submitted, [])

    def test_stop_cancels_queued_and_drains_running_without_new_submissions(self):
        control = RegisterTaskControl()
        executor = mock.Mock()
        queued = Future()
        running = Future()
        running.set_running_or_notify_cancel()
        def submit(_fn, item):
            if item == 0:
                return queued
            control.request_stop()
            running.set_result("running-result")
            return running
        executor.submit.side_effect = submit
        results = list(limits.bounded_as_completed(
            executor, lambda x: x, range(10000), workers=2, control=control
        ))
        self.assertEqual(executor.submit.call_count, 2)
        self.assertTrue(queued.cancelled())
        self.assertCountEqual(results, [queued, running])
        self.assertEqual(running.result(), "running-result")

    def test_close_cancels_other_queued_futures(self):
        completed, queued = Future(), Future()
        completed.set_result(1)
        executor = mock.Mock()
        executor.submit.side_effect = [completed, queued]
        results = limits.bounded_as_completed(executor, lambda x: x, range(10), workers=2)
        self.assertIs(next(results), completed)
        results.close()
        self.assertTrue(queued.cancelled())

    def test_source_failure_cancels_submitted_work(self):
        queued = Future()
        executor = mock.Mock()
        executor.submit.return_value = queued
        def source():
            yield 1
            raise ValueError("source-error")
        with self.assertRaisesRegex(ValueError, "source-error"):
            list(limits.bounded_as_completed(executor, lambda x: x, source(), workers=2))
        self.assertTrue(queued.cancelled())

    def test_rolling_map_sizes_executor_and_propagates_original_interruptions(self):
        executor = _ImmediateExecutor()
        manager = mock.MagicMock()
        manager.__enter__.return_value = executor
        with mock.patch.object(limits, "task_worker_count", return_value=1):
            with mock.patch.object(limits, "ThreadPoolExecutor", return_value=manager) as pool:
                self.assertEqual(list(limits.rolling_map(lambda x: x + 1, range(3), workers=20)), [1, 2, 3])
        pool.assert_called_once_with(max_workers=1, thread_name_prefix="account")
        manager.__exit__.assert_called_once()
        interrupted = StopTaskRequested("exact")
        def worker(_item):
            raise interrupted
        manager = mock.MagicMock()
        manager.__enter__.return_value = _ImmediateExecutor()
        with mock.patch.object(limits, "ThreadPoolExecutor", return_value=manager):
            with self.assertRaises(StopTaskRequested) as caught:
                list(limits.rolling_map(worker, [1]))
        self.assertIs(caught.exception, interrupted)


class AdmissionTests(unittest.TestCase):
    def test_default_four_rejects_fifth_and_lease_release_is_idempotent(self):
        limiter = limits.TopLevelTaskLimiter()
        leases = [limiter.acquire(str(index)) for index in range(4)]
        with self.assertRaises(limits.ActiveTaskLimitReached):
            limiter.acquire("fifth")
        leases[0].release()
        leases[0].release()
        self.assertEqual(limiter.active_count, 3)
        with limiter.acquire("fifth"):
            self.assertEqual(limiter.active_count, 4)
        for lease in leases:
            lease.release()
        self.assertEqual(limiter.active_count, 0)

    def test_duplicate_ids_are_not_counted_twice_and_exception_releases(self):
        limiter = limits.TopLevelTaskLimiter(2)
        with self.assertRaisesRegex(ValueError, "body"):
            with limiter.acquire("a"):
                with self.assertRaises(ValueError):
                    limiter.acquire("a")
                raise ValueError("body")
        self.assertEqual(limiter.active_count, 0)
        with self.assertRaises(ValueError):
            limiter.acquire("")

    def test_process_admission_environment_has_safe_bounds(self):
        for value, expected in (("bad", 4), ("-10", 1), ("99999", 64)):
            with mock.patch.object(limits, "_top_level_limiter", None):
                with mock.patch.dict(os.environ, {"APP_MAX_ACTIVE_TASKS": value}):
                    self.assertEqual(limits.get_top_level_limiter().limit, expected)

    def test_admission_is_atomic_under_competing_callers(self):
        limiter = limits.TopLevelTaskLimiter(4)
        def acquire(index):
            try:
                return limiter.acquire(str(index))
            except limits.ActiveTaskLimitReached:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            leases = list(pool.map(acquire, range(32)))
        self.assertEqual(sum(lease is not None for lease in leases), 4)
        for lease in leases:
            if lease is not None:
                lease.release()


class BackgroundQueueTests(unittest.TestCase):
    def make_queue(self, **kwargs):
        queue = background.BoundedBackgroundQueue(name="test-write", **kwargs)
        self.addCleanup(queue.shutdown)
        return queue

    def test_full_queue_backpressures_and_preserves_every_write(self):
        queue = self.make_queue(workers=1, queue_size=1)
        started, release, submitting, submitted = (threading.Event() for _ in range(4))
        writes = []
        def first():
            started.set()
            self.assertTrue(release.wait(2))
            writes.append(1)
        first_future = queue.submit(first)
        self.assertTrue(started.wait(1))
        second = queue.submit(writes.append, 2)
        holder = []
        def producer():
            submitting.set()
            holder.append(queue.submit(writes.append, 3))
            submitted.set()
        thread = threading.Thread(target=producer)
        thread.start()
        try:
            self.assertTrue(submitting.wait(1))
            self.assertFalse(submitted.wait(0.05))
            self.assertEqual(queue.queued_count, 1)
            self.assertEqual(queue.pending_count, 2)
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        first_future.result(timeout=1)
        second.result(timeout=1)
        holder[0].result(timeout=1)
        self.assertTrue(queue.drain(timeout=1))
        self.assertEqual(writes, [1, 2, 3])

    def test_fixed_worker_count_and_all_results_survive(self):
        queue = self.make_queue(workers=2, queue_size=3)
        work = mock.Mock(side_effect=lambda value: value * 3)
        futures = [queue.submit(work, value) for value in range(100)]
        self.assertEqual([future.result(timeout=2) for future in futures], [value * 3 for value in range(100)])
        self.assertEqual(work.call_count, 100)
        self.assertEqual(len(queue._threads), 2)
        self.assertTrue(queue.drain(timeout=1))
        self.assertEqual(queue.pending_count, 0)

    def test_failure_is_visible_redacted_and_does_not_kill_writer(self):
        queue = self.make_queue(workers=1, queue_size=2)
        secret_failure = ValueError("secret-token-must-not-be-logged")
        with mock.patch.object(background.logger, "error") as log_error:
            failed = queue.submit(mock.Mock(side_effect=secret_failure))
            with self.assertRaises(ValueError) as caught:
                failed.result(timeout=1)
            self.assertIs(caught.exception, secret_failure)
            self.assertEqual(queue.submit(lambda: "next-job").result(timeout=1), "next-job")
            queue.drain()
            self.assertNotIn("secret-token", str(log_error.call_args_list))
            self.assertTrue(log_error.called)

    def test_shutdown_unblocks_waiting_producer_but_drains_accepted_jobs(self):
        queue = self.make_queue(workers=1, queue_size=1)
        started, release, attempting = (threading.Event() for _ in range(3))
        def first():
            started.set()
            self.assertTrue(release.wait(2))
        queue.submit(first)
        self.assertTrue(started.wait(1))
        accepted = mock.Mock(return_value=None)
        queued = queue.submit(accepted)
        rejected = mock.Mock()
        errors = []
        def producer():
            attempting.set()
            try:
                queue.submit(rejected)
            except RuntimeError as exc:
                errors.append(exc)
        thread = threading.Thread(target=producer)
        thread.start()
        self.assertTrue(attempting.wait(1))
        try:
            queue.shutdown(wait=False)
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertFalse(queue.drain(timeout=0))
        finally:
            release.set()
            queue.shutdown()
        self.assertIsNone(queued.result(timeout=1))
        accepted.assert_called_once_with()
        rejected.assert_not_called()
        with self.assertRaises(RuntimeError):
            queue.submit(rejected)

    def test_cancelled_job_is_not_run_and_drain_still_finishes(self):
        queue = self.make_queue(workers=1, queue_size=1)
        started, release = threading.Event(), threading.Event()
        def first():
            started.set()
            self.assertTrue(release.wait(2))
        queue.submit(first)
        self.assertTrue(started.wait(1))
        fn = mock.Mock()
        cancelled = queue.submit(fn)
        self.assertTrue(cancelled.cancel())
        release.set()
        self.assertTrue(queue.drain(timeout=1))
        fn.assert_not_called()

    def test_same_queue_reentry_and_worker_shutdown_are_rejected_not_deadlocked(self):
        queue = self.make_queue(workers=1, queue_size=1)
        with mock.patch.object(background.logger, "error"):
            recursive = queue.submit(lambda: queue.submit(lambda: None))
            with self.assertRaisesRegex(RuntimeError, "recursively"):
                recursive.result(timeout=1)
            shutdown = queue.submit(queue.shutdown)
            with self.assertRaisesRegex(RuntimeError, "from its worker"):
                shutdown.result(timeout=1)
            drain = queue.submit(queue.drain)
            with self.assertRaisesRegex(RuntimeError, "from its worker"):
                drain.result(timeout=1)
        self.assertEqual(queue.submit(lambda: "still alive").result(timeout=1), "still alive")

    def test_failing_future_callback_does_not_remove_worker(self):
        queue = self.make_queue(workers=1, queue_size=1)
        entered, release = threading.Event(), threading.Event()
        def job():
            entered.set()
            self.assertTrue(release.wait(2))
            return "first"
        future = queue.submit(job)
        self.assertTrue(entered.wait(1))
        def callback(_future):
            raise KeyboardInterrupt("bad-callback")
        future.add_done_callback(callback)
        with mock.patch.object(background.logger, "error"):
            release.set()
            self.assertTrue(queue.drain(timeout=1))
        self.assertEqual(future.result(timeout=1), "first")
        self.assertEqual(queue.submit(lambda: "next").result(timeout=1), "next")

    def test_import_and_construction_do_not_start_threads(self):
        with mock.patch.object(background.threading, "Thread") as thread:
            queue = self.make_queue(workers=1, queue_size=1)
            self.assertEqual(queue.pending_count, 0)
            thread.assert_not_called()

    def test_thread_start_failure_rejects_unaccepted_job_and_allows_retry(self):
        queue = self.make_queue(workers=1, queue_size=1)
        failed_thread = mock.Mock()
        failed_thread.start.side_effect = RuntimeError("cannot-start-thread")
        job = mock.Mock(return_value="written")
        with mock.patch.object(background.threading, "Thread", return_value=failed_thread):
            with self.assertRaisesRegex(RuntimeError, "cannot-start-thread"):
                queue.submit(job)
        self.assertEqual(queue.pending_count, 0)
        self.assertEqual(queue.queued_count, 0)
        self.assertEqual(queue._threads, [])
        job.assert_not_called()
        self.assertEqual(queue.submit(job).result(timeout=1), "written")

    def test_invalid_queue_sizes_are_rejected(self):
        for workers, size in ((0, 1), (21, 1), (1, 0), (1, 1025)):
            with self.assertRaises(ValueError):
                background.BoundedBackgroundQueue(name="bad", workers=workers, queue_size=size)


class BackgroundLifecycleTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(background, "_background_jobs", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(background.shutdown_background_jobs)

    def test_upload_and_logs_have_separate_safe_environment_budgets(self):
        with mock.patch.dict(os.environ, {
            "APP_UPLOAD_WORKERS": "1000", "APP_UPLOAD_QUEUE_SIZE": "100000",
            "APP_TASK_LOG_QUEUE_SIZE": "0",
        }):
            jobs = background.get_background_jobs()
        self.assertEqual(jobs.uploads.workers, 2)
        self.assertEqual(jobs.uploads.queue_size, 256)
        self.assertEqual(jobs.task_logs.workers, 1)
        self.assertEqual(jobs.task_logs.queue_size, 1)

    def test_invalid_environment_uses_defaults(self):
        with mock.patch.dict(os.environ, {
            "APP_UPLOAD_WORKERS": "bad", "APP_UPLOAD_QUEUE_SIZE": "bad",
            "APP_TASK_LOG_QUEUE_SIZE": "bad",
        }):
            jobs = background.get_background_jobs()
        self.assertEqual(jobs.uploads.workers, 1)
        self.assertEqual(jobs.uploads.queue_size, 16)
        self.assertEqual(jobs.task_logs.queue_size, 64)

    def test_slow_upload_does_not_starve_task_log_writer(self):
        entered, release = threading.Event(), threading.Event()
        def upload():
            entered.set()
            self.assertTrue(release.wait(2))
        future = background.submit_upload(upload)
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(background.submit_task_log(lambda: "persisted").result(timeout=1), "persisted")
        finally:
            release.set()
        future.result(timeout=1)
        background.drain_background_jobs()

    def test_shutdown_drains_upload_before_its_final_task_log(self):
        entered, release, shutdown_started = (threading.Event() for _ in range(3))
        persisted = mock.Mock()
        def upload():
            entered.set()
            self.assertTrue(release.wait(2))
            background.submit_task_log(persisted, "final")
        background.submit_upload(upload)
        self.assertTrue(entered.wait(1))
        errors = []
        def shutdown():
            shutdown_started.set()
            try:
                background.shutdown_background_jobs()
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=shutdown)
        thread.start()
        try:
            self.assertTrue(shutdown_started.wait(1))
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        persisted.assert_called_once_with("final")
        with self.assertRaises(RuntimeError):
            background.submit_upload(lambda: None)
        with self.assertRaises(RuntimeError):
            background.submit_task_log(lambda: None)
        background.shutdown_background_jobs()

    def test_lifecycle_can_be_explicitly_restarted_but_submit_never_reopens(self):
        background.start_background_jobs()
        first = background.get_background_jobs()
        background.shutdown_background_jobs()
        with self.assertRaises(RuntimeError):
            background.submit_task_log(lambda: None)
        background.start_background_jobs()
        self.assertIsNot(background.get_background_jobs(), first)
        self.assertEqual(background.submit_task_log(lambda: 7).result(timeout=1), 7)

    def test_manager_lifecycle_from_a_worker_is_rejected_without_deadlock(self):
        jobs = background.get_background_jobs()
        for queue in (jobs.uploads, jobs.task_logs):
            with mock.patch.object(background.logger, "error"):
                for operation in (jobs.shutdown, jobs.drain):
                    future = queue.submit(operation)
                    with self.assertRaisesRegex(RuntimeError, "from their workers"):
                        future.result(timeout=1)
        self.assertEqual(background.submit_task_log(lambda: "alive").result(timeout=1), "alive")

    def test_unused_drain_and_shutdown_do_not_construct_services(self):
        with mock.patch.object(background, "BackgroundJobs") as factory:
            background.drain_background_jobs()
            background.shutdown_background_jobs()
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
