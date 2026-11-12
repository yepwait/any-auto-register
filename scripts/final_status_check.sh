set -eu
echo "=== any-auto-register ==="
docker inspect any-auto-register --format 'Status={{.State.Status}} Restarts={{.RestartCount}}'
readlink -f /opt/any-auto-register/src
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/api/chatgpt-web-pool/trigger -X POST -H 'Content-Type: application/json' -d '{}')
echo "trigger endpoint (no secret): HTTP $code"

echo ""
echo "=== chatgpt2api (yukkcat) ==="
docker inspect chatgpt2api-yukkcat --format 'Status={{.State.Status}} Restarts={{.RestartCount}}'
grep -c '_fire_replenish_webhook' /var/lib/docker/volumes/chatgpt2api-yukkcat_chatgpt2api-runtime/_data/services/account_service.py && echo "patch present" || echo "patch missing"
docker exec -i chatgpt2api-yukkcat python3 - <<'PY'
import json
from pathlib import Path
raw = Path('/app/config.json').read_bytes()
if raw.startswith(b'\xef\xbb\xbf'):
    raw = raw[3:]
cfg = json.loads(raw.decode('utf-8'))
print('webhook_url:', cfg.get('replenish_webhook_url', '<not set>'))
print('threshold:', cfg.get('replenish_quota_threshold', '<not set>'))
print('secret_set:', bool(cfg.get('replenish_webhook_secret', '')))
PY

echo ""
echo "=== any-auto-register trigger secret ==="
docker exec -i any-auto-register python3 -c "
import sqlite3
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
row=c.execute(\"select value from configs where key='chatgpt_web_pool_trigger_secret'\").fetchone()
print('secret_configured:', bool(row and row[0]))
print('secret_len:', len(row[0]) if row and row[0] else 0)
c.close()
"

echo ""
echo "=== web pool enabled ==="
docker exec -i any-auto-register python3 -c "
import sqlite3
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
row=c.execute(\"select value from configs where key='chatgpt_web_pool_enabled'\").fetchone()
print('web_pool_enabled:', row[0] if row else 'not set')
c.close()
"
