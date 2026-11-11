set -eu
# Backup original
docker cp chatgpt2api:/app/services/account_service.py /opt/chatgpt2api/data/account_service.py.bak-webhook-20261111
printf 'BACKUP_OK\n'
# Inject patched file
docker cp /tmp/account_service_patched.py chatgpt2api:/app/services/account_service.py
# Verify patch landed
docker exec chatgpt2api grep -c '_fire_replenish_webhook' /app/services/account_service.py
printf 'PATCH_APPLIED\n'
# Restart to load new code (config + data mounted externally, no data loss)
docker restart chatgpt2api
# Health check
python3 - <<'PY'
import subprocess,time
for attempt in range(45):
 p=subprocess.run(['curl','--max-time','3','-s','-o','/dev/null','-w','%{http_code}','http://127.0.0.1:3000/health?format=json'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 if p.returncode==0 and p.stdout=='200':print('CHATGPT2API_HEALTH_OK');break
 time.sleep(1)
else:raise SystemExit('chatgpt2api health check failed')
PY
# Verify webhook fields are readable from config
docker exec -i chatgpt2api python3 - <<'PY'
import json
from pathlib import Path
cfg=json.loads(Path('/app/config.json').read_text())
print('replenish_webhook_url:',repr(cfg.get('replenish_webhook_url','')))
print('replenish_quota_threshold:',cfg.get('replenish_quota_threshold',0))
print('replenish_webhook_secret:','<set>' if cfg.get('replenish_webhook_secret') else '<not set>')
print('CONFIG_READABLE')
PY
printf 'CHATGPT2API_PATCH_DEPLOY_OK\n'
