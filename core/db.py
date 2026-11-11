"""数据库模型 - SQLite via SQLModel"""
from datetime import datetime, timezone
import os
import secrets
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine, Session, select
import json


def _utcnow():
    return datetime.now(timezone.utc)


def new_alias_share_token() -> str:
    """128 位随机串，够长到不能枚举，又短到能塞进一行链接里。"""
    return secrets.token_urlsafe(16)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///account_manager.db")
engine = create_engine(DATABASE_URL)


class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    email: str = Field(index=True)
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: str = "registered"
    trial_end_time: int = 0
    cashier_url: str = ""
    extra_json: str = "{}"   # JSON 存储平台自定义字段
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_extra(self) -> dict:
        return json.loads(self.extra_json or "{}")

    def set_extra(self, d: dict):
        self.extra_json = json.dumps(d, ensure_ascii=False)


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str
    email: str
    status: str        # success | failed
    error: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class TaskRunModel(SQLModel, table=True):
    __tablename__ = "task_runs"

    id: str = Field(primary_key=True)
    platform: str = Field(index=True)
    source: str = Field(default="manual", index=True)
    status: str = Field(default="pending", index=True)
    total: int = 0
    progress: str = "0/0"
    success: int = 0
    registered: int = 0
    skipped: int = 0
    error: str = ""
    meta_json: str = "{}"
    logs_json: str = "[]"  # legacy rows only; new log lines live in task_run_logs
    log_count: int = 0
    error_count: int = 0
    logs_version: int = 0
    errors_json: str = "[]"
    cashier_urls_json: str = "[]"
    control_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class TaskRunLogModel(SQLModel, table=True):
    """Append-only log lines; legacy task_runs.logs_json stays readable."""
    __tablename__ = "task_run_logs"

    task_id: str = Field(primary_key=True)
    sequence: int = Field(primary_key=True)
    line: str
    created_at: datetime = Field(default_factory=_utcnow)


class OutlookAccountModel(SQLModel, table=True):
    __tablename__ = "outlook_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, sa_column_kwargs={"unique": True})
    password: str
    client_id: str = ""
    refresh_token: str = ""
    account_type: str = "microsoft_oauth"
    mailapi_url: str = ""
    enabled: bool = True
    status: str = Field(default="available", index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_used: Optional[datetime] = None


class ICloudAccountModel(SQLModel, table=True):
    """iCloud 主号。Web Session 与 IMAP 凭据统一以 AES-256-GCM 密文保存。"""

    __tablename__ = "icloud_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, sa_column_kwargs={"unique": True})
    display_name: str = ""
    region: str = "global"
    status: str = "active"
    enabled: bool = True
    credentials_cipher: str = ""
    sync_error: str = ""
    last_sync_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ICloudAliasModel(SQLModel, table=True):
    """从主号生成或同步得到的 Hide My Email 隐私邮箱。"""

    __tablename__ = "icloud_aliases"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="icloud_accounts.id")
    address: str = Field(index=True, sa_column_kwargs={"unique": True})
    label: str = ""
    note: str = ""
    status: str = "active"
    provider_id: str = ""
    # 免登录查看最新邮件的凭证，链接本身就是权限，所以必须是猜不出来的随机串
    share_token: str = Field(default_factory=lambda: new_alias_share_token(), index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ProxyModel(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True)
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None


def save_account(account) -> 'AccountModel':
    """从 base_platform.Account 存入数据库（同平台同邮箱则更新）"""
    with Session(engine) as session:
        existing = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == account.platform)
            .where(AccountModel.email == account.email)
        ).first()
        if existing:
            existing.password = account.password
            existing.user_id = account.user_id or ""
            existing.region = account.region or ""
            existing.token = account.token or ""
            existing.status = account.status.value
            existing.extra_json = json.dumps(account.extra or {}, ensure_ascii=False)
            existing.cashier_url = (account.extra or {}).get("cashier_url", "")
            existing.updated_at = _utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        m = AccountModel(
            platform=account.platform,
            email=account.email,
            password=account.password,
            user_id=account.user_id or "",
            region=account.region or "",
            token=account.token or "",
            status=account.status.value,
            extra_json=json.dumps(account.extra or {}, ensure_ascii=False),
            cashier_url=(account.extra or {}).get("cashier_url", ""),
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        return m


def _migrate_outlook_accounts_schema() -> None:
    if engine.url.get_backend_name() != "sqlite":
        return
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info('outlook_accounts')").fetchall()
        if not rows:
            return
        existing_columns = {str(row[1]) for row in rows}
        if "account_type" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN account_type TEXT DEFAULT 'microsoft_oauth'"
            )
        if "mailapi_url" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN mailapi_url TEXT DEFAULT ''"
            )
        if "status" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN status TEXT DEFAULT 'available'"
            )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET account_type = 'microsoft_oauth' WHERE account_type IS NULL OR TRIM(account_type) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET mailapi_url = '' WHERE mailapi_url IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET status = 'available' WHERE status IS NULL OR TRIM(status) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET status = 'used' "
            "WHERE EXISTS (SELECT 1 FROM accounts "
            "WHERE lower(accounts.email) = lower(outlook_accounts.email)) "
            "AND status = 'available'"
        )


def _migrate_icloud_aliases_schema() -> None:
    if engine.url.get_backend_name() == "sqlite":
        with engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info('icloud_aliases')").fetchall()
            if not rows:
                return
            if "share_token" not in {str(row[1]) for row in rows}:
                conn.exec_driver_sql(
                    "ALTER TABLE icloud_aliases ADD COLUMN share_token TEXT DEFAULT ''"
                )

    # 建表之前就存在的隐私邮箱没有 token，逐行补一个（不能用一条 UPDATE，
    # 每行得是不同的随机值）
    with Session(engine) as session:
        pending = session.exec(
            select(ICloudAliasModel).where(
                (ICloudAliasModel.share_token == None)  # noqa: E711 - SQL 里要 IS NULL
                | (ICloudAliasModel.share_token == "")
            )
        ).all()
        for row in pending:
            row.share_token = new_alias_share_token()
            session.add(row)
        if pending:
            session.commit()


def _migrate_task_log_storage() -> None:
    """Additive migration; old logs remain intact for backward reads/rollback."""
    if engine.url.get_backend_name() != "sqlite":
        return
    with engine.begin() as connection:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info('task_runs')")}
        for name in ("log_count", "error_count", "logs_version"):
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE task_runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        # Populate only counters, leaving all legacy JSON unchanged for rollback.
        # JSON1 is available on supported SQLite; tolerate older builds gracefully.
        try:
            connection.exec_driver_sql("UPDATE task_runs SET log_count=json_array_length(logs_json) WHERE logs_version=0 AND log_count=0 AND json_valid(logs_json)")
            connection.exec_driver_sql("UPDATE task_runs SET error_count=json_array_length(errors_json) WHERE error_count=0 AND json_valid(errors_json)")
        except Exception:
            pass
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_task_runs_source_created ON task_runs(source, created_at)")
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_task_runs_status_created ON task_runs(status, created_at)")


def init_db():
    SQLModel.metadata.create_all(engine)
    _migrate_task_log_storage()
    _migrate_outlook_accounts_schema()
    _migrate_icloud_aliases_schema()


def get_session():
    with Session(engine) as session:
        yield session
