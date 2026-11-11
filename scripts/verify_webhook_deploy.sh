set -eu
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
rows=dict(c.execute("select key,value from configs where key like '%trigger%'"))
print('trigger_config:',json.dumps(rows))
c.close()
PY
docker stats --no-stream any-auto-register
printf 'DEPLOY_OK\n'
