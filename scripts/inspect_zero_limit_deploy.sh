set -eu
printf '\n--- Current release ---\n'
readlink -f /opt/any-auto-register/src
docker inspect any-auto-register --format 'Image={{.Image}} Status={{.State.Status}} Restarts={{.RestartCount}}'
docker inspect any-auto-register --format '{{json .Mounts}}'
printf '\n--- Resources ---\n'
docker stats --no-stream any-auto-register
df -h /opt/any-auto-register
printf '\n--- Read-only account/active/config summary ---\n'
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
safe=('chatgpt_web_pool_daily_registration_limit','chatgpt_web_pool_max_attempts_per_run','chatgpt_web_pool_max_consecutive_failures','chatgpt_web_pool_enabled','inspection_enabled','sms_stock_enabled','cpa_cleanup_enabled')
result={'accounts':c.execute('select count(*) from accounts').fetchone()[0],
'active':c.execute("select id,source,status,total,progress from task_runs where status in ('pending','running')").fetchall(),
'settings':dict(c.execute('select key,value from configs where key in (%s)'%(','.join('?' for _ in safe)),safe))}
print(json.dumps(result));c.close()
PY
