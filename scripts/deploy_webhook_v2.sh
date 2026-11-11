set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T145604Z
ARCHIVE="$BASE/incoming/$RELEASE.tar.gz"
EXPECTED=1f74f1d016c8815dff627221602d2b0561edd733fa61a4612b1123e7f400bc9d
printf '%s  %s\n' "$EXPECTED" "$ARCHIVE" | sha256sum -c -
TARGET="$BASE/releases/$RELEASE"
[ ! -e "$TARGET" ] || { echo 'Target exists'; exit 1; }
mkdir -m 700 "$TARGET"
python3 - "$ARCHIVE" "$TARGET" <<'PY'
import tarfile,sys,json,hashlib
from pathlib import Path
archive,target=map(Path,sys.argv[1:])
with tarfile.open(str(archive)) as pack:
 for item in pack.getmembers():
  path=Path(item.name)
  if path.is_absolute() or '..' in path.parts or not item.isfile() or item.issym() or item.islnk():raise SystemExit('Unsafe archive item')
  if item.name!='manifest.json' and not item.name.startswith('payload/'):raise SystemExit('Unexpected path')
 pack.extractall(str(target))
manifest=json.loads((target/'manifest.json').read_text())
for name,digest in manifest['files'].items():
 if hashlib.sha256((target/'payload'/name).read_bytes()).hexdigest()!=digest:raise SystemExit('Checksum mismatch: '+name)
print('MANIFEST_OK')
PY
docker build --network=none --build-arg RUNTIME_IMAGE=any-auto-register:before-zero-limit-20261111 \
 -f "$TARGET/payload/docker/Dockerfile.deploy" -t "any-auto-register:$RELEASE" "$TARGET/payload"
# Check idle
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
active=c.execute("select id,source,status from task_runs where status in ('pending','running')").fetchall()
if active:print('ACTIVE',json.dumps(active));raise SystemExit(2)
c.close();print('IDLE_OK')
PY
# Stop, swap image, start
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" stop -t 90 app
python3 - "$BASE/docker-compose.yml" "$RELEASE" <<'PY'
import sys,re
from pathlib import Path
p,release=Path(sys.argv[1]),sys.argv[2]
text,n=re.subn(r'(?m)^(\s*image:)\s*any-auto-register:[^\s]+',r'\1 any-auto-register:'+release,p.read_text())
assert n==1,'Expected exactly one image: line'
(p.parent/('docker-compose.yml.next-'+release)).write_text(text)
print('COMPOSE_UPDATED')
PY
mv -f "$BASE/docker-compose.yml.next-$RELEASE" "$BASE/docker-compose.yml"
ln -s "$TARGET/payload" "$BASE/src.next-$RELEASE"
mv -Tf "$BASE/src.next-$RELEASE" "$BASE/src"
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" up -d --no-build app
# Health
python3 - <<'PY'
import subprocess,time
for attempt in range(45):
 p=subprocess.run(['curl','--max-time','2','-s','-o','/dev/null','-w','%{http_code}','http://127.0.0.1:18000/api/auth/status'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 if p.returncode==0 and p.stdout=='200':print('HEALTH_OK');break
 time.sleep(1)
else:raise SystemExit('Health check failed')
PY
# Verify trigger endpoint passes without token (no trigger secret configured)
trigger_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:18000/api/chatgpt-web-pool/trigger)
printf 'TRIGGER_HTTP_%s\n' "$trigger_code"
[ "$trigger_code" = "200" ] || { echo 'Trigger endpoint still returns non-200'; exit 1; }
# Verify 200 response body
trigger_body=$(curl -s -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:18000/api/chatgpt-web-pool/trigger)
printf 'TRIGGER_RESPONSE: %s\n' "$trigger_body"
docker stats --no-stream any-auto-register
printf 'WEBHOOK_DEPLOY_OK\n'
