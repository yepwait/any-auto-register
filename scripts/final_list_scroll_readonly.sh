set -eu
umask 077
python3 - <<'PY'
import json,hashlib,subprocess
from pathlib import Path
base=Path('/opt/any-auto-register');release='list-scroll-20261112T062323Z';backup=base/'backups'/release
before=json.loads((backup/'container-before.json').read_text())
after=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
assert after['Id']==before['Id'] and after['State']['StartedAt']==before['State']['StartedAt'] and after['RestartCount']==before['RestartCount']
assert after['State']['Running'] and not after['State']['OOMKilled']
compose=json.loads(subprocess.check_output(['docker','compose','--project-directory',str(base),'-p','any-auto-register','-f',str(base/'docker-compose.yml'),'config','--format','json']).decode())
assert compose['services']['app']['image']=='any-auto-register:'+release
assert (base/'src').resolve()==base/'releases'/release/'source'
assert json.loads(subprocess.check_output(['docker','image','inspect','any-auto-register:'+release]).decode())[0]['Id']=='sha256:dc5aff329459cc7ea39ced0a7d85134f04f5d0b3cddf49284d766161cf6a7b9f'
manifest=json.loads((base/'releases'/release/'manifest.json').read_text())
expected={k:v for k,v in manifest['files'].items() if k.startswith('static/') or k=='UI-RELEASE.json'}
code='import json,hashlib,sys;from pathlib import Path;d=json.load(sys.stdin);assert all((Path("/app")/k).is_file() and hashlib.sha256((Path("/app")/k).read_bytes()).hexdigest()==v for k,v in d.items());print("LIVE_STATIC_MANIFEST_OK",len(d))'
p=subprocess.Popen(['docker','exec','-i','any-auto-register','python','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
out,err=p.communicate(json.dumps(expected).encode());assert p.returncode==0,'Live resource checksum mismatch'
print(out.decode().strip())
rollback=base/'incoming'/(release+'.rollback.py');assert rollback.is_file()
subprocess.check_call(['cp','-p',str(rollback),str(backup/'rollback-ui.py')])
(backup/'rollback-ui.py').chmod(0o600)
print('DEPLOYMENT',json.dumps(json.loads((backup/'deployment.json').read_text()),ensure_ascii=False))
print('ROLLBACK_READY',str(backup/'rollback-ui.py'))
PY
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
print('READONLY_COUNTS',json.dumps({'accounts':c.execute('select count(*) from accounts').fetchone()[0],
'active_tasks':c.execute("select count(*) from task_runs where status in ('pending','running')").fetchone()[0]}));c.close()
PY
curl --max-time 20 -s -o /dev/null -w 'public HTTP %{http_code}\n' https://sdk.yepoodai.cn/api/auth/status
