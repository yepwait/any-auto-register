set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T144935Z
BACKUP="$BASE/backups/$RELEASE"
OLD=$(readlink -f "$BASE/src")
[ ! -e "$BACKUP" ] || { echo 'Backup exists; refusing overwrite'; exit 1; }
mkdir -m 700 "$BACKUP"
cp -p "$BASE/docker-compose.yml" "$BACKUP/docker-compose.yml"
printf '%s\n' "$OLD" > "$BACKUP/source-path.txt"
docker inspect any-auto-register --format '{{.Image}}' > "$BACKUP/image-id.txt"
# Check active tasks
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
active=c.execute("select id,source,status from task_runs where status in ('pending','running')").fetchall()
if active:print('ACTIVE_TASKS',json.dumps(active));raise SystemExit(2)
c.close();print('IDLE_OK')
PY
# Build updated compose (image tag only change)
python3 - "$BASE/docker-compose.yml" "$RELEASE" <<'PY'
import sys,re
from pathlib import Path
p,release=Path(sys.argv[1]),sys.argv[2]
text,n=re.subn(r'(?m)^(\s*image:)\s*any-auto-register:[^\s]+',r'\1 any-auto-register:'+release,p.read_text())
assert n==1,'Expected exactly one image: line'
(p.parent/('docker-compose.yml.next-'+release)).write_text(text)
print('COMPOSE_OK')
PY
# Quiescent stop
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" stop -t 90 app
tar -czf "$BACKUP/runtime-data.tar.gz" -C "$BASE" data
mv -f "$BASE/docker-compose.yml.next-$RELEASE" "$BASE/docker-compose.yml"
ln -s "$BASE/releases/$RELEASE/payload" "$BASE/src.next-$RELEASE"
mv -Tf "$BASE/src.next-$RELEASE" "$BASE/src"
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" up -d --no-build app
# Health check
python3 - <<'PY'
import subprocess,time
for attempt in range(45):
 p=subprocess.run(['curl','--max-time','2','-s','-o','/dev/null','-w','%{http_code}','http://127.0.0.1:18000/api/auth/status'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 if p.returncode==0 and p.stdout=='200':print('HEALTH_OK');break
 time.sleep(1)
else:raise SystemExit('Health check failed')
PY
# Verify trigger endpoint exists (no secret configured = disabled error 200, not 404)
code=$(curl -sf -o /dev/null -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:18000/api/chatgpt-web-pool/trigger || echo "000")
printf 'POST /api/chatgpt-web-pool/trigger HTTP %s\n' "$code"
[ "$code" = "200" ] || [ "$code" = "401" ] || { echo 'Trigger endpoint missing'; exit 1; }
echo 'TRIGGER_ENDPOINT_PRESENT'
docker stats --no-stream any-auto-register
printf 'ANY_AUTO_REGISTER_DEPLOY_OK\n'
