set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T145604Z
# Wait for active inspection task to finish (up to 5 min)
python3 - <<'PY'
import subprocess,time,json
for attempt in range(300):
 p=subprocess.run(['docker','exec','-i','any-auto-register','python','-c',
   'import sqlite3,json;c=sqlite3.connect("file:/runtime/account_manager.db?mode=ro",uri=True);print(json.dumps(c.execute("select count(*) from task_runs where status in (\'pending\',\'running\')").fetchone()[0]));c.close()'],
   stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 count=int((p.stdout or '1').strip())
 if count==0:print('IDLE_OK');break
 if attempt%30==0:print(f'Waiting for {count} active task(s)... attempt {attempt}')
 time.sleep(1)
else:raise SystemExit('Tasks still running after 5 min')
PY
# Stop, swap image, start
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" stop -t 90 app
python3 - "$BASE/docker-compose.yml" "$RELEASE" <<'PY'
import sys,re
from pathlib import Path
p,release=Path(sys.argv[1]),sys.argv[2]
text,n=re.subn(r'(?m)^(\s*image:)\s*any-auto-register:[^\s]+',r'\1 any-auto-register:'+release,p.read_text())
assert n==1
(p.parent/('docker-compose.yml.next-'+release)).write_text(text)
print('COMPOSE_UPDATED')
PY
mv -f "$BASE/docker-compose.yml.next-$RELEASE" "$BASE/docker-compose.yml"
ln -s "$BASE/releases/$RELEASE/payload" "$BASE/src.next-$RELEASE"
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
# Verify trigger passes without JWT
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:18000/api/chatgpt-web-pool/trigger)
body=$(curl -s -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:18000/api/chatgpt-web-pool/trigger)
printf 'TRIGGER_HTTP_%s body=%s\n' "$code" "$body"
[ "$code" = "200" ] || { echo 'Trigger not 200'; exit 1; }
docker stats --no-stream any-auto-register
readlink -f "$BASE/src"
printf 'WEBHOOK_DEPLOY_OK\n'
