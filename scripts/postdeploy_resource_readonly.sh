set -eu
printf '\n--- Live release and processes ---\n'
readlink -f /opt/any-auto-register/src
docker inspect any-auto-register --format 'Image={{.Image}} Restarts={{.RestartCount}} OOMKilled={{.State.OOMKilled}} Status={{.State.Status}}'
docker stats --no-stream any-auto-register
docker top any-auto-register -eo pid,ppid,rss,comm
printf '\n--- Full API and history verification ---\n'
docker exec -i any-auto-register python - < /opt/any-auto-register/incoming/verify_resource_runtime.py
printf '\n--- Data integrity and additive log storage (no log contents) ---\n'
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json,os
from pathlib import Path
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
r={'quick_check':c.execute('PRAGMA quick_check').fetchone()[0],
   'new_append_log_rows':c.execute('SELECT count(*) FROM task_run_logs').fetchone()[0],
   'new_task_rows':c.execute('SELECT count(*) FROM task_runs WHERE logs_version=1').fetchone()[0],
   'new_task_legacy_json_bytes':c.execute('SELECT coalesce(sum(length(logs_json)),0) FROM task_runs WHERE logs_version=1').fetchone()[0],
   'recent_tasks':[{'source':x[0],'status':x[1],'progress':x[2],'log_count':x[3]} for x in c.execute('SELECT source,status,progress,log_count FROM task_runs ORDER BY created_at DESC LIMIT 3')],
   'restored_enabled_settings':dict(c.execute("SELECT key,value FROM configs WHERE key IN ('inspection_enabled','sms_stock_enabled','chatgpt_web_pool_enabled','cpa_cleanup_enabled')"))}
print(json.dumps(r));c.close()
PY
printf '\n--- Frontend assets and public health ---\n'
curl --max-time 20 -fsS https://sdk.yepoodai.cn/ | head -c 1000
printf '\n'
for url in https://sdk.yepoodai.cn/api/auth/status https://gpt.yepoodai.cn/health?format=json https://sub.yepoodai.cn/ https://cli.yepoodai.cn/; do
  code=$(curl --max-time 20 -s -o /dev/null -w '%{http_code}' "$url")
  printf '%s HTTP %s\n' "$url" "$code"
done
printf '\n--- Host memory and IO pressure ---\n'
free -m
uptime
[ ! -r /proc/pressure/memory ] || cat /proc/pressure/memory
[ ! -r /proc/pressure/io ] || cat /proc/pressure/io
printf '\n--- Error log signature counts (no sensitive content) ---\n'
docker logs --since 10m any-auto-register 2>&1 | python3 -c 'import sys,json;v=sys.stdin.read();print(json.dumps({"tracebacks":v.count("Traceback (most recent call last)"),"integrity_errors":v.count("IntegrityError"),"database_locked":v.count("database is locked"),"solver_disabled":("已禁用" in v) or ("disabled" in v)}))'
printf '\n--- Backups present ---\n'
ls -lh /opt/any-auto-register/backups/resource-20261111T132254Z-cutover/runtime-data.tar.gz
