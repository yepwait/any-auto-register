set -eu
CONTAINER=chatgpt2api-yukkcat
VOLUME_DIR=/var/lib/docker/volumes/chatgpt2api-yukkcat_chatgpt2api-runtime/_data
FILE="$VOLUME_DIR/services/account_service.py"

echo "=== Patch present in running file ==="
grep -c '_fire_replenish_webhook' "$FILE"

echo "=== Write config (BOM-aware) ==="
docker exec -i $CONTAINER python3 - <<'PY'
import json, secrets
from pathlib import Path
cfg_path = Path('/app/config.json')
raw = cfg_path.read_bytes()
if raw.startswith(b'\xef\xbb\xbf'):
    raw = raw[3:]
cfg = json.loads(raw.decode('utf-8'))
existing = str(cfg.get('replenish_webhook_secret') or '').strip()
if not existing:
    existing = secrets.token_urlsafe(24)
    print('SECRET_GENERATED')
else:
    print('SECRET_KEPT')
cfg['replenish_webhook_url'] = 'http://any-auto-register:8000/api/chatgpt-web-pool/trigger'
cfg['replenish_quota_threshold'] = 2300
cfg['replenish_webhook_secret'] = existing
cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
Path('/tmp/replenish_secret.txt').write_text(existing)
print('webhook_url:', cfg['replenish_webhook_url'])
print('threshold:', cfg['replenish_quota_threshold'])
print('CONFIG_WRITTEN')
PY

echo "=== Sync secret to any-auto-register ==="
SECRET=$(docker exec $CONTAINER cat /tmp/replenish_secret.txt)
docker exec -i any-auto-register python3 - "$SECRET" <<'PY'
import sys, sqlite3
c = sqlite3.connect('/runtime/account_manager.db', timeout=15)
c.execute("insert into configs(key,value) values('chatgpt_web_pool_trigger_secret',?) on conflict(key) do update set value=excluded.value", (sys.argv[1],))
c.commit()
row = c.execute("select value from configs where key='chatgpt_web_pool_trigger_secret'").fetchone()
print('SECRET_STORED len=%d' % len(row[0]))
c.close()
PY

echo "=== E2E: correct secret ==="
SECRET2=$(docker exec $CONTAINER cat /tmp/replenish_secret.txt)
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18000/api/chatgpt-web-pool/trigger \
  -H 'Content-Type: application/json' -H "X-Replenish-Secret: $SECRET2" -d '{}')
body=$(curl -s -X POST http://127.0.0.1:18000/api/chatgpt-web-pool/trigger \
  -H 'Content-Type: application/json' -H "X-Replenish-Secret: $SECRET2" -d '{}')
echo "TRIGGER HTTP $code: $body"
[ "$code" = "200" ] || { echo E2E_FAIL; exit 1; }

echo "=== E2E: wrong secret rejected ==="
bad=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18000/api/chatgpt-web-pool/trigger \
  -H 'Content-Type: application/json' -H 'X-Replenish-Secret: wrong' -d '{}')
echo "WRONG SECRET HTTP $bad (expect 401)"
[ "$bad" = "401" ] || { echo WRONG_SECRET_NOT_BLOCKED; exit 1; }

echo "=== Final container status ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep $CONTAINER
echo "WEBHOOK_LINKUP_COMPLETE"
