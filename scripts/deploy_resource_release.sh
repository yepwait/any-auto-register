set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T132254Z
TARGET="$BASE/releases/$RELEASE/payload"
BACKUP="$BASE/backups/$RELEASE-cutover"
CANDIDATE="$BASE/incoming/$RELEASE.compose.yml"
OLD_SOURCE=$(readlink -f "$BASE/src")
[ -f "$TARGET/main.py" ] && [ -f "$CANDIDATE" ]
[ ! -e "$BACKUP" ] || { echo 'Cutover backup already exists; refusing overwrite'; exit 1; }
[ "$(docker inspect any-auto-register-resource-preflight --format '{{.Image}}')" = "$(docker image inspect any-auto-register:$RELEASE --format '{{.Id}}')" ]
docker compose --project-directory "$BASE" -p any-auto-register -f "$CANDIDATE" config --quiet
mkdir -m 700 "$BACKUP"
cp -p "$BASE/docker-compose.yml" "$BACKUP/docker-compose.yml"
printf '%s\n' "$OLD_SOURCE" > "$BACKUP/source-path.txt"
docker inspect any-auto-register --format '{{.Image}}' > "$BACKUP/image-id.txt"
# Confirm all move/replacement targets are precisely the intended deployment.
python3 - "$BASE" "$TARGET" "$OLD_SOURCE" <<'PY'
from pathlib import Path
import sys
base,target,old=map(Path,sys.argv[1:])
assert str(base.resolve())=='/opt/any-auto-register'
assert str(target.resolve()).startswith('/opt/any-auto-register/releases/')
assert str(old.resolve()).startswith('/opt/any-auto-register/releases/')
assert (base/'src').is_symlink()
assert (base/'docker-compose.yml').is_file()
print('VERIFIED_DEPLOY_TARGETS',base,target)
PY
switched=0
paused=0
restore_flags() {
  docker run --rm --network none --entrypoint python -v "$BASE/data:/runtime" "any-auto-register:$RELEASE" -c 'import sqlite3,json;from pathlib import Path;p=Path("/runtime/resource-maintenance-flags.json");saved=json.loads(p.read_text());c=sqlite3.connect("/runtime/account_manager.db");[(c.execute("delete from configs where key=?",(k,)) if v is None else c.execute("insert into configs(key,value) values(?,?) on conflict(key) do update set value=excluded.value",(k,v))) for k,v in saved.items()];c.commit();c.close();print("AUTOMATION_SETTINGS_RESTORED")'
}
rollback() {
  code=$?
  trap - EXIT
  if [ "$code" -ne 0 ]; then
    echo 'Deployment failed; restoring previous image/configuration, preserving additive database.'
    if [ "$paused" -eq 1 ]; then restore_flags || true; fi
    if [ "$switched" -eq 1 ]; then
      cp -p "$BACKUP/docker-compose.yml" "$BASE/docker-compose.yml"
      ln -s "$OLD_SOURCE" "$BASE/src.rollback-$RELEASE"
      mv -Tf "$BASE/src.rollback-$RELEASE" "$BASE/src"
      docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" up -d --no-build app || true
    fi
  fi
  exit "$code"
}
trap rollback EXIT
# Pause only automatic task admission. Preserve exact values for restoration.
# An in-flight task is never stopped by this script: it aborts before restart.
docker exec -i any-auto-register python - <<'PY'
import json,sqlite3
from pathlib import Path
c=sqlite3.connect('/runtime/account_manager.db',timeout=15)
keys=('inspection_enabled','sms_stock_enabled','chatgpt_web_pool_enabled','cpa_cleanup_enabled')
path=Path('/runtime/resource-maintenance-flags.json')
if path.exists(): raise SystemExit('Existing maintenance state requires review')
saved={key:(lambda r:r[0] if r else None)(c.execute('select value from configs where key=?',(key,)).fetchone()) for key in keys}
path.write_text(json.dumps(saved));path.chmod(0o600)
c.execute('begin immediate')
for key in keys:c.execute("update configs set value='0' where key=?",(key,))
c.commit()
print('AUTOMATION_PAUSED_FOR_CUTOVER')
c.close()
PY
paused=1
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
active=c.execute("select id,source,status,progress from task_runs where status in ('pending','running')").fetchall()
if active:
 print('ACTIVE_TASKS_NOT_INTERRUPTED',json.dumps(active));raise SystemExit(2)
print('IDLE_CHECK_OK')
c.close()
PY
# Now stop the old app gracefully, retain every mounted directory and image.
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" stop -t 120 app
switched=1
restore_flags
paused=0
# Quiescent backup: data, keys, configs and logs, no live SQLite copy race.
tar -czf "$BACKUP/runtime-data.tar.gz" -C "$BASE" data
cp -p "$CANDIDATE" "$BASE/docker-compose.yml.next-$RELEASE"
mv -f "$BASE/docker-compose.yml.next-$RELEASE" "$BASE/docker-compose.yml"
ln -s "$TARGET" "$BASE/src.next-$RELEASE"
mv -Tf "$BASE/src.next-$RELEASE" "$BASE/src"
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" up -d --no-build app
python3 - <<'PY'
import subprocess,time
for attempt in range(45):
 p=subprocess.run(['curl','--max-time','2','-s','-o','/dev/null','-w','%{http_code}','http://127.0.0.1:18000/api/auth/status'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 if p.returncode==0 and p.stdout=='200':print('PRODUCTION_UPSTREAM_HEALTH_OK');break
 time.sleep(1)
else:raise SystemExit('Production upstream health failed')
PY
# Verified runtime uses the exact preserved account database and legacy history.
docker exec -i any-auto-register python - < "$BASE/incoming/verify_resource_runtime.py"
printf '\n--- New production processes ---\n'
docker top any-auto-register -eo pid,ppid,rss,comm
if docker top any-auto-register -eo comm | grep -E 'camoufox|firefox|Xvfb' >/dev/null; then
 echo 'Unexpected browser remains in new container';exit 1
fi
printf '\n--- Resource limits ---\n'
docker inspect any-auto-register --format 'Memory={{.HostConfig.Memory}} SwapTotal={{.HostConfig.MemorySwap}} CPUs={{.HostConfig.NanoCpus}} Log={{json .HostConfig.LogConfig}}'
printf '\n--- Production memory ---\n'
docker stats --no-stream any-auto-register
free -m
printf '\n--- Public application route ---\n'
PUBLIC_HTTP=$(curl --max-time 20 -s -o /dev/null -w '%{http_code}' https://sdk.yepoodai.cn/api/auth/status)
printf 'sdk.yepoodai.cn HTTP %s\n' "$PUBLIC_HTTP"
[ "$PUBLIC_HTTP" = 200 ] || { echo 'Public reverse-proxy health failed'; exit 1; }
printf '\nDEPLOYMENT_VERIFIED\nbackup=%s\nsource=%s\n' "$BACKUP" "$TARGET"
# Keep the preflight container stopped for diagnosis; no deletion of any data.
docker stop -t 20 any-auto-register-resource-preflight >/dev/null
trap - EXIT
