set -eu
CONTAINER=chatgpt2api-yukkcat
echo "=== version ==="
docker exec $CONTAINER sh -c 'cat /app/VERSION 2>/dev/null || cat /VERSION 2>/dev/null || echo unknown'
echo "=== source layout ==="
docker exec $CONTAINER ls /app/ 2>/dev/null | sort
docker exec $CONTAINER ls /app/services/ 2>/dev/null | sort
echo "=== config mounts ==="
docker inspect $CONTAINER --format '{{json .Mounts}}'
echo "=== config.json replenish fields ==="
docker exec -i $CONTAINER python3 - <<'PY'
import json
from pathlib import Path
for p in ['/app/config.json', '/data/config.json', '/config.json']:
  if Path(p).exists():
    cfg=json.loads(Path(p).read_text())
    print('config_path:',p)
    print('replenish_webhook_url:',cfg.get('replenish_webhook_url','<not set>'))
    print('replenish_quota_threshold:',cfg.get('replenish_quota_threshold','<not set>'))
    break
else:
  print('no config.json found')
PY
echo "=== quota decrement code ==="
docker exec $CONTAINER grep -rn 'quota.*-\|after_image\|mark_image\|webhook\|replenish' /app/services/ 2>/dev/null | grep -v '.pyc' | grep -v __pycache__ | head -40 || echo none
echo "=== account_service.py location ==="
docker exec $CONTAINER find /app -name 'account_service.py' 2>/dev/null || echo not_found
