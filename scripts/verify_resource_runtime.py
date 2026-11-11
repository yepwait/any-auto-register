"""Read-only release verification inside an isolated or production container.

Auth token is created only in memory from the already configured secret. It is
not logged or written. Only GET requests and read-only database checks run.
"""
import json
import os
import sqlite3
import time
import requests
from pathlib import Path

path = "/runtime/account_manager.db"
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
password = connection.execute("SELECT value FROM configs WHERE key='auth_password_hash'").fetchone()
secret = connection.execute("SELECT value FROM configs WHERE key='auth_jwt_secret'").fetchone()
headers = {}
if password and password[0]:
    if not secret or not secret[0]:
        raise SystemExit("Configured admin auth has no saved signing secret; refusing unauthenticated test")
    import base64
    import hashlib
    import hmac
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    h, p = encode({"alg": "HS256", "typ": "JWT"}), encode({"sub": "admin", "exp": int(time.time()) + 180, "iat": int(time.time())})
    key = os.getenv("APP_JWT_SECRET") or secret[0]
    sig = base64.urlsafe_b64encode(hmac.new(key.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    headers["Authorization"] = f"Bearer {h}.{p}.{sig}"

session = requests.Session()
session.trust_env = False
base = "http://127.0.0.1:8000"
result = {"counts": {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in ("accounts", "task_runs", "task_logs")}}
for endpoint in ("/api/auth/status", "/api/tasks/resources", "/api/tasks/page?status=active&limit=20", "/api/tasks/page?status=finished&limit=20", "/api/inspection/status", "/api/sms-stock/status", "/api/chatgpt-web-pool/status"):
    before = time.monotonic()
    response = session.get(base + endpoint, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    result[endpoint] = {"http": response.status_code, "bytes": len(response.content), "elapsed_ms": round((time.monotonic() - before) * 1000, 1)}
    if endpoint.endswith("/resources"):
        result["resources"] = data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        assert all(not item.get("logs") and not item.get("errors") for item in data["items"]), "summary contains heavy logs"
        result[endpoint]["total"] = data.get("total")

# Verify one legacy log detail can still be paged; never expose content.
legacy = connection.execute("SELECT id,log_count FROM task_runs WHERE logs_version=0 AND log_count>0 ORDER BY created_at DESC LIMIT 1").fetchone()
if legacy:
    response = session.get(base + f"/api/tasks/{legacy['id']}/logs?since=0&limit=5", headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    assert data["logs"] and data["next_since"] > 0
    result["legacy_logs"] = {"http": response.status_code, "returned_lines": len(data["logs"]), "log_count": data["log_count"]}
# Credential continuity without displaying ciphertext/plaintext.
key_path = Path(os.getenv("CREDENTIAL_ENCRYPTION_KEY_FILE", "/runtime/.secrets/credential_key"))
result["credential_key_present"] = key_path.is_file()
result["active_task_count"] = connection.execute("SELECT count(*) FROM task_runs WHERE status IN ('pending','running')").fetchone()[0]
connection.close()
print(json.dumps(result, ensure_ascii=False))
