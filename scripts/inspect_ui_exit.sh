set -eu
python3 - <<'PY'
import json,subprocess
from pathlib import Path
p=Path('/opt/any-auto-register/backups/ui-reliable-20261112T051329Z')
print('PREVIOUS_EXIT', (p/'old-exit.json').read_text())
d=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
print('CURRENT',json.dumps({'image':d['Config']['Image'],'state':d['State'],'restart':d['RestartCount']},ensure_ascii=False))
print('ATTEMPT', (p/'cutover-started.json').read_text())
PY
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
print('ACTIVE',json.dumps(c.execute("select id,source,status,progress from task_runs where status in ('pending','running')").fetchall(),ensure_ascii=False))
print('COUNTS',c.execute('select count(*) from accounts').fetchone()[0]);c.close()
PY
curl --max-time 20 -s -o /dev/null -w 'public HTTP %{http_code}\n' https://sdk.yepoodai.cn/api/auth/status
