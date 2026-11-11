# Review before execution. Roll back code/image only; NEVER restore old account data.
set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T132254Z
BACKUP="$BASE/backups/$RELEASE-cutover"
[ -f "$BACKUP/docker-compose.yml" ] && [ -f "$BACKUP/source-path.txt" ]
CURRENT=$(readlink -f "$BASE/src")
OLD=$(cat "$BACKUP/source-path.txt")
[ "$CURRENT" = "$BASE/releases/$RELEASE/payload" ] || { echo 'Current release differs; manual review required'; exit 1; }
case "$OLD" in "$BASE"/releases/*/payload) ;; *) echo 'Unexpected rollback path';exit 1;; esac
[ -d "$OLD" ]
# Reject active tasks rather than losing an in-flight signup/SMS result.
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('/runtime/account_manager.db',timeout=15)
active=c.execute("select id,source,status,progress from task_runs where status in ('pending','running')").fetchall()
if active:
 print('REFUSE_ROLLBACK_ACTIVE_TASKS',json.dumps(active));raise SystemExit(2)
c.close()
PY
# Use the current app's graceful shutdown/drain before the one-time compatibility export.
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" stop -t 120 app
# The old release reads logs_json only. Export NEW task log rows there without
# deleting the append table or changing accounts/configs. Data remains forward-compatible.
docker run --rm -i --network none --entrypoint python -v "$BASE/data:/runtime" "any-auto-register:$RELEASE" - <<'PY'
import sqlite3,json
c=sqlite3.connect('/runtime/account_manager.db',timeout=15)
assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
ids=[r[0] for r in c.execute('SELECT id FROM task_runs WHERE logs_version=1')]
for task_id in ids:
 lines=[r[0] for r in c.execute('SELECT line FROM task_run_logs WHERE task_id=? ORDER BY sequence',(task_id,))]
 c.execute('UPDATE task_runs SET logs_json=? WHERE id=?',(json.dumps(lines,ensure_ascii=False),task_id))
c.commit();c.close();print('NEW_LOGS_EXPORTED_FOR_LEGACY_READER',len(ids))
PY
# These targets have been explicitly checked above.
cp -p "$BACKUP/docker-compose.yml" "$BASE/docker-compose.yml.rollback-next"
mv -f "$BASE/docker-compose.yml.rollback-next" "$BASE/docker-compose.yml"
ln -s "$OLD" "$BASE/src.rollback-next"
mv -Tf "$BASE/src.rollback-next" "$BASE/src"
docker compose --project-directory "$BASE" -p any-auto-register -f "$BASE/docker-compose.yml" up -d --no-build app
printf 'ROLLED_BACK_CODE_ONLY; account data was not restored or deleted\n'
