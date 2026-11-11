from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel
from core.db import AccountModel, get_session
from services.account_export import (
    DEFAULT_EXPORT_FORMAT,
    export_filename,
    list_export_formats,
    render_accounts,
)
from services.chatgpt_account_state import filter_accounts_by_plus_status, filter_accounts_by_sms_verified
from typing import Optional
from datetime import datetime, timezone
import io, csv, json, logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]


class BatchDeleteRequest(BaseModel):
    ids: list[int]


class ExportTextRequest(BaseModel):
    """按格式导出：给了 ``account_ids`` 就只导这些，否则导当前筛选的全部。"""

    format: str = DEFAULT_EXPORT_FORMAT
    platform: str = ""
    account_ids: list[int] = []
    status: str = ""
    email: str = ""
    plus_status: str = ""
    sms_verified: str = ""
    created_at_start: Optional[datetime] = None
    created_at_end: Optional[datetime] = None


def _filtered_accounts(
    session: Session,
    *,
    platform: str = "",
    status: str = "",
    email: str = "",
    plus_status: str = "",
    sms_verified: str = "",
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
) -> list[AccountModel]:
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    if email:
        q = q.where(AccountModel.email.contains(email))
    if created_at_start:
        q = q.where(AccountModel.created_at >= created_at_start)
    if created_at_end:
        q = q.where(AccountModel.created_at <= created_at_end)
    rows = list(session.exec(q).all())
    if plus_status:
        rows = list(filter_accounts_by_plus_status(rows, plus_status))
    if sms_verified:
        rows = list(filter_accounts_by_sms_verified(rows, sms_verified))
    return rows


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    plus_status: Optional[str] = None,
    sms_verified: Optional[str] = None,
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
    session: Session = Depends(get_session),
):
    # Plus 试用状态存在 extra_json 里，SQL 筛不动，只能取出来再过一遍。
    # 反正这个接口本来就要把整个结果集读出来算 total。
    rows = _filtered_accounts(
        session,
        platform=platform or "",
        status=status or "",
        email=email or "",
        plus_status=plus_status or "",
        sms_verified=sms_verified or "",
        created_at_start=created_at_start,
        created_at_end=created_at_end,
    )
    total = len(rows)
    start = max(page - 1, 0) * page_size
    return {"total": total, "page": page, "items": rows[start : start + page_size]}


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    accounts = session.exec(select(AccountModel)).all()
    platforms: dict = {}
    statuses: dict = {}
    for acc in accounts:
        platforms[acc.platform] = platforms.get(acc.platform, 0) + 1
        statuses[acc.status] = statuses.get(acc.status, 0) + 1
    return {"total": len(accounts), "by_platform": platforms, "by_status": statuses}


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    accounts = session.exec(q).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "email", "password", "user_id", "region",
                     "status", "cashier_url", "created_at"])
    for acc in accounts:
        writer.writerow([acc.platform, acc.email, acc.password, acc.user_id,
                         acc.region, acc.status, acc.cashier_url,
                         acc.created_at.strftime("%Y-%m-%d %H:%M:%S")])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.get("/export-formats")
def get_export_formats():
    """导出格式清单。前端下拉框直接渲染这个列表，新增格式不用改前端。"""
    return {"formats": list_export_formats(), "default": DEFAULT_EXPORT_FORMAT}


@router.post("/export-text")
def export_accounts_text(body: ExportTextRequest, session: Session = Depends(get_session)):
    if body.account_ids:
        ids = [int(v) for v in body.account_ids if int(v) > 0]
        rows = list(session.exec(select(AccountModel).where(AccountModel.id.in_(ids))).all())
        # 按前端给的顺序回填，勾选顺序就是导出顺序
        row_map = {row.id: row for row in rows}
        accounts = [row_map[i] for i in dict.fromkeys(ids) if i in row_map]
    else:
        accounts = _filtered_accounts(
            session,
            platform=body.platform,
            status=body.status,
            email=body.email,
            plus_status=body.plus_status,
            sms_verified=body.sms_verified,
            created_at_start=body.created_at_start,
            created_at_end=body.created_at_end,
        )

    try:
        content = render_accounts(accounts, body.format)
    except KeyError:
        raise HTTPException(400, f"未知的导出格式: {body.format}")

    return {
        "format": body.format,
        "total": len(accounts),
        "lines": len([line for line in content.split("\n") if line]) if content else 0,
        "content": content,
        "filename": export_filename(body.platform, body.format),
    }


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入，每行格式: email password [extra]"""
    created = 0
    for line in body.lines:
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = parts[2] if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            extra = "{}"
        acc = AccountModel(platform=body.platform, email=email,
                           password=password, extra_json=extra)
        session.add(acc)
        created += 1
    session.commit()
    return {"created": created}


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
    session: Session = Depends(get_session)
):
    """批量删除账号"""
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    deleted_count = 0
    not_found_ids = []
    
    try:
        for account_id in body.ids:
            acc = session.get(AccountModel, account_id)
            if acc:
                session.delete(acc)
                deleted_count += 1
            else:
                not_found_ids.append(account_id)
        
        session.commit()
        logger.info(f"批量删除成功: {deleted_count} 个账号")
        
        return {
            "deleted": deleted_count,
            "not_found": not_found_ids,
            "total_requested": len(body.ids)
        }
    except Exception as e:
        session.rollback()
        logger.exception("批量删除失败")
        raise HTTPException(500, f"批量删除失败: {str(e)}")


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return acc


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        acc.status = body.status
    if body.token is not None:
        acc.token = body.token
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    session.delete(acc)
    session.commit()
    return {"ok": True}


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import engine
    from sqlmodel import Session
    with Session(engine) as s:
        acc = s.get(AccountModel, account_id)
    if acc:
        from core.base_platform import Account, RegisterConfig
        from core.registry import get
        try:
            PlatformCls = get(acc.platform)
            plugin = PlatformCls(config=RegisterConfig())
            obj = Account(platform=acc.platform, email=acc.email,
                         password=acc.password, user_id=acc.user_id,
                         region=acc.region, token=acc.token,
                         extra=json.loads(acc.extra_json or "{}"))
            valid = plugin.check_valid(obj)
            with Session(engine) as s:
                a = s.get(AccountModel, account_id)
                if a:
                    if a.platform != "chatgpt":
                        a.status = a.status if valid else "invalid"
                    a.updated_at = datetime.now(timezone.utc)
                    s.add(a)
                    s.commit()
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
