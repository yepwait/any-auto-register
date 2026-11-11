set -eu
printf '\n--- Caddy upstream for this app ---\n'
docker exec caddy sh -c 'grep -B 3 -A 4 "18000" /etc/caddy/Caddyfile || true'
printf '\n--- Saved deployment source hashes ---\n'
cd /opt/any-auto-register/src
sha256sum core/base_platform.py core/proxy_utils.py api/accounts.py api/actions.py services/sms_service.py platforms/chatgpt/protocol/auth_flow.py platforms/chatgpt/protocol/phone_flow.py platforms/chatgpt/status_probe.py requirements.txt main.py
printf '\n--- Active tasks and library baseline ---\n'
docker exec any-auto-register python -c 'import sqlite3,json; c=sqlite3.connect("file:/runtime/account_manager.db?mode=ro",uri=True); print(json.dumps(c.execute("select id,source,status,total,progress from task_runs where status in (\"pending\",\"running\")").fetchall()));c.close()'
printf '\n--- Disk image baseline ---\n'
docker image inspect any-auto-register:solver --format '{{.Id}} {{.Size}}'
