set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T132254Z
BACKUP="$BASE/backups/$RELEASE-preflight2"
[ ! -e "$BACKUP" ] || { echo 'Preflight backup path exists; refusing overwrite'; exit 1; }
mkdir -m 700 "$BACKUP" "$BACKUP/preflight-data"
cp -p "$BASE/docker-compose.yml" "$BACKUP/original-compose.yml"
readlink -f "$BASE/src" > "$BACKUP/original-source-path.txt"
docker inspect any-auto-register --format '{{.Image}}' > "$BACKUP/original-image-id.txt"
# Live-safe SQLite backup; the production database and keys are never edited here.
# Host Python is older; use the verified container Python's SQLite backup API.
docker exec -i any-auto-register python - /runtime/account_manager.db /runtime/resource-preflight-backup.sqlite <<'PY'
import sqlite3,sys
from pathlib import Path
if Path(sys.argv[2]).exists(): raise SystemExit('Refusing to overwrite backup')
source=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)
target=sqlite3.connect(sys.argv[2])
source.backup(target)
assert target.execute('PRAGMA quick_check').fetchone()[0]=='ok'
print('PREFLIGHT_DB_BACKUP_OK')
source.close();target.close()
PY
cp -p "$BASE/data/resource-preflight-backup.sqlite" "$BACKUP/preflight-data/account_manager.db"
[ ! -d "$BASE/data/.secrets" ] || cp -a "$BASE/data/.secrets" "$BACKUP/preflight-data/.secrets"
# Save a legacy in-image card store too, without exposing any values.
if docker exec any-auto-register test -f /app/platforms/chatgpt/payment_channels/cards.db; then
  docker cp any-auto-register:/app/platforms/chatgpt/payment_channels/cards.db "$BACKUP/legacy-cards.db"
  chmod 600 "$BACKUP/legacy-cards.db"
fi
docker inspect any-auto-register --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' > "$BACKUP/original-network.txt"
docker run -d --name any-auto-register-resource-preflight --init --network none --memory 768m --memory-swap 1g --cpus 0.5 \
  -e APP_ENABLE_SOLVER=0 -e APP_ENABLE_SCHEDULER=0 -e APP_TASK_WORKERS=1 \
  -e APP_RUNTIME_DIR=/runtime -e DATABASE_URL=sqlite:////runtime/account_manager.db \
  -e CREDENTIAL_ENCRYPTION_KEY_FILE=/runtime/.secrets/credential_key \
  -v "$BACKUP/preflight-data:/runtime" "any-auto-register:$RELEASE"
# Bounded health wait. It only GETs the isolated app, never registers or calls providers.
python3 - <<'PY'
import subprocess,time
for attempt in range(30):
    result=subprocess.run(['docker','exec','any-auto-register-resource-preflight','python','-c',
        'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/api/auth/status",timeout=2); print(r.status)'],capture_output=True,text=True)
    if result.returncode==0 and result.stdout.strip()=='200':
        print('ISOLATED_PREFLIGHT_HEALTH_OK');break
    time.sleep(1)
else:
    raise SystemExit('Preflight health failed; production untouched')
PY
printf '\n--- Preflight memory ---\n'
docker stats --no-stream any-auto-register-resource-preflight
printf '\n--- Isolated process list ---\n'
docker top any-auto-register-resource-preflight -eo pid,ppid,rss,comm
printf '\n--- Current app Caddy hostnames/upstreams only ---\n'
python3 - <<'PY'
import subprocess,json
p=subprocess.run(['docker','exec','caddy','caddy','adapt','--config','/etc/caddy/Caddyfile','--adapter','caddyfile'],capture_output=True,text=True)
if p.returncode==0:
    try:
        data=json.loads(p.stdout)
        def visit(node,hosts=()):
            if isinstance(node,dict):
                match=node.get('match') or []
                for item in match:
                    if isinstance(item,dict) and item.get('host'):hosts=tuple(item['host'])
                up=node.get('upstreams') or []
                for item in up:
                    dial=str(item.get('dial') or '')
                    if 'any-auto-register' in dial or ':18000' in dial: print(json.dumps({'hosts':hosts,'upstream':dial}))
                for value in node.values():visit(value,hosts)
            elif isinstance(node,list):
                for value in node:visit(value,hosts)
        visit(data)
    except Exception as exc: print('Caddy route parse:',type(exc).__name__)
else:print('Caddy adapter route not available; use local upstream health')
PY
