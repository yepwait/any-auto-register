"""Independent Web pool quota/refill API; never calls phone inventory automation."""
from __future__ import annotations

import threading
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from api import tasks
from core.config_store import config_store
from core.task_runtime import StopTaskRequested
from services import chatgpt_web_pool as service

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
    tasks._task_store.append_meta_list(parent_id, "child_task_ids", child_id)
    tasks._persist_task_snapshot(parent_id)
    tasks._log(parent_id, f"[Web 注册子任务] {child_id}")
    tasks._run_register(child_id, request, task_control=control)
    control.checkpoint()
    snapshot = tasks._task_store.snapshot(child_id)
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
    tasks._task_store.mark_running(task_id)
    tasks._persist_task_snapshot(task_id)
    result = service.PoolRunResult()
    status = "done"

    def progress(value):
        nonlocal result
        result = value
        tasks._task_store.update_counters(task_id, success=value.success, registered=value.attempts)
        tasks._task_store.set_progress(task_id, f"{value.attempts}/{settings.max_attempts_per_run}")
        tasks._task_store.update_meta(task_id, {"registrations": value.registrations, "snapshot": value.snapshot})
        tasks._persist_task_snapshot(task_id)

    def should_continue():
        current = service.WebPoolSettings.from_store()
        # schedule/webhook require enabled flag; manual always continues
        if trigger in ("schedule", "webhook"):
            return current.enabled
        return True

    try:
        result = service.replenish(
            settings, control=control, log=lambda text: tasks._log(task_id, text),
            register=lambda config, switch: _register_one(task_id, config, switch),
            progress=progress, should_continue=should_continue,
        )
        if result.errors and (not result.snapshot or not service.target_reached(settings, result.snapshot)):
            status = "failed"
    except StopTaskRequested:
        status = "stopped"
        result.reason = "Web 号池任务已停止，已注册账号保留本地"
    except service.PoolError as exc:
        status = "failed"
        result.reason = str(exc)
        result.errors.append(result.reason)
    except Exception as exc:
        status = "failed"
        result.reason = f"Web 号池任务异常（{type(exc).__name__}），未继续注册"
        result.errors.append(result.reason)
    tasks._log(task_id, f"[Web 号池结束] {result.reason}；已确认可用 {result.success}，注册尝试 {result.registrations}")
    tasks._task_store.update_meta(task_id, {"reason": result.reason, "snapshot": result.snapshot, "registrations": result.registrations})
    tasks._task_store.finish(task_id, status=status, success=result.success, registered=result.attempts, skipped=result.skipped, errors=result.errors)
    tasks._persist_task_snapshot(task_id)
    tasks._task_store.cleanup()


def enqueue(*, trigger="schedule", background_tasks: BackgroundTasks | None = None):
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
        task_id = f"{service.SOURCE}_{uuid4().hex}"
        tasks._task_store.create(
            task_id, platform="chatgpt", total=settings.max_attempts_per_run, source=service.SOURCE,
            meta={"kind": service.SOURCE, "trigger": trigger, "settings": settings.public_dict()},
        )
        tasks._persist_task_snapshot(task_id)
        tasks._log(task_id, "Web 号池独立巡检：先读取远端图片额度；不足才补传/注册，不接手机短信、不补 RT")
    if background_tasks is not None:
        background_tasks.add_task(_run, task_id, settings, trigger)
    else:
        threading.Thread(target=_run, args=(task_id, settings, trigger), daemon=True).start()
    return {"triggered": True, "task_id": task_id}


class TriggerRequest(BaseModel):
    secret: str = ""


@router.post("/trigger")
def trigger(body: TriggerRequest, background_tasks: BackgroundTasks, request: Request):
    """Receive a low-quota push from chatgpt2api and immediately enqueue a replenish run.

    Security: compare the shared secret configured in chatgpt_web_pool_trigger_secret.
    An empty secret means no token check (suitable for private internal networks only).
    """
    from fastapi.responses import JSONResponse

    expected = config_store.get("chatgpt_web_pool_trigger_secret", "").strip()
    header_secret = (request.headers.get("X-Replenish-Secret") or "").strip()
    provided = body.secret.strip() or header_secret
    if expected and provided != expected:
        raise HTTPException(401, "无效的触发密钥")
    try:
        result = enqueue(trigger="webhook", background_tasks=background_tasks)
    except HTTPException:
        raise
    return {"triggered": result.get("triggered", False), "reason": result.get("reason", ""), "task_id": result.get("task_id")}


@router.post("/run")
def run_now(body: RunRequest, background_tasks: BackgroundTasks):
    if not body.confirm_registration:
        raise HTTPException(400, "补号可能产生邮箱费用（不会接手机短信），请明确确认")
    return enqueue(trigger="manual", background_tasks=background_tasks)
