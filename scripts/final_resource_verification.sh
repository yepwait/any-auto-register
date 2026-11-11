set -eu
printf '\n--- Stable production metrics ---\n'
docker stats --no-stream any-auto-register
docker inspect any-auto-register --format 'RestartCount={{.RestartCount}} Status={{.State.Status}} OOMKilled={{.State.OOMKilled}}'
printf '\n--- Public assets ---\n'
for asset in index-_uostjeg.js RunningTasks-B7fgoAK_.js TaskLogPanel-B5sT6GFc.js; do
 code=$(curl --max-time 20 -s -o /dev/null -w '%{http_code}' "https://sdk.yepoodai.cn/assets/$asset")
 printf '%s HTTP %s\n' "$asset" "$code"
 [ "$code" = 200 ]
done
printf '\n--- Live task progress and account preservation ---\n'
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
result={'accounts':c.execute('SELECT count(*) FROM accounts').fetchone()[0],
'latest_tasks':c.execute('SELECT source,status,progress,log_count FROM task_runs ORDER BY created_at DESC LIMIT 2').fetchall(),
'new_append_lines':c.execute('SELECT count(*) FROM task_run_logs').fetchone()[0]}
print(json.dumps(result));c.close()
PY
printf '\n--- Browser process absence ---\n'
docker top any-auto-register -eo pid,ppid,rss,comm
printf '\n--- Auth protection unchanged ---\n'
status=$(curl --max-time 10 -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/api/tasks/resources)
printf 'Unauthenticated resources HTTP %s\n' "$status"
printf '\n--- Disk and host memory ---\n'
df -h /
free -m
printf '\n--- Rollback helper shell syntax only ---\n'
sh -n /opt/any-auto-register/backups/resource-20261111T132254Z-cutover/rollback_resource_release.sh
printf '\nFINAL_VERIFICATION_OK\n'
