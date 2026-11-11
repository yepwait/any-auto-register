"""接码账号库存中心：配置、只读预览、付费操作显式确认及后台任务。"""
from __future__ import annotations

import threading
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from api import tasks
from core.config_store import config_store
from core.task_runtime import StopTaskRequested
from services import sms_stock_service as service

router = APIRouter(prefix="/sms-stock", tags=["sms-stock"])


class ConfigUpdate(BaseModel):
    config: service.SmsStockSettings
    confirm_cost: bool = False


class RunRequest(BaseModel):
    confirm_cost: bool = False


def _validate():
    try:
        service.validate_sms_config()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/config")
def get_config():
    return service.SmsStockSettings.from_store().model_dump()


@router.put("/config")
def put_config(body: ConfigUpdate):
    if body.config.enabled:
        if not body.confirm_cost:
            raise HTTPException(400, "开启自动补库会产生接码/邮箱费用，请明确确认")
        _validate()
    service.save_settings(body.config)
    if body.config.enabled:
        from core.scheduler import scheduler

        scheduler.trigger_sms_stock_now()
    # 关闭时阻止新动作，并向正在等待验证码的链路传递停止开关。
    if not body.config.enabled:
        with tasks.chatgpt_enqueue_lock:
            for task in tasks._task_store.list_snapshots():
                if task.get("source") == service.SOURCE and task["status"] in {"pending", "running"}:
                    tasks._task_store.request_stop(task["id"])
    return {"ok": True, "config": body.config.model_dump()}


@router.get("/status")
def get_status():
    settings = service.SmsStockSettings.from_store()
    history = tasks._list_persisted_tasks(source=service.SOURCE, limit=20)
    history.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    active = next((item for item in history if item["status"] in {"pending", "running"}), None)
    return {
        "enabled": settings.enabled,
        "inventory": service.inventory(settings),
        "active": active,
        "tasks": history[:20],
    }


def _register_one(parent_id: str, settings: service.SmsStockSettings, control) -> bool:
    """复用现有注册交付和邮箱快照持久化，不另写一套注册流程。"""
    request = tasks._prepare_register_request(tasks.RegisterTaskRequest(
        platform="chatgpt", count=1, concurrency=1, register_retry_times=0,
        executor_type="protocol",
        captcha_solver=config_store.get("default_captcha_solver", "yescaptcha"),
        extra={
            "chatgpt_register_flow": settings.register_flow,
            "chatgpt_registration_mode": "refresh_token",
            "chatgpt_require_rt_ready": "1",
        },
    ))
    child_id = f"sms_stock_register_{uuid4().hex}"
    tasks._create_task_record(child_id, request, service.REGISTER_SOURCE, {"parent_task_id": parent_id})
    tasks._task_store.append_meta_list(parent_id, "child_task_ids", child_id)
    tasks._persist_task_snapshot(parent_id)
    tasks._log(parent_id, f"[注册子任务] {child_id}")
    # 同步运行子任务、共享停止开关；不另起一个无法停止的注册线程。
    tasks._run_register(child_id, request, task_control=control)
    control.checkpoint()
    snapshot = tasks._task_store.snapshot(child_id)
    return snapshot.get("success", 0) > 0


def _run(task_id: str, settings: service.SmsStockSettings, trigger: str):
    control = tasks._task_store.control_for(task_id)
    tasks._task_store.mark_running(task_id)
    tasks._persist_task_snapshot(task_id)
    log = lambda text: tasks._log(task_id, text)
    result = service.StockRunResult()
    status = "done"

    def progress(value, phase):
        nonlocal result
        result = value
        tasks._task_store.update_counters(task_id, success=value.success, registered=value.attempts)
        tasks._task_store.set_progress(task_id, f"{value.attempts}/{settings.max_attempts_per_run}")
        tasks._task_store.update_meta(task_id, {"phase": phase, "attempts": value.attempts})
        tasks._persist_task_snapshot(task_id)

    try:
        result = service.replenish(
            settings, control=control, log=log,
            register=lambda config, switch: _register_one(task_id, config, switch),
            progress=progress,
            should_continue=lambda: trigger != "schedule" or service.SmsStockSettings.from_store().enabled,
        )
        if result.final_inventory < settings.min_accounts and result.errors:
            status = "failed"
    except StopTaskRequested:
        status = "stopped"
        result.reason = "补库已停止，不再启动新的注册或接码"
    except Exception as exc:
        status = "failed"
        result.reason = f"补库异常: {type(exc).__name__}"
        result.errors.append(result.reason)
    try:
        result.final_inventory = service.inventory(settings)["verified_accounts"]
    except Exception as exc:
        status = "failed"
        result.errors.append(f"最终库存统计失败: {type(exc).__name__}")
    log(f"[补库结束] {result.reason}；成功 {result.success}，跳过 {result.skipped}，失败 {len(result.errors)}，当前库存 {result.final_inventory}/{settings.min_accounts}")
    tasks._task_store.update_meta(task_id, {"reason": result.reason, "final_inventory": result.final_inventory})
    tasks._task_store.finish(
        task_id, status=status, success=result.success, registered=result.attempts,
        skipped=result.skipped, errors=result.errors,
    )
    tasks._persist_task_snapshot(task_id)
    tasks._task_store.cleanup()


def enqueue(*, trigger="schedule", background_tasks: BackgroundTasks | None = None):
    settings = service.SmsStockSettings.from_store()
    if trigger == "schedule" and not settings.enabled:
        return {"triggered": False, "reason": "自动补库未启用"}
    with tasks.chatgpt_enqueue_lock:
        # 复用应用的单进程任务模型；任何 GPT 任务在运行时都不抢同一账号或号源。
        if tasks.has_active_register_task(platform="chatgpt"):
            if trigger == "manual":
                raise HTTPException(409, "已有 GPT 任务正在执行，请结束后再补库")
            return {"triggered": False, "reason": "已有 GPT 任务正在执行"}
        stock = service.inventory(settings)
        if stock["missing_accounts"] == 0:
            return {"triggered": False, "reason": "接码账号库存已达标", "inventory": stock}
        if stock["daily_attempts_remaining"] == 0:
            return {"triggered": False, "reason": "已达到每日尝试上限（UTC）", "inventory": stock}
        _validate()
        tasks.ensure_task_capacity()
        task_id = f"sms_stock_{uuid4().hex}"
        tasks._task_store.create(
            task_id, platform="chatgpt", total=settings.max_attempts_per_run, source=service.SOURCE,
            meta={"kind": service.SOURCE, "trigger": trigger, "settings": settings.model_dump(), "initial_inventory": stock},
        )
        tasks._persist_task_snapshot(task_id)
        tasks._log(task_id, f"接码库存 {stock['verified_accounts']}/{settings.min_accounts}，优先处理已有未接码账号；每轮最多 {settings.max_attempts_per_run} 次尝试")
    if background_tasks is not None:
        background_tasks.add_task(_run, task_id, settings, trigger)
    else:
        threading.Thread(target=_run, args=(task_id, settings, trigger), daemon=True).start()
    return {"triggered": True, "task_id": task_id, "inventory": stock}


@router.post("/run")
def run_now(body: RunRequest, background_tasks: BackgroundTasks):
    if not body.confirm_cost:
        raise HTTPException(400, "立即补库可能产生接码/邮箱费用，请明确确认")
    return enqueue(trigger="manual", background_tasks=background_tasks)
