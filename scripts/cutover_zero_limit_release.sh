set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T140633Z
TARGET="$BASE/releases/$RELEASE/payload"
BACKUP="$BASE/backups/$RELEASE"
ORIGINAL="$BACKUP/docker-compose.yml"
CANDIDATE="$BASE/incoming/$RELEASE.compose.yml"
OLD=$(cat "$BACKUP/source-path.txt")
FLAGS="$RELEASE-maintenance-flags.json"
[ "$(readlink -f "$BASE/src")" = "$OLD" ] || { echo 'Current source changed since preflight; abort'; exit 1; }
[ "$(docker inspect any-auto-register --format '{{.Image}}')" = "$(cat "$BACKUP/image-id.txt")" ] || { echo 'Current image changed since preflight; abort'; exit 1; }
[ "$(docker inspect any-auto-register-zero-limit-preflight --format '{{.Image}}')" = "$(docker image inspect any-auto-register:$RELEASE --format '{{.Id}}')" ]
[ ! -e "$BACKUP/runtime-data.tar.gz" ] || { echo 'Cutover backup exists; review before retry'; exit 1; }
python3 - "$BASE" "$TARGET" "$ORIGINAL" "$CANDIDATE" <<'PY'
from pathlib import Path
import sys,re
base,target,original,candidate=map(Path,sys.argv[1:])
assert str(base.resolve())=='/opt/any-auto-register'
assert str(target.resolve())=='/opt/any-auto-register/releases/resource-20261111T140633Z/payload'
assert (base/'src').is_symlink() and (base/'docker-compose.yml').is_file()
assert original.read_bytes()==(base/'docker-compose.yml').read_bytes(),'compose changed since preflight'
text=original.read_text()
text,count=re.subn(r'(?m)^(\s*image:)\s*any-auto-register:[^\s]+\s*$',r'\1 any-auto-register:resource-20261111T140633Z',text)
assert count==1,'Image field count mismatch'
candidate.write_text(text)
print('VERIFIED_COMPOSE_IMAGE_ONLY_CHANGE')
PY
docker compose --project-directory "$BASE" -p any-auto-register -f "$CANDIDATE" config --quiet
paused=0
switched=0
restore_flags() {
 docker run --rm --network none --entrypoint python -v "$BASE/data:/runtime" "any-auto-register:$RELEASE" -c 'import sqlite3,json;from pathlib import Path;p=Path("/runtime/resource-20261111T140633Z-maintenance-flags.json");values=json.loads(p.read_text());c=sqlite3.connect("/runtime/account_manager.db",timeout=15);c.execute("begin immediate");[(c.execute("delete from configs where key=?",(k,)) if v is None else c.execute("insert into configs(key,value) values(?,?) on conflict(key) do update set value=excluded.value",(k,v))) for k,v in values.items()];c.commit();c.close();print("AUTOMATION_FLAGS_RESTORED")'
}
rollback() {
 code=$?
 trap - EXIT
 if [ "$code" -ne 0 ]; then
  echo 'Cutover failed; restoring original code/image and flags, never overwriting new account data.'
  if [ "$paused" -eq 1 ]; then restore_flags || true; fi
  if [ "$switched" -eq 1 ]; then
   cp -p "$ORIGINAL" "$BASE/docker-compose.yml"
   ln -s "$OLD" "$BASE/src.rollback-$RELEASE"
   mv -Tf "$BASE/src.rollback-$RELEASE" "$BASE/src"
   docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" up -d --no-build app || true
  fi
 fi
 exit "$code"
}
trap rollback EXIT
# Pause only scheduler admission, not active workers; never modify the daily cap.
docker exec -i any-auto-register python - "$FLAGS" <<'PY'
import sqlite3,json,sys
from pathlib import Path
p=Path('/runtime')/sys.argv[1]
if p.exists():raise SystemExit('Existing maintenance state needs review')
c=sqlite3.connect('/runtime/account_manager.db',timeout=15)
keys=('inspection_enabled','sms_stock_enabled','chatgpt_web_pool_enabled','cpa_cleanup_enabled')
c.execute('begin immediate')
active=c.execute("select id,source,status,progress from task_runs where status in ('pending','running')").fetchall()
if active:
 c.rollback();print('ACTIVE_TASKS_NOT_INTERRUPTED',json.dumps(active));raise SystemExit(2)
saved={key:(lambda r:r[0] if r else None)(c.execute('select value from configs where key=?',(key,)).fetchone()) for key in keys}
p.write_text(json.dumps(saved));p.chmod(0o600)
for key in keys:c.execute("update configs set value='0' where key=?",(key,))
c.commit();c.close();print('IDLE_AND_AUTOMATION_PAUSED')
PY
paused=1
# Double-check once after pausing; if a manual task appeared, restore flags and abort.
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
active=c.execute("select id,source,status,progress from task_runs where status in ('pending','running')").fetchall()
if active: print('REFUSE_TO_INTERRUPT_TASK',json.dumps(active));raise SystemExit(2)
c.close()
PY
# We verified exact paths above. The old release remains intact for rollback.
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" stop -t 120 app
switched=1
restore_flags
paused=0
# Copy while stopped for a consistent rollback snapshot, never restore it automatically.
tar -czf "$BACKUP/runtime-data.tar.gz" -C "$BASE" data
docker run --rm --network none --entrypoint python -v "$BASE/data:/runtime:ro" "any-auto-register:$RELEASE" -c 'import sqlite3,json,hashlib;c=sqlite3.connect("file:/runtime/account_manager.db?mode=ro",uri=True);data=c.execute("select key,value from configs order by key").fetchall();print(json.dumps({"config_digest":hashlib.sha256(json.dumps(data).encode()).hexdigest(),"daily_limit":dict(data).get("chatgpt_web_pool_daily_registration_limit"),"accounts":c.execute("select count(*) from accounts").fetchone()[0]}));c.close()' > "$BACKUP/preserved-config.json"
cp -p "$CANDIDATE" "$BASE/docker-compose.yml.next-$RELEASE"
mv -f "$BASE/docker-compose.yml.next-$RELEASE" "$BASE/docker-compose.yml"
ln -s "$TARGET" "$BASE/src.next-$RELEASE"
mv -Tf "$BASE/src.next-$RELEASE" "$BASE/src"
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" up -d --no-build app
python3 - <<'PY'
import subprocess,time
for attempt in range(45):
 p=subprocess.run(['curl','--max-time','2','-s','-o','/dev/null','-w','%{http_code}','http://127.0.0.1:18000/api/auth/status'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 if p.returncode==0 and p.stdout=='200':print('PRODUCTION_HEALTH_OK');break
 time.sleep(1)
else:raise SystemExit('Production health check failed')
PY
docker exec -i any-auto-register python - < "$BASE/incoming/verify_zero_limit_runtime.py"
# Existing configuration must not be changed by startup or the read-only tests.
docker run --rm --network none --entrypoint python -v "$BASE/data:/runtime:ro" -v "$BACKUP:/deployment-backup:ro" "any-auto-register:$RELEASE" -c 'import sqlite3,json,hashlib;from pathlib import Path;c=sqlite3.connect("file:/runtime/account_manager.db?mode=ro",uri=True);before=json.loads(Path("/deployment-backup/preserved-config.json").read_text());digest=hashlib.sha256(json.dumps(c.execute("select key,value from configs order by key").fetchall()).encode()).hexdigest();assert digest==before["config_digest"],"Saved configuration changed during deployment";assert c.execute("select count(*) from accounts").fetchone()[0]==before["accounts"];print("ALL_CONFIG_VALUES_AND_ACCOUNT_COUNT_PRESERVED");c.close()'
printf '\n--- Public frontend/backend ---\n'
for path in /api/auth/status /assets/index-DOI_liSL.js /assets/ChatgptWebPool-CuH9R645.js; do
 code=$(curl --max-time 20 -s -o /dev/null -w '%{http_code}' "https://sdk.yepoodai.cn$path")
 printf '%s HTTP %s\n' "$path" "$code"
 [ "$code" = 200 ]
done
printf '\n--- Resource state ---\n'
docker stats --no-stream any-auto-register
docker inspect any-auto-register --format 'Restarts={{.RestartCount}} OOMKilled={{.State.OOMKilled}} Image={{.Image}}'
docker top any-auto-register -eo pid,ppid,rss,comm
# Stop only this staging container, keep it and all backups for diagnosis.
docker stop -t 20 any-auto-register-zero-limit-preflight >/dev/null
printf '\nZERO_LIMIT_DEPLOYMENT_VERIFIED\nbackup=%s\nsource=%s\n' "$BACKUP" "$TARGET"
trap - EXIT
