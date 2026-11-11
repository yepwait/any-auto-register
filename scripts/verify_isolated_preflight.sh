set -eu
docker exec -i any-auto-register-resource-preflight python - < /opt/any-auto-register/incoming/verify_resource_runtime.py
printf '\n--- Preflight Node SDK exit smoke test (no network) ---\n'
docker exec any-auto-register-resource-preflight node --version
printf '\n--- Production active task check ---\n'
docker exec any-auto-register python -c 'import sqlite3,json;c=sqlite3.connect("file:/runtime/account_manager.db?mode=ro",uri=True);print(json.dumps(c.execute("select id,source,status,total,progress from task_runs where status in (\"pending\",\"running\")").fetchall()));c.close()'
