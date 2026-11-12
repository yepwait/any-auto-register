set -eu
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json,time
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
rows=c.execute("select id,source,status,progress,created_at,updated_at,success,skipped from task_runs where status in ('pending','running')").fetchall()
print(json.dumps({'utc_epoch':int(time.time()),'active':rows},ensure_ascii=False));c.close()
PY
