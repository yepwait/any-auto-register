"""Read-only, sanitized inspection intended to run inside the app container."""
import json
import os
import sqlite3
from pathlib import Path

path = os.getenv("DATABASE_URL", "sqlite:////runtime/account_manager.db").removeprefix("sqlite:///")
if not Path(path).is_file():
    path = "/app/account_manager.db"
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
result = {"database_path": str(Path(path).resolve()), "database_bytes": Path(path).stat().st_size}
for table in ("accounts", "task_runs", "task_logs"):
    if table in tables:
        result[table + "_count"] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
if "task_runs" in tables:
    result["active_tasks"] = [dict(row) for row in connection.execute("SELECT id,platform,source,status,total,progress,success,created_at FROM task_runs WHERE status IN ('pending','running') LIMIT 30")]
    result["stored_log_bytes"] = connection.execute("SELECT coalesce(sum(length(logs_json)),0) FROM task_runs").fetchone()[0]
if "configs" in tables:
    safe_keys = ("inspection_enabled", "inspection_concurrency", "inspection_interval_minutes", "sms_stock_enabled", "sms_stock_interval_minutes", "chatgpt_web_pool_enabled", "chatgpt_web_pool_interval_minutes", "cpa_cleanup_enabled", "cpa_cleanup_concurrency", "default_executor", "default_captcha_solver")
    result["resource_settings"] = dict(connection.execute("SELECT key,value FROM configs WHERE key IN (%s)" % ",".join("?" for _ in safe_keys), safe_keys))
connection.close()
print(json.dumps(result, ensure_ascii=False, default=str))
