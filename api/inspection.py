"""巡检中心 API：配置、手动执行和最近运行状态。"""

from __future__ import annotations

from typing import Any, Optional
import threading
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from core.db import TaskRunModel, engine
from api.tasks import (
    _list_persisted_tasks,
    _log,
    _persist_task_snapshot,
    _run_account_batch_task,
    _task_store,
    _finalize_orphan_tasks,
    has_active_register_task,
    chatgpt_enqueue_lock,
)
from core.task_runtime import AttemptResult
from services.inspection_service import (
    InspectionSettings,
    inspect_account,
    select_account_ids,
    update_settings,
)

router = APIRouter(prefix="/inspection", tags=["inspection"])
_enqueue_lock = chatgpt_enqueue_lock


class InspectionConfigUpdate(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)


class InspectionRunRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    statuses: list[str] | str | None = None
    email_filter: Optional[str] = None
    max_accounts: Optional[int] = Field(default=None, ge=0, le=5000)
    concurrency: Optional[int] = None
    delay_seconds: Optional[float] = None
    probe_enabled: Optional[bool] = None
    backfill_rt_enabled: Optional[bool] = None
    backfill_allow_login: Optional[bool] = None
    only_missing_rt: Optional[bool] = None
    only_sms_verified: Optional[bool] = None
    upload_cpa: Optional[bool] = None
    upload_sub2api: Optional[bool] = None
    chat2api_enabled: Optional[bool] = None
    chat2api_protocol: Optional[str] = None
    proxy: Optional[str] = None


def _settings_overrides(req: InspectionRunRequest) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "email_filter": req.email_filter,
            "max_accounts": req.max_accounts,
            "concurrency": req.concurrency,
            "delay_seconds": req.delay_seconds,
            "probe_enabled": req.probe_enabled,
            "backfill_rt_enabled": req.backfill_rt_enabled,
            "backfill_allow_login": req.backfill_allow_login,
            "only_missing_rt": req.only_missing_rt,
            "only_sms_verified": req.only_sms_verified,
            "upload_cpa": req.upload_cpa,
            "upload_sub2api": req.upload_sub2api,
            "chat2api_enabled": req.chat2api_enabled,
            "chat2api_protocol": req.chat2api_protocol,
        }.items()
        if value is not None
    }


def _has_active_inspection_task() -> bool:
    _finalize_orphan_tasks()
    if any(has_active_register_task(platform="chatgpt", source=source) for source in ("inspection", "sms_stock", "chatgpt_web_pool")):
        return True
    with Session(engine) as session:
        row = session.exec(
            select(TaskRunModel)
            .where(TaskRunModel.source == "inspection")
            .where(TaskRunModel.status.in_(["pending", "running"]))
            .limit(1)
        ).first()
    return row is not None


def _validate_run_settings(settings: InspectionSettings) -> None:
    if settings.chat2api_enabled:
        from services.inspection_service import _validate_webhook_url
        try:
            _validate_webhook_url(settings.chat2api_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if settings.upload_cpa:
        from core.config_store import config_store

        if not str(config_store.get("cpa_api_url", "") or "").strip():
            raise HTTPException(400, "已开启 CPA 上传，但没有配置 CPA API URL")
    if settings.upload_sub2api:
        from core.config_store import config_store

        if not str(config_store.get("sub2api_api_url", "") or "").strip():
            raise HTTPException(400, "已开启 Sub2API 上传，但没有配置 Sub2API API URL")
        if not str(config_store.get("sub2api_api_key", "") or "").strip():
            raise HTTPException(400, "已开启 Sub2API 上传，但没有配置 Sub2API API Key")
    if not any(
        (
            settings.probe_enabled,
            settings.backfill_rt_enabled,
            settings.upload_cpa,
            settings.upload_sub2api,
            settings.chat2api_enabled,
        )
    ):
        raise HTTPException(400, "至少启用测活、补 RT 或一种上传动作")


def _create_inspection_task(
    *,
    account_ids: list[int],
    missing_ids: list[int],
    settings: InspectionSettings,
    proxy: Optional[str],
    trigger: str,
) -> str:
    task_id = f"inspection_{int(time.time() * 1000)}"
    _task_store.create(
        task_id,
        platform="chatgpt",
        total=len(account_ids),
        source="inspection",
        meta={
            "kind": "inspection",
            "trigger": trigger,
            "settings": settings.public_dict(),
            "missing_ids": missing_ids,
            "proxy_configured": bool(proxy),
        },
    )
    _persist_task_snapshot(task_id)
    _log(task_id, f"待巡检 ChatGPT 账号 {len(account_ids)} 个")
    if missing_ids:
        _log(task_id, f"忽略不存在或不匹配的账号: {missing_ids}")
    return task_id


def _run_inspection(
    task_id: str,
    account_ids: list[int],
    settings: InspectionSettings,
    proxy: Optional[str],
) -> None:
    def _handle(*, account_id, fields, proxy: Optional[str], control, attempt_id) -> AttemptResult:
        email = str(fields.get("email") or "")
        _log(task_id, f"开始巡检 {email or f'账号 #{account_id}'}")
        return inspect_account(
            account_id=account_id,
            fields=fields,
            settings=settings,
            proxy=proxy,
            log_fn=lambda msg: _log(task_id, f"  {msg}"),
            task_control=control,
            attempt_id=attempt_id,
        )

    _run_account_batch_task(
        task_id,
        account_ids,
        label="巡检",
        concurrency=settings.concurrency,
        delay_seconds=settings.delay_seconds,
        proxy=proxy,
        handle_account=_handle,
    )


def enqueue_inspection_task(
    *,
    settings: InspectionSettings | None = None,
    account_ids: list[int] | None = None,
    statuses: list[str] | str | None = None,
    email_filter: str | None = None,
    max_accounts: int | None = None,
    proxy: Optional[str] = None,
    trigger: str = "manual",
    background_tasks: BackgroundTasks | None = None,
    reject_empty: bool = False,
) -> dict[str, Any] | None:
    settings = settings or InspectionSettings.from_store()
    selected_statuses = statuses if statuses is not None else settings.account_statuses
    selected_email = settings.email_filter if email_filter is None else email_filter
    selected_max = settings.max_accounts if max_accounts is None else max_accounts
    ids, missing_ids = select_account_ids(
        account_ids=account_ids,
        statuses=selected_statuses,
        email_filter=selected_email,
        max_accounts=max(0, int(selected_max or 0)),
        only_sms_verified=settings.only_sms_verified,
    )
    if not ids:
        if reject_empty:
            raise HTTPException(400, "没有匹配到可巡检的 ChatGPT 账号")
        return None

    _validate_run_settings(settings)
    with _enqueue_lock:
        if _has_active_inspection_task():
            if reject_empty:
                raise HTTPException(409, "已有巡检或接码补库任务正在运行")
            return None

        from api.tasks import ensure_task_capacity
        ensure_task_capacity()
        task_id = _create_inspection_task(
            account_ids=ids,
            missing_ids=missing_ids,
            settings=settings,
            proxy=proxy,
            trigger=trigger,
        )
    if background_tasks is None:
        import threading

        threading.Thread(
            target=_run_inspection,
            args=(task_id, ids, settings, proxy),
            daemon=True,
        ).start()
    else:
        background_tasks.add_task(_run_inspection, task_id, ids, settings, proxy)
    return {"task_id": task_id, "total": len(ids), "missing_ids": missing_ids}


@router.get("/config")
def get_inspection_config():
    return InspectionSettings.from_store().public_dict()


@router.put("/config")
def put_inspection_config(body: InspectionConfigUpdate):
    current = InspectionSettings.from_store()
    candidate = current.with_overrides({
        key.removeprefix("inspection_"): value
        for key, value in (body.data or {}).items()
        if key.startswith("inspection_")
    })
    # 只有真正开启自动巡检时才校验执行动作和上传目标；关闭巡检时允许
    # 同时清空所有动作开关，便于用户保存一份“已关闭”的配置。
    if candidate.enabled:
        _validate_run_settings(candidate)
    settings = update_settings(body.data)
    if settings.enabled:
        # 保存后立即唤醒调度器，避免用户开启自动巡检后还要等完整间隔。
        from core.scheduler import scheduler

        scheduler.trigger_inspection_now()
    return {"ok": True, "config": settings.public_dict()}


@router.get("/status")
def get_inspection_status():
    tasks = _list_persisted_tasks(source="inspection", limit=20)
    tasks.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    active = [task for task in tasks if task.get("status") in {"pending", "running"}]
    return {
        "enabled": InspectionSettings.from_store().enabled,
        "active": active[0] if active else None,
        "last_task": tasks[0] if tasks else None,
        "tasks": tasks[:20],
    }


@router.post("/run")
def run_inspection(body: InspectionRunRequest, background_tasks: BackgroundTasks):
    settings = InspectionSettings.from_store().with_overrides(_settings_overrides(body))
    statuses = body.statuses
    return enqueue_inspection_task(
        settings=settings,
        account_ids=body.account_ids,
        statuses=statuses,
        email_filter=body.email_filter,
        max_accounts=body.max_accounts,
        proxy=body.proxy,
        trigger="manual",
        background_tasks=background_tasks,
        reject_empty=True,
    )


def enqueue_scheduled_inspection() -> dict[str, Any] | None:
    """由 scheduler 调用；无匹配账号或已有任务时静默跳过。"""
    settings = InspectionSettings.from_store()
    if not settings.enabled:
        return None
    return enqueue_inspection_task(settings=settings, trigger="schedule")
