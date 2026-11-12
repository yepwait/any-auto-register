"""Independent Web pool quota/refill API; never calls phone inventory automation."""
from __future__ import annotations

import threading
import hmac
import time
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict

from api import tasks
from core.config_store import config_store
from core.task_runtime import StopTaskRequested
from services import chatgpt_web_pool as service
from services import web_pool_events as events

router = APIRouter(prefix="/chatgpt-web-pool", tags=["chatgpt-web-pool"])


class ConfigUpdate(BaseModel):
    config: service.WebPoolSettings
    confirm_registration: bool = False


class RunRequest(BaseModel):
    confirm_registration: bool = False


def _validate(settings):
    try:
        settings.validate_connection()
    except service.PoolError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/config")
def get_config():
    return service.WebPoolSettings.from_store().public_dict()


@router.put("/config")
def put_config(body: ConfigUpdate):
    previous = service.WebPoolSettings.from_store()
    settings = body.config
    if not settings.admin_token:
        if settings.base_url and settings.base_url != previous.base_url and previous.admin_token:
            raise HTTPException(400, "更换服务地址时必须重新输入管理员密钥，避免旧密钥发送到新目标")
        settings = settings.model_copy(update={"admin_token": previous.admin_token})
    if settings.enabled:
        if not body.confirm_registration:
            raise HTTPException(400, "自动补号不会接手机短信，但可能产生邮箱费用，请明确确认")
        _validate(settings)
    with tasks.chatgpt_enqueue_lock:
        service.save_settings(settings)
        # Configuration changes stop an existing run before sending any more
        # credentials to its old target. Blank token preserves the saved secret.
        if settings != previous or not settings.enabled:
            events.invalidate("配置已变更或自动补号已关闭，旧实时待办已取消")
            for task in tasks._task_store.list_snapshots():
                if task.get("source") == service.SOURCE and task["status"] in {"pending", "running"}:
                    tasks._task_store.request_stop(task["id"])
    if settings.enabled and not previous.enabled:
        from core.scheduler import scheduler

        scheduler.trigger_web_pool_now()
    return {"ok": True, "config": settings.public_dict()}


@router.get("/status")
def get_status():
    settings = service.WebPoolSettings.from_store()
    history = tasks._list_persisted_tasks(source=service.SOURCE, limit=20)
    history.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    used = service.daily_used()
    return {
        "enabled": settings.enabled,
        "snapshot": service.last_snapshot(settings),
        "pending_uploads": service.pending_count(settings),
        "realtime": events.status(),
        "daily_registrations_used": used,
        "daily_registration_unlimited": settings.daily_registration_limit == 0,
        "daily_registrations_remaining": (
            None if settings.daily_registration_limit == 0
            else max(0, settings.daily_registration_limit - used)
        ),
        "budget_date_utc": service.today(),
        "active": next((item for item in history if item["status"] in {"pending", "running"}), None),
        "tasks": history[:20],
    }


@router.post("/check")
def check():
    settings = service.WebPoolSettings.from_store()
    _validate(settings)
    try:
        return {"snapshot": service.check_quota(settings)}
    except service.PoolError as exc:
        raise HTTPException(502, str(exc)) from exc


def _register_one(parent_id: str, settings: service.WebPoolSettings, control) -> int | None:
    request = tasks._prepare_register_request(tasks.RegisterTaskRequest(
        platform="chatgpt", count=1, concurrency=1, register_retry_times=0,
        executor_type="protocol", captcha_solver=config_store.get("default_captcha_solver", "yescaptcha"),
        extra={
            "chatgpt_web_only": "1",
            "chatgpt_web_pool_target": settings.target_id,
            "chatgpt_register_flow": "email",
            "chatgpt_registration_mode": "access_token_only",
            "chatgpt_bind_2fa": "0",
            "chatgpt_require_rt_ready": "0",
            "chatgpt_require_sms_verified": "0",
            "sms_enabled": "0",
        },
    ))
    child_id = f"{service.REGISTER_SOURCE}_{uuid4().hex}"
    tasks._create_task_record(child_id, request, service.REGISTER_SOURCE, {"parent_task_id": parent_id})
    parent_meta = tasks._task_store.snapshot(parent_id, include_logs=False).get("meta") or {}
    # Full child history lives in task_runs by parent_task_id; retain only a
    # bounded convenience tail in an unlimited parent's live metadata.
    tasks._task_store.update_meta(parent_id, {"child_task_ids": [*(parent_meta.get("child_task_ids") or []), child_id][-100:]})
    tasks._persist_task_snapshot(parent_id)
    tasks._log(parent_id, f"[Web 注册子任务] {child_id}")
    tasks._run_register(child_id, request, task_control=control)
    control.checkpoint()
    snapshot = tasks._get_task_snapshot(child_id)
    ids = (snapshot.get("meta") or {}).get("account_ids") or []
    if snapshot.get("success", 0) <= 0 or not ids:
        return None
    fields = tasks._load_account_fields(ids[-1])
    if fields is None:
        return None
    extra = fields.get("extra") or {}
    if not (extra.get("access_token") or extra.get("accessToken") or fields.get("token")):
        tasks._log(parent_id, "[FAIL] 注册身份已保留，但没有 Web access_token，不能上传")
        return None
    return int(ids[-1])


def _run(task_id: str, settings: service.WebPoolSettings, trigger: str):
    control = tasks._task_store.control_for(task_id)
    result = service.PoolRunResult()
    status = "done"
    outcome = "completed"
    starting_secret = config_store.get(events.SECRET_KEY, "").strip() if trigger == "webhook" else ""

    def progress(value):
        nonlocal result
        result = value
        tasks._task_store.update_counters(task_id, success=value.success, registered=value.attempts,
                                         error_count=max(value.error_count, len(value.errors)))
        limit = "不限" if settings.max_attempts_per_run == 0 else str(settings.max_attempts_per_run)
        tasks._task_store.set_progress(task_id, f"{value.attempts}/{limit}")
        tasks._task_store.update_meta(task_id, {"registrations": value.registrations, "snapshot": value.snapshot})
        tasks._persist_task_snapshot(task_id)

    def should_continue():
        current = service.WebPoolSettings.from_store()
        # A queued job cannot continue using an old destination/token/policy.
        if trigger == "webhook":
            secret = config_store.get(events.SECRET_KEY, "").strip()
            if not starting_secret or not hmac.compare_digest(secret.encode(), starting_secret.encode()):
                return False
        return current == settings and (trigger not in {"schedule", "webhook"} or current.enabled)

    try:
        tasks._task_store.mark_running(task_id)
        tasks._persist_task_snapshot(task_id)
        result = service.replenish(
            settings, control=control, log=lambda text: tasks._log(task_id, text),
            register=lambda config, switch: _register_one(task_id, config, switch),
            progress=progress, should_continue=should_continue,
            refill_active=bool((tasks._task_store.snapshot(task_id, include_logs=False).get("meta") or {}).get("refill_latched")),
        )
        if result.errors and (not result.snapshot or not service.target_reached(settings, result.snapshot)):
            status = "failed"
            outcome = "failed"
    except service.QuotaUnavailable as exc:
        # This is a confirmed read with an incomparable numeric goal, not a
        # failed registration or a 0-quota pool. Keep the snapshot for the UI.
        outcome = "quota_unknown"
        result.reason = str(exc)
    except StopTaskRequested:
        status = "stopped"
        outcome = "stopped"
        result.reason = "Web 号池任务已停止，已注册账号保留本地"
    except service.PoolError as exc:
        status = "failed"
        outcome = "failed"
        result.reason = str(exc)
        result.add_error(result.reason)
    except Exception as exc:
        status = "failed"
        outcome = "failed"
        result.reason = f"Web 号池任务异常（{type(exc).__name__}），未继续注册"
        result.add_error(result.reason)
    # Complete runtime and event claim under the dispatch lock. Otherwise a
    # dispatcher can see terminal status before the ACK and launch a duplicate.
    with tasks.chatgpt_enqueue_lock:
        tasks._task_store.update_meta(task_id, {"reason": result.reason, "snapshot": result.snapshot,
                                               "registrations": result.registrations,
                                               "refill_started": result.refill_started,
                                               "budget_exhausted": result.budget_exhausted,
                                               "outcome": outcome})
        tasks._task_store.finish(task_id, status=status, success=result.success, registered=result.attempts,
                                 skipped=result.skipped, errors=result.errors, error_count=result.error_count)
        try:
            tasks._log(task_id, f"[Web 号池结束] {result.reason}；已确认可用 {result.success}，注册尝试 {result.registrations}")
            tasks._persist_task_snapshot(task_id)
        except Exception:
            # Preserve terminal state for the task writer and event recovery.
            with tasks._log_io_lock:
                tasks._dirty_task_ids.add(task_id)
            return
        if trigger == "webhook":
            failed = status == "failed" or bool(result.errors)
            no_need = False
            if not failed and result.snapshot is not None:
                try:
                    no_need = service.target_reached(settings, result.snapshot) or not result.refill_started
                except service.PoolError:
                    failed = True
            events.complete(task_id, reached=no_need, stopped=status == "stopped" or not should_continue(),
                            blocked=outcome == "quota_unknown",
                            retry=not no_need and result.refill_started and not failed,
                            failed=failed, budget_exhausted=result.budget_exhausted,
                            refill_latched=result.refill_started and not no_need, reason=result.reason)
            events.wake()
        tasks._task_store.cleanup()


def enqueue(*, trigger="schedule", background_tasks: BackgroundTasks | None = None,
            prepared_task_id: str | None = None, refill_latched: bool = False):
    settings = service.WebPoolSettings.from_store()
    if trigger in ("schedule", "webhook") and not settings.enabled:
        return {"triggered": False, "reason": "Web 号池自动巡检未启用"}
    _validate(settings)
    with tasks.chatgpt_enqueue_lock:
        tasks._finalize_orphan_tasks()
        if tasks.has_active_register_task(platform="chatgpt"):
            if trigger == "manual":
                raise HTTPException(409, "已有 GPT 任务执行中，请结束后再运行 Web 号池巡检")
            return {"triggered": False, "reason": "已有 GPT 任务执行中"}
        tasks.ensure_task_capacity()
        task_id = prepared_task_id or f"{service.SOURCE}_{uuid4().hex}"
        tasks._task_store.create(
            task_id, platform="chatgpt", total=settings.max_attempts_per_run, source=service.SOURCE,
            meta={"kind": service.SOURCE, "trigger": trigger, "settings": settings.public_dict(),
                  "refill_latched": refill_latched, "attempt_limit_unlimited": settings.max_attempts_per_run == 0},
        )
        tasks._task_store.set_progress(task_id, "0/不限" if settings.max_attempts_per_run == 0 else f"0/{settings.max_attempts_per_run}")
        try:
            tasks._persist_task_snapshot(task_id)
            tasks._log(task_id, "Web 号池独立巡检：先读取远端图片额度；不足才补传/注册，不接手机短信、不补 RT")
        except Exception:
            tasks._task_store.finish(task_id, status="failed", success=0, registered=0, skipped=0,
                                     errors=["初始任务持久化失败，未启动注册"])
            with tasks._log_io_lock:
                tasks._dirty_task_ids.add(task_id)
            raise
    try:
        if background_tasks is not None:
            background_tasks.add_task(_run, task_id, settings, trigger)
        else:
            threading.Thread(target=_run, args=(task_id, settings, trigger), daemon=True).start()
    except Exception:
        tasks._task_store.finish(task_id, status="failed", success=0, registered=0, skipped=0,
                                 errors=["后台线程启动失败，实时待办保留等待重试"])
        tasks._persist_task_snapshot(task_id)
        raise
    return {"triggered": True, "task_id": task_id}


def dispatch_pending_trigger(*, background_tasks: BackgroundTasks | None = None) -> dict:
    """Fixed worker calls this; busy tasks retain a durable coalesced wake-up."""
    with tasks.chatgpt_enqueue_lock, events.lock:
        current_event = events.status()
        if not current_event["pending"] and not current_event["active_task_id"]:
            return {"triggered": False, "reason": "没有实时待办"}
        settings = service.WebPoolSettings.from_store()
        state = events.pending_state(settings)
        if state is None:
            return {"triggered": False, "reason": "没有到期的实时待办"}
        active_id = state["active_task_id"]
        if active_id:
            snapshot = (tasks._task_store.snapshot(active_id, include_logs=False)
                        if tasks._task_store.exists(active_id) else tasks._get_persisted_task(active_id))
            if snapshot:
                if snapshot["status"] in {"pending", "running"} and tasks._task_store.exists(active_id):
                    return {"triggered": False, "reason": "实时任务已在处理中"}
                if snapshot["status"] == "stopped" and "重启" not in str(snapshot.get("error") or ""):
                    events.complete(active_id, reached=False, stopped=True, reason="上次任务已停止，不自动恢复注册")
                    return {"triggered": False, "reason": "上次任务已停止"}
                if (snapshot.get("meta") or {}).get("outcome") == "quota_unknown":
                    events.complete(active_id, reached=False, blocked=True,
                                    reason="远端额度未知，等待数据更新；本任务未继续注册")
                    return {"triggered": False, "queued": False}
                if snapshot["status"] == "failed":
                    events.complete(active_id, reached=False, failed=True, reason="上次任务未完成确认，等待冷却复核",
                                    refill_latched=bool(state["refill_latched"] or (snapshot.get("meta") or {}).get("refill_started")),
                                    budget_exhausted=bool((snapshot.get("meta") or {}).get("budget_exhausted")))
                    return {"triggered": False, "queued": events.status()["pending"]}
            events.recover(active_id)
            state = events.pending_state(settings)
            if state is None:
                return {"triggered": False}
        if tasks.has_active_register_task(platform="chatgpt"):
            events.waiting("已有 GPT 任务执行中，待办已保留，空闲后自动复核")
            return {"triggered": False, "queued": True, "reason": "等待空闲"}
        try:
            tasks.ensure_task_capacity()
            _validate(settings)
        except HTTPException as exc:
            events.waiting("等待任务容量或有效连接配置")
            return {"triggered": False, "queued": True, "reason": "等待任务容量或配置"}
        task_id = f"{service.SOURCE}_{uuid4().hex}"
        if not events.claim(task_id, settings):
            return {"triggered": False}
        try:
            result = enqueue(trigger="webhook", background_tasks=background_tasks,
                             prepared_task_id=task_id, refill_latched=bool(state["refill_latched"]))
            if not result.get("triggered"):
                events.recover(task_id)
            return result
        except Exception:
            events.complete(task_id, reached=False, failed=True, reason="任务提交失败，稍后重试",
                            refill_latched=bool(state["refill_latched"]))
            return {"triggered": False, "queued": True, "reason": "任务提交失败，待办保留"}


class TriggerRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Existing senders with only {total_quota, threshold} remain compatible.
    secret: str = Field(default="", max_length=512, repr=False)
    event_id: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_.:-]*$")
    sent_at: float | None = Field(default=None, allow_inf_nan=False)


def verify_trigger_secret(provided: str) -> None:
    expected = str(config_store.get(events.SECRET_KEY, "") or "").strip()
    if not expected:
        raise HTTPException(503, "实时触发未配置密钥，入口已关闭")
    if len(provided) > 512 or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(401, "无效的触发密钥")


@router.post("/trigger", status_code=202)
def trigger(body: TriggerRequest, request: Request):
    """ACK only after durable enqueue; never trusts sender quota/threshold values."""
    header_secret = (request.headers.get("X-Replenish-Secret") or "").strip()
    if header_secret and body.secret and header_secret != body.secret.strip():
        raise HTTPException(401, "触发密钥不一致")
    verify_trigger_secret(header_secret or body.secret.strip())
    if body.sent_at is not None and abs(time.time() - body.sent_at) > events.EVENT_TTL_SECONDS:
        raise HTTPException(400, "事件时间过期，请使用新的事件编号重试")
    if not events.rate_limit.allow():
        raise HTTPException(429, "实时通知过于频繁，请合并后重试", headers={"Retry-After": "5"})
    with tasks.chatgpt_enqueue_lock:
        settings = service.WebPoolSettings.from_store()
        if not settings.enabled:
            return {"accepted": False, "queued": False, "triggered": False, "reason": "Web 号池自动补号未启用"}
        _validate(settings)
        result = events.accept(settings, event_id=body.event_id)
    # Wake an existing fixed worker. Never create a per-event registration thread.
    events.wake()
    return result


@router.post("/run")
def run_now(body: RunRequest, background_tasks: BackgroundTasks):
    if not body.confirm_registration:
        raise HTTPException(400, "补号可能产生邮箱费用（不会接手机短信），请明确确认")
    return enqueue(trigger="manual", background_tasks=background_tasks)
