set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=ui-reliable-20261112T051329Z
BACKUP="$BASE/backups/$RELEASE/attempt-2"
printf '\n--- Completed cutover evidence ---\n'
python3 - <<'PY'
import json,subprocess,hashlib
from pathlib import Path
base=Path('/opt/any-auto-register');release='ui-reliable-20261112T051329Z';backup=base/'backups'/release/'attempt-2'
for name in ('deployment-complete.json','old-exit.json','shutdown-check.json'):
 print(name,json.dumps(json.loads((backup/name).read_text()),ensure_ascii=False))
before=json.loads((backup/'quiescent-baseline.json').read_text())
maintenance=json.loads((backup/'maintenance-verification.json').read_text())
assert before['accounts_digest']==maintenance['accounts_digest']
assert before['configs_digest']==maintenance['configs_digest']
assert before['key_present']==maintenance['key_present'] and before['key_digest']==maintenance['key_digest']
print('MAINTENANCE_PRESERVATION',json.dumps({'accounts':before['counts']['accounts'],'configs_identical':True,'accounts_identical':True,'key_state_identical':True}))
info=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
env=dict(x.split('=',1) for x in info['Config']['Env'] if '=' in x)
assert info['Config']['Image']=='any-auto-register:'+release
assert info['State']['Running'] and not info['State']['OOMKilled']
assert env.get('APP_ENABLE_SCHEDULER','1').lower() not in ('0','false','no','off')
print('CURRENT_RUNTIME',json.dumps({'image_id':info['Image'],'image':info['Config']['Image'],'restart_count':info['RestartCount'],
 'state':info['State']['Status'],'oom':info['State']['OOMKilled'],'scheduler_restored':True,
 'mounts':[{'source':v['Source'],'destination':v['Destination'],'rw':v['RW']} for v in info['Mounts']],
 'resources':{'memory':info['HostConfig']['Memory'],'swap':info['HostConfig']['MemorySwap'],'cpus':info['HostConfig']['NanoCpus']}}))
archive=backup/'runtime-data.tar.gz'
actual=hashlib.sha256(archive.read_bytes()).hexdigest()
expected=(backup/'runtime-data.sha256').read_text().split()[0]
assert actual==expected
print('BACKUP_VERIFIED',json.dumps({'path':str(archive),'bytes':archive.stat().st_size,'sha256':actual,'permissions':oct(archive.stat().st_mode & 0o777)}))
assert (backup/'rollback-code-only.py').is_file()
print('ROLLBACK_SCRIPT_PRESENT',str(backup/'rollback-code-only.py'))
# Check runtime source/static bytes against the release manifest, without reading any credentials.
manifest=json.loads((base/'releases'/release/'manifest.json').read_text())
selected={k:v for k,v in manifest['files'].items() if k.split('/')[0] in ('api','core','services','platforms','mail','static') or k in ('main.py','requirements.txt','RELEASE.json','docker/entrypoint.server.sh')}
# The Dockerfile deliberately converts the shell entrypoint from CRLF to LF.
entry=base/'releases'/release/'payload/docker/entrypoint.server.sh'
selected['docker/entrypoint.server.sh']=hashlib.sha256(entry.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
code='''import json,hashlib,sys;from pathlib import Path;data=json.load(sys.stdin);bad=[name for name,digest in data.items() if not (Path("/app")/name).is_file() or hashlib.sha256((Path("/app")/name).read_bytes()).hexdigest()!=digest];assert not bad,bad;print("LIVE_MANIFEST_VERIFIED",len(data))'''
p=subprocess.Popen(['docker','exec','-i','any-auto-register','python','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
out,err=p.communicate(json.dumps(selected).encode())
assert p.returncode==0,'Live manifest differs'
print(out.decode().strip())
# Query live Caddy configuration in its namespace; never print any unrelated configuration.
cmd=['docker','run','--rm','--network','container:caddy','--entrypoint','python','any-auto-register:'+release,'-c',
 'import urllib.request,json;d=json.load(urllib.request.urlopen("http://127.0.0.1:2019/config/",timeout=10));assert not any(r.get("@id")=="ui-release-write-gate" for s in d.get("apps",{}).get("http",{}).get("servers",{}).values() for r in s.get("routes",[]));print("PUBLIC_WRITE_GATE_ABSENT")']
print(subprocess.check_output(cmd).decode().strip())
PY
printf '\n--- Runtime GET verification ---\n'
docker exec -i any-auto-register python - < "$BASE/incoming/$RELEASE.verify.py"
printf '\n--- Production resources ---\n'
docker stats --no-stream any-auto-register
printf '\n--- Existing site health ---\n'
curl --max-time 20 -s -o /dev/null -w 'public auth/status HTTP %{http_code}\n' https://sdk.yepoodai.cn/api/auth/status
printf '\n--- Stop isolated preflight only ---\n'
docker stop -t 30 any-auto-register-ui-preflight
printf '\nFINAL_RELEASE_READONLY_VERIFIED\n'
