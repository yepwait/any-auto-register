set -eu
echo '=== chatgpt2api container ==='
docker inspect chatgpt2api --format 'Image={{.Image}} Status={{.State.Status}}' 2>/dev/null || echo NOT_FOUND
docker inspect chatgpt2api --format '{{json .Mounts}}' 2>/dev/null
echo '=== patch check ==='
docker exec chatgpt2api grep -c '_fire_replenish_webhook' /app/services/account_service.py 2>/dev/null && echo PATCH_PRESENT || echo PATCH_GONE
echo '=== config fields ==='
docker exec -i chatgpt2api python3 - <<'PY'
import json
from pathlib import Path
cfg=json.loads(Path('/app/config.json').read_text())
print('webhook_url:',cfg.get('replenish_webhook_url','<not set>'))
print('threshold:',cfg.get('replenish_quota_threshold','<not set>'))
print('secret_set:',bool(cfg.get('replenish_webhook_secret','')))
PY
echo '=== backup check ==='
ls -la /opt/chatgpt2api/data/account_service.py.bak* 2>/dev/null || echo NO_BACKUP
echo '=== version ==='
docker exec chatgpt2api sh -c 'cat /app/VERSION 2>/dev/null || echo unknown'
echo '=== services layout ==='
docker exec chatgpt2api ls /app/services/ 2>/dev/null | sort
