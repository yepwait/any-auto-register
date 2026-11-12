set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=ui-reliable-20261112T051329Z
BACKUP="$BASE/backups/$RELEASE"
[ -f "$BACKUP/container-before.json" ] && [ -f "$BASE/incoming/$RELEASE.verify.py" ]
[ ! -e "$BACKUP/preflight-data" ] || { echo 'Preflight copy already exists';exit 1; }
mkdir -m 700 "$BACKUP/preflight-data"
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json,hashlib,os
from pathlib import Path
base=Path('/tmp/ui-reliable-20261112T051329Z')
assert not base.exists();base.mkdir(mode=0o700)
def backup(name):
 source=sqlite3.connect('file:/runtime/'+name+'?mode=ro',uri=True)
 target=sqlite3.connect(str(base/name));source.backup(target);source.close()
 assert target.execute('pragma quick_check').fetchone()[0]=='ok'
 return target
c=backup('account_manager.db')
digest=lambda value:hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str).encode()).hexdigest()
key=Path(os.getenv('CREDENTIAL_ENCRYPTION_KEY_FILE','/runtime/.secrets/credential_key'))
config=dict(c.execute('select key,value from configs'))
result={'configs_digest':digest(sorted(config.items())),
 'accounts_digest':digest(c.execute('select * from accounts order by id').fetchall()),
 'counts':{t:c.execute('select count(*) from '+t).fetchone()[0] for t in ('accounts','task_runs','task_logs','task_run_logs')},
 'key_present':key.is_file(),'key_digest':hashlib.sha256(key.read_bytes()).hexdigest() if key.is_file() else None}
(base/'baseline.json').write_text(json.dumps(result));c.close()
if Path('/runtime/payment_cards.db').is_file(): backup('payment_cards.db').close()
print('CONSISTENT_PREFLIGHT_SNAPSHOT_CREATED',result['counts'])
PY
docker cp any-auto-register:/tmp/ui-reliable-20261112T051329Z/account_manager.db "$BACKUP/preflight-data/account_manager.db"
docker cp any-auto-register:/tmp/ui-reliable-20261112T051329Z/baseline.json "$BACKUP/preflight-baseline.json"
if docker exec any-auto-register test -f /tmp/ui-reliable-20261112T051329Z/payment_cards.db; then
 docker cp any-auto-register:/tmp/ui-reliable-20261112T051329Z/payment_cards.db "$BACKUP/preflight-data/payment_cards.db"
fi
if [ -d "$BASE/data/.secrets" ]; then cp -a "$BASE/data/.secrets" "$BACKUP/preflight-data/.secrets"; fi
chmod 600 "$BACKUP/preflight-data/account_manager.db" "$BACKUP/preflight-baseline.json"
python3 - <<'PY'
import json,subprocess
from pathlib import Path
release='ui-reliable-20261112T051329Z';base=Path('/opt/any-auto-register');backup=base/'backups'/release
info=json.loads((backup/'container-before.json').read_text())
env=dict(x.split('=',1) for x in info['Config']['Env'] if '=' in x)
env.update(APP_ENABLE_SCHEDULER='0',APP_ENABLE_SOLVER='0',APP_RUNTIME_DIR='/runtime',DATABASE_URL='sqlite:////runtime/account_manager.db')
args=['docker','run','-d','--name','any-auto-register-ui-preflight','--init','--network','none','--memory','768m','--memory-swap','1g','--cpus','0.5']
for k,v in env.items():args.extend(['-e',k+'='+v])
args.extend(['-v',str(backup/'preflight-data')+':/runtime','-v',str(backup/'preflight-baseline.json')+':/verification-before.json:ro',
 '-v',str(base/'incoming'/(release+'.verify.py'))+':/app/verify-ui.py:ro','any-auto-register:'+release])
subprocess.check_call(args)
PY
python3 - <<'PY'
import subprocess,time
for i in range(45):
 p=subprocess.Popen(['docker','exec','any-auto-register-ui-preflight','python','-c','import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8000/api/auth/status",timeout=2).status)'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 out,err=p.communicate()
 if p.returncode==0 and out.strip()==b'200':print('ISOLATED_PREFLIGHT_HEALTH_OK');break
 time.sleep(1)
else:raise SystemExit('Preflight failed; production untouched')
PY
docker exec any-auto-register-ui-preflight python /app/verify-ui.py --idle --compare /verification-before.json
printf '\n--- Preflight resource usage ---\n'
docker stats --no-stream any-auto-register-ui-preflight
docker inspect any-auto-register-ui-preflight --format 'network={{.HostConfig.NetworkMode}} scheduler-disabled=verified-by-env restart={{.RestartCount}}'
printf '\nPREFLIGHT_OK_PRODUCTION_UNCHANGED\n'
