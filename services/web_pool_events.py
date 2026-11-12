"""Durable, coalescing wake-ups for the Web pool (single app process).

Only one pending wake-up and one active task are stored. Sender payloads are
hints, never registration instructions. No token, credential or supplied URL
is written into this state. All callers serialize with the same reentrant lock.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from typing import Any

from core.config_store import config_store

STATE_KEY = "chatgpt_web_pool_trigger_state"
SECRET_KEY = "chatgpt_web_pool_trigger_secret"
MIN_CHECK_INTERVAL = 5.0
BUSY_RETRY_SECONDS = 5.0
MAX_RETRY_RUNS = 3
MAX_RECENT_EVENTS = 128
EVENT_TTL_SECONDS = 600
lock = threading.RLock()


def settings_fingerprint(settings) -> str:
    # Binds queued work to the exact authorized destination/policy, including
    # credential changes, without persisting those credentials in the queue.
    # Trigger-key rotation also revokes already queued events. Only the final
    # hash is persisted; neither the trigger key nor admin token is stored here.
    raw = json.dumps({"settings": settings.model_dump(),
                      "trigger_key": config_store.get(SECRET_KEY, "").strip()},
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _default() -> dict[str, Any]:
    return {
        "version": 1, "fingerprint": "", "pending": False, "active_task_id": "",
        "status": "idle", "reason": "尚未收到实时通知", "last_received_at": 0.0,
        "last_started_at": 0.0, "last_finished_at": 0.0, "next_attempt_at": 0.0,
        "received": 0, "coalesced": 0, "failure_runs": 0, "recent_events": [],
        "refill_latched": False,
    }


def _load() -> dict[str, Any]:
    raw = config_store.get(STATE_KEY, "")
    if not raw:
        return _default()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError
        state = {**_default(), **{key: value for key, value in data.items() if key in _default()}}
        for key in ("last_received_at", "last_started_at", "last_finished_at", "next_attempt_at"):
            if isinstance(state[key], bool) or not isinstance(state[key], (int, float)) or not math.isfinite(state[key]):
                raise ValueError
        for key in ("received", "coalesced", "failure_runs"):
            if not isinstance(state[key], int) or state[key] < 0:
                raise ValueError
        if not isinstance(state["recent_events"], list):
            raise ValueError
        for key in ("pending", "refill_latched"):
            if type(state[key]) is not bool:
                raise ValueError
        for key in ("fingerprint", "active_task_id", "status", "reason"):
            if not isinstance(state[key], str):
                raise ValueError
        return state
    except (ValueError, TypeError):
        # A damaged queue is not interpreted as a fresh request to spend money.
        state = _default()
        state.update(status="error", reason="实时通知状态损坏，请重新保存配置后重试")
        return state


def _save(state: dict[str, Any]) -> None:
    config_store.set_many({STATE_KEY: json.dumps(state, ensure_ascii=False, separators=(",", ":"))})


def status() -> dict[str, Any]:
    with lock:
        state = _load()
    return {key: state[key] for key in (
        "pending", "active_task_id", "status", "reason", "last_received_at",
        "last_started_at", "last_finished_at", "next_attempt_at", "received",
        "coalesced", "failure_runs", "refill_latched",
    )} | {"secret_configured": bool(config_store.get(SECRET_KEY, "").strip())}


def invalidate(reason: str = "配置已更改，旧实时待办已取消") -> None:
    with lock:
        old = _load()
        state = _default()
        for key in ("received", "coalesced", "last_received_at"):
            state[key] = old[key]
        state.update(status="paused", reason=reason)
        _save(state)


def accept(settings, *, event_id: str = "", now: float | None = None) -> dict[str, Any]:
    """Persist before ACK; incoming duplicates never create independent jobs."""
    now = time.time() if now is None else now
    fingerprint = settings_fingerprint(settings)
    digest = hashlib.sha256(event_id.encode()).hexdigest() if event_id else ""
    with lock:
        state = _load()
        if state["fingerprint"] != fingerprint:
            state = _default()
            state["fingerprint"] = fingerprint
        recent = [item for item in state["recent_events"] if (
            isinstance(item, dict) and isinstance(item.get("at"), (int, float))
            and now - item["at"] <= EVENT_TTL_SECONDS
        )][-MAX_RECENT_EVENTS:]
        duplicate = bool(digest and any(item.get("digest") == digest for item in recent))
        if duplicate:
            return {"accepted": True, "queued": bool(state["pending"]), "coalesced": True,
                    "duplicate": True, "triggered": False, "task_id": state["active_task_id"] or None,
                    "reason": "重复事件已确认，不重复创建任务"}
        coalesced = bool(state["pending"] or state["active_task_id"])
        state["pending"] = True
        state["received"] += 1
        state["coalesced"] += int(coalesced)
        state["last_received_at"] = now
        if not state["active_task_id"]:
            # A genuinely later event after the open circuit may start a new
            # bounded retry series, never events within the cooldown window.
            if state["failure_runs"] >= MAX_RETRY_RUNS and now >= state["next_attempt_at"]:
                state["failure_runs"] = 0
            state["status"] = "pending" if now >= state["next_attempt_at"] else "cooldown"
            state["reason"] = "实时通知已保存，等待额度复核"
        if digest:
            recent.append({"digest": digest, "at": now})
        state["recent_events"] = recent[-MAX_RECENT_EVENTS:]
        _save(state)
        return {"accepted": True, "queued": True, "coalesced": coalesced, "duplicate": False,
                "triggered": False, "task_id": state["active_task_id"] or None,
                "reason": "已合并到待检查队列" if coalesced else "已保存实时待办，额度不足时执行补号"}


def pending_state(settings, *, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    with lock:
        state = _load()
        if not state["pending"] and not state["active_task_id"]:
            return None
        if not settings.enabled or state["fingerprint"] != settings_fingerprint(settings):
            invalidate("自动补号关闭或配置改变，旧实时待办已取消")
            return None
        if not config_store.get(SECRET_KEY, "").strip():
            invalidate("未配置实时触发密钥，实时待办已暂停")
            return None
        if state["active_task_id"]:
            return dict(state)
        if now < state["next_attempt_at"]:
            return None
        return dict(state)


def waiting(reason: str) -> None:
    with lock:
        state = _load()
        if state["status"] == "waiting_busy" and state["reason"] == reason:
            return  # Do not rewrite the same busy state every few seconds.
        state.update(status="waiting_busy", reason=reason)
        _save(state)


def claim(task_id: str, settings) -> bool:
    with lock:
        state = pending_state(settings)
        if not state or state["active_task_id"] or not state["pending"]:
            return False
        now = time.time()
        state.update(pending=False, active_task_id=task_id, status="processing",
                     reason="正在核对额度并按需补号", last_started_at=now,
                     next_attempt_at=now + MIN_CHECK_INTERVAL)
        _save(state)
        return True


def recover(task_id: str) -> None:
    """Retry a missing/finished active task with a new *quota check*, not blind signup."""
    with lock:
        state = _load()
        if state["active_task_id"] != task_id:
            return
        state.update(active_task_id="", pending=True, status="pending", next_attempt_at=0.0,
                     reason="上次任务已结束或服务重启，重新复核额度")
        _save(state)


def complete(task_id: str, *, reached: bool, stopped: bool = False,
             retry: bool = False, failed: bool = False, reason: str = "",
             budget_exhausted: bool = False, refill_latched: bool = False, blocked: bool = False) -> None:
    with lock:
        state = _load()
        if state["active_task_id"] != task_id:
            return  # Configuration invalidated this generation; cannot resurrect it.
        now = time.time()
        state.update(active_task_id="", last_finished_at=now, reason=reason[:240])
        if stopped:
            state.update(pending=False, status="paused", refill_latched=False,
                         next_attempt_at=now + 30)
        elif blocked:
            # Unknown quota is not a failed registration. Do not immediately
            # self-retry a decision that needs new remote data or user policy.
            state.update(pending=False, status="paused", failure_runs=0,
                         refill_latched=False, next_attempt_at=now + 30)
        elif reached:
            state.update(failure_runs=0, refill_latched=False,
                         status="pending" if state["pending"] else "idle")
        elif budget_exhausted:
            # Persist one wake-up for the next UTC day instead of creating tasks all day.
            next_day = (int(now) // 86400 + 1) * 86400
            state.update(pending=True, status="cooldown", next_attempt_at=next_day,
                         refill_latched=refill_latched)
        elif failed:
            count = state["failure_runs"] + 1
            delay = 300 if count >= MAX_RETRY_RUNS else min(300, 30 * (2 ** (count - 1)))
            # Events received during a failing run cannot bypass the failure
            # ceiling. A later event may request another check only after the
            # five-minute circuit cooldown; no automatic infinite retry chain.
            state.update(failure_runs=count, next_attempt_at=now + delay,
                         pending=count < MAX_RETRY_RUNS,
                         status="cooldown" if count < MAX_RETRY_RUNS else "error",
                         refill_latched=refill_latched)
        else:
            state.update(pending=retry or state["pending"], failure_runs=0,
                         next_attempt_at=max(state["next_attempt_at"], now + MIN_CHECK_INTERVAL),
                         status="pending" if retry or state["pending"] else "idle",
                         refill_latched=refill_latched)
        _save(state)


class TriggerRateLimit:
    """One fixed-size token bucket, not an unbounded per-IP dictionary."""
    def __init__(self, burst: int = 20, per_minute: int = 60):
        self._lock = threading.Lock()
        self.burst, self.per_minute = burst, per_minute
        self.tokens = float(burst)
        self.last = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self.tokens = min(float(self.burst), self.tokens + max(0, now - self.last) * self.per_minute / 60)
            self.last = now
            if self.tokens < 1:
                return False
            self.tokens -= 1
            return True


rate_limit = TriggerRateLimit()
_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None
_lifecycle = threading.Lock()


def wake() -> None:
    _wake.set()


def start() -> None:
    global _thread
    with _lifecycle:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        def run():
            import logging
            while not _stop.is_set():
                _wake.clear()  # An event received during processing remains set.
                try:
                    from api.chatgpt_web_pool import dispatch_pending_trigger
                    dispatch_pending_trigger()
                except Exception as exc:
                    logging.getLogger(__name__).warning("实时待办调度暂未完成 (%s)", type(exc).__name__)
                _wake.wait(BUSY_RETRY_SECONDS)
        _thread = threading.Thread(target=run, name="web-pool-events", daemon=True)
        _thread.start()


def stop() -> None:
    global _thread
    with _lifecycle:
        _stop.set()
        _wake.set()
        if _thread is not None:
            _thread.join(timeout=10)
            if not _thread.is_alive():
                _thread = None
