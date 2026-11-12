set -eu
python3 - <<'PY'
import json,subprocess
from pathlib import Path
base=Path('/opt/any-auto-register')
d=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
print(json.dumps({'image':d['Config']['Image'],'image_id':d['Image'],'id':d['Id'],'started':d['State']['StartedAt'],'restart_count':d['RestartCount'],
'state':d['State']['Status'],'source':str((base/'src').resolve()),'mounts':[(x['Source'],x['Destination']) for x in d['Mounts']]}))
PY
docker exec -i any-auto-register python - <<'PY'
import json,hashlib,sqlite3
from pathlib import Path
index=Path('/app/static/index.html')
print('INDEX_SHA256',hashlib.sha256(index.read_bytes()).hexdigest())
print('RELEASE',Path('/app/RELEASE.json').read_text())
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
print('ACCOUNT_COUNT',c.execute('select count(*) from accounts').fetchone()[0])
print('ACTIVE',json.dumps(c.execute("select id,source,status,progress from task_runs where status in ('pending','running')").fetchall(),ensure_ascii=False));c.close()
PY
