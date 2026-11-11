set -eu
# 1. Write replenish config to chatgpt2api/config.json atomically
docker exec -i chatgpt2api python3 - <<'PY'
import json,secrets
from pathlib import Path
cfg_path=Path('/app/config.json')
cfg=json.loads(cfg_path.read_text())
# Generate a secret if not already set
existing_secret=str(cfg.get('replenish_webhook_secret') or '').strip()
if not existing_secret:
    existing_secret=secrets.token_urlsafe(24)
    print('GENERATED_SECRET')
else:
    print('EXISTING_SECRET_KEPT')
# any-auto-register is on the same Docker network under container name any-auto-register
cfg['replenish_webhook_url']='http://any-auto-register:8000/api/chatgpt-web-pool/trigger'
# Threshold: trigger when total quota drops below 2300 (target 2000 + 300 buffer)
cfg['replenish_quota_threshold']=2300
cfg['replenish_webhook_secret']=existing_secret
cfg_path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n')
print('replenish_webhook_url:',cfg['replenish_webhook_url'])
print('replenish_quota_threshold:',cfg['replenish_quota_threshold'])
print('replenish_webhook_secret_len:',len(existing_secret))
# Save secret to a temp file so we can read it back for configuring the other side
Path('/tmp/replenish_secret.txt').write_text(existing_secret)
print('CONFIG_WRITTEN')
PY
# 2. Store the same secret in any-auto-register's DB
SECRET=$(docker exec chatgpt2api cat /tmp/replenish_secret.txt)
docker exec -i any-auto-register python3 - "$SECRET" <<'PY'
import sys,sqlite3
c=sqlite3.connect('/runtime/account_manager.db',timeout=15)
secret=sys.argv[1]
c.execute("insert into configs(key,value) values('chatgpt_web_pool_trigger_secret',?) on conflict(key) do update set value=excluded.value",(secret,))
c.commit()
verify=c.execute("select value from configs where key='chatgpt_web_pool_trigger_secret'").fetchone()
print('TRIGGER_SECRET_STORED len=%d'%len(verify[0]))
c.close()
PY
# 3. Reload chatgpt2api config (restart)
docker restart chatgpt2api
python3 - <<'PY'
import subprocess,time
for i in range(45):
 p=subprocess.run(['curl','--max-time','3','-s','-o','/dev/null','-w','%{http_code}','http://127.0.0.1:3000/health?format=json'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 if p.returncode==0 and p.stdout=='200':print('CHATGPT2API_HEALTH_OK');break
 time.sleep(1)
else:raise SystemExit('chatgpt2api health check failed')
PY
# 4. End-to-end test: call trigger with the secret and verify 200 + response
SECRET2=$(docker exec chatgpt2api cat /tmp/replenish_secret.txt)
result=$(curl -s -X POST http://127.0.0.1:18000/api/chatgpt-web-pool/trigger \
  -H 'Content-Type: application/json' \
  -H "X-Replenish-Secret: $SECRET2" \
  -d '{}')
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18000/api/chatgpt-web-pool/trigger \
  -H 'Content-Type: application/json' \
  -H "X-Replenish-Secret: $SECRET2" \
  -d '{}')
printf 'TRIGGER HTTP %s: %s\n' "$code" "$result"
[ "$code" = "200" ] || { echo 'Trigger call failed'; exit 1; }
# 5. Verify wrong secret is rejected
bad_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18000/api/chatgpt-web-pool/trigger \
  -H 'Content-Type: application/json' \
  -H 'X-Replenish-Secret: wrong-secret' \
  -d '{}')
printf 'WRONG SECRET HTTP %s (expect 401)\n' "$bad_code"
[ "$bad_code" = "401" ] || { echo 'Wrong secret not rejected'; exit 1; }
# 6. Verify chatgpt2api config has the webhook URL
docker exec chatgpt2api python3 -c "
import json
from pathlib import Path
cfg=json.loads(Path('/app/config.json').read_text())
print('webhook_url:',cfg.get('replenish_webhook_url'))
print('threshold:',cfg.get('replenish_quota_threshold'))
print('secret_configured:', bool(cfg.get('replenish_webhook_secret')))
"
printf '\nE2E_WEBHOOK_LINKUP_VERIFIED\n'
