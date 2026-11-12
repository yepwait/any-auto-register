set -eu
# GET only against the already configured remote; no quota refresh, sync, upload,
# registration, SMS or config writes. Never print credentials or account rows.
docker exec -i any-auto-register python - <<'PY'
import json
from services.chatgpt_web_pool import WebPoolSettings,WebPoolClient
settings=WebPoolSettings.from_store()
value=WebPoolClient(settings).snapshot()
print('REMOTE_POOL_READONLY_ADAPTER_OK',json.dumps({k:value.get(k) for k in ('protocol','total_accounts','normal_accounts','available_accounts','remaining_quota','known_remaining_quota','quota_known','unknown_quota_accounts','unlimited_accounts','limited_accounts','abnormal_accounts','disabled_accounts')},ensure_ascii=False))
PY
