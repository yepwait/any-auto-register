"""注册任务运行时控制与状态存储。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import threading
import time
import os
from typing import Any


class TaskInterruption(RuntimeError):
    """任务执行过程中触发的协作式中断。"""


class StopTaskRequested(TaskInterruption):
    """整个任务被手动停止。"""

    def __init__(self, message: str = "任务已手动停止"):
        super().__init__(message)


class SkipCurrentAttemptRequested(TaskInterruption):
    """当前账号被手动跳过。"""

    def __init__(self, message: str = "已手动跳过当前账号"):
        super().__init__(message)


class NonRetryableRegisterError(RuntimeError):
    """重开一轮也是同样结局的失败。

    典型是手机注册里"账号已建好、但接码平台一条短信都没收到"：号源被静默拦下，
    再开一轮只会用同样的号源再造一个没人认领的孤号，还多花一次租号钱。
    """


class AttemptOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    STOPPED = "stopped"


@dataclass(slots=True)
class AttemptResult:
    outcome: AttemptOutcome
    message: str = ""
    # 有些失败重开一轮也是同样的结局，还要再赔上一次租号和一个半成品账号
    retryable: bool = True
    # 这一轮用的身份（邮箱或手机号）。多轮重试时只有最终结果才落注册记录，
    # 所以身份要跟着结果一起交回去。
    email: str = ""

    @classmethod
    def success(cls) -> "AttemptResult":
        return cls(AttemptOutcome.SUCCESS)

    @classmethod
    def failed(
        cls, message: str, *, retryable: bool = True, email: str = ""
    ) -> "AttemptResult":
        return cls(AttemptOutcome.FAILED, message, retryable=retryable, email=email)

    @classmethod
    def skipped(cls, message: str) -> "AttemptResult":
        return cls(AttemptOutcome.SKIPPED, message)

    @classmethod
    def stopped(cls, message: str) -> "AttemptResult":
        return cls(AttemptOutcome.STOPPED, message)


class RegisterTaskControl:
    """协作式任务控制器：支持停止整个任务、跳过一个当前账号。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop_requested = False
        self._pending_skip_requests = 0
        self._next_attempt_id = 1
        self._active_attempt_ids: set[int] = set()
        self._skip_active_attempt_ids: set[int] = set()

    def request_stop(self) -> None:
        with self._lock:
            self._stop_requested = True

    def request_skip_current(self) -> None:
        with self._lock:
            if self._active_attempt_ids:
                self._skip_active_attempt_ids.update(self._active_attempt_ids)
            else:
                self._pending_skip_requests += 1

    def start_attempt(self) -> int:
        with self._lock:
            attempt_id = self._next_attempt_id
            self._next_attempt_id += 1
            self._active_attempt_ids.add(attempt_id)
            return attempt_id

    def finish_attempt(self, attempt_id: int | None) -> None:
        if attempt_id is None:
            return
        with self._lock:
            self._active_attempt_ids.discard(attempt_id)
            self._skip_active_attempt_ids.discard(attempt_id)

    def checkpoint(
        self,
        *,
        consume_skip: bool = True,
        attempt_id: int | None = None,
    ) -> None:
        with self._lock:
            if self._stop_requested:
                raise StopTaskRequested()
            if consume_skip:
                if (
                    attempt_id is not None
                    and attempt_id in self._skip_active_attempt_ids
                ):
                    self._skip_active_attempt_ids.discard(attempt_id)
                    raise SkipCurrentAttemptRequested()
                if self._pending_skip_requests > 0:
                    self._pending_skip_requests -= 1
                    raise SkipCurrentAttemptRequested()

    def is_stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stop_requested": self._stop_requested,
                "pending_skip_requests": self._pending_skip_requests,
                "active_attempts": len(self._active_attempt_ids),
                "targeted_skip_attempts": len(self._skip_active_attempt_ids),
            }


@dataclass
class RegisterTaskRecord:
    id: str
    platform: str
    source: str
    total: int
    meta: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    progress: str = "0/0"
    logs: list[str] = field(default_factory=list)
    log_offset: int = 0
    log_count: int = 0
    log_bytes: int = 0
    success: int = 0
    registered: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    cashier_urls: list[str] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    control: RegisterTaskControl = field(
        default_factory=RegisterTaskControl,
        repr=False,
    )

    def to_dict(self, *, include_logs: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "status": self.status,
            "platform": self.platform,
            "source": self.source,
            "meta": dict(self.meta),
            "total": self.total,
            "progress": self.progress,
            "logs": list(self.logs) if include_logs else [],
            "log_offset": self.log_offset,
            "log_count": self.log_count,
            "success": self.success,
            "registered": self.registered,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "control": self.control.snapshot(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.cashier_urls:
            data["cashier_urls"] = list(self.cashier_urls)
        if self.error:
            data["error"] = self.error
        return data


class RegisterTaskStore:
    """线程安全的注册任务存储。"""

    def __init__(
        self,
        *,
        max_finished_tasks: int = 200,
        cleanup_threshold: int = 250,
        max_log_lines: int | None = None,
        max_log_bytes: int | None = None,
    ):
        self._lock = threading.Lock()
        self._records: dict[str, RegisterTaskRecord] = {}
        self.max_finished_tasks = max_finished_tasks
        self.cleanup_threshold = cleanup_threshold
        def limit(value, env_key, default, minimum):
            try:
                return max(minimum, int(value if value is not None else os.getenv(env_key, str(default))))
            except (TypeError, ValueError):
                return default
        self.max_log_lines = limit(max_log_lines, "APP_TASK_LOG_TAIL_LINES", 2000, 1)
        self.max_log_bytes = limit(max_log_bytes, "APP_TASK_LOG_TAIL_BYTES", 1048576, 256)

    def create(
        self,
        task_id: str,
        *,
        platform: str,
        total: int,
        source: str,
        meta: dict[str, Any] | None = None,
    ) -> RegisterTaskRecord:
        with self._lock:
            record = RegisterTaskRecord(
                id=task_id,
                platform=platform,
                total=total,
                source=source,
                meta=dict(meta or {}),
                progress=f"0/{total}",
            )
            self._records[task_id] = record
            return record

    def exists(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._records

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._records.pop(task_id, None)

    def has_active(
        self,
        *,
        platform: str | None = None,
        source: str | None = None,
    ) -> bool:
        with self._lock:
            for record in self._records.values():
                if record.status not in ("pending", "running"):
                    continue
                if platform and record.platform != platform:
                    continue
                if source and record.source != source:
                    continue
                return True
        return False

    def control_for(self, task_id: str) -> RegisterTaskControl:
        with self._lock:
            return self._records[task_id].control

    def bind_control(self, task_id: str, control: RegisterTaskControl) -> None:
        """父子任务共享中断开关，任一入口停止都能传递到真实执行链。"""
        with self._lock:
            self._records[task_id].control = control

    def request_stop(self, task_id: str) -> dict[str, Any]:
        control = self.control_for(task_id)
        control.request_stop()
        return control.snapshot()

    def request_skip_current(self, task_id: str) -> dict[str, Any]:
        control = self.control_for(task_id)
        control.request_skip_current()
        return control.snapshot()

    def append_log(self, task_id: str, entry: str) -> int | None:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return None
            sequence = record.log_count
            record.log_count += 1
            record.logs.append(entry)
            record.log_bytes += len(entry.encode("utf-8"))
            while len(record.logs) > self.max_log_lines or (record.log_bytes > self.max_log_bytes and len(record.logs) > 1):
                removed = record.logs.pop(0)
                record.log_bytes -= len(removed.encode("utf-8"))
                record.log_offset += 1
            record.updated_at = time.time()
            return sequence

    def parent_task_id(self, task_id: str) -> str | None:
        with self._lock:
            record = self._records.get(task_id)
            return (record.meta.get("parent_task_id") if record else None)

    def log_slice(self, task_id: str, since: int = 0, limit: int = 500) -> dict[str, Any]:
        """Absolute cursors survive tail eviction and client reconnects."""
        with self._lock:
            record = self._records[task_id]
            start = max(record.log_offset, since)
            lines = record.logs[start - record.log_offset:start - record.log_offset + max(1, limit)]
            return {"logs": list(lines), "log_offset": start, "next_since": start + len(lines),
                    "log_count": record.log_count, "status": record.status,
                    "success": record.success, "registered": record.registered, "total": record.total}

    def update_meta(self, task_id: str, patch: dict[str, Any]) -> None:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return
            record.meta.update(patch)
            record.updated_at = time.time()

    def append_meta_list(self, task_id: str, key: str, value: Any) -> None:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return
            current = record.meta.get(key)
            values = list(current) if isinstance(current, list) else []
            values.append(value)
            record.meta[key] = values
            record.updated_at = time.time()

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            record = self._records[task_id]
            record.status = "running"
            record.updated_at = time.time()

    def set_progress(self, task_id: str, progress: str) -> None:
        with self._lock:
            record = self._records[task_id]
            record.progress = progress
            record.updated_at = time.time()

    def add_cashier_url(self, task_id: str, cashier_url: str) -> None:
        with self._lock:
            record = self._records[task_id]
            record.cashier_urls.append(cashier_url)
            record.updated_at = time.time()

    def update_counters(
        self,
        task_id: str,
        *,
        success: int | None = None,
        registered: int | None = None,
    ) -> None:
        with self._lock:
            record = self._records[task_id]
            if success is not None:
                record.success = max(0, int(success))
            if registered is not None:
                record.registered = max(0, int(registered))
            record.updated_at = time.time()

    def finish(
        self,
        task_id: str,
        *,
        status: str,
        success: int,
        registered: int | None = None,
        skipped: int,
        errors: list[str],
        error: str = "",
    ) -> None:
        with self._lock:
            record = self._records[task_id]
            record.status = status
            record.success = success
            if registered is None:
                record.registered = max(success + skipped + len(errors), 0)
            else:
                record.registered = max(0, int(registered))
            record.skipped = skipped
            record.errors = list(errors)
            record.error = error
            record.updated_at = time.time()

    def snapshot(self, task_id: str, *, include_logs: bool = True) -> dict[str, Any]:
        with self._lock:
            return self._records[task_id].to_dict(include_logs=include_logs)

    def list_snapshots(self, *, include_logs: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            return [record.to_dict(include_logs=include_logs) for record in self._records.values()]

    def log_state(self, task_id: str) -> tuple[list[str], str]:
        with self._lock:
            record = self._records[task_id]
            return list(record.logs), record.status

    def cleanup(self) -> None:
        with self._lock:
            if len(self._records) <= self.cleanup_threshold:
                return
            finished = [
                (task_id, record)
                for task_id, record in self._records.items()
                if record.status in ("done", "failed", "stopped")
            ]
            if len(finished) <= self.max_finished_tasks:
                return
            finished.sort(key=lambda item: item[1].created_at)
            to_remove = finished[: len(finished) - self.max_finished_tasks]
            for task_id, _ in to_remove:
                self._records.pop(task_id, None)


__all__ = [
    "AttemptOutcome",
    "AttemptResult",
    "RegisterTaskControl",
    "RegisterTaskRecord",
    "RegisterTaskStore",
    "SkipCurrentAttemptRequested",
    "StopTaskRequested",
    "TaskInterruption",
]
