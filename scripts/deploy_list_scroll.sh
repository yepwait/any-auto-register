set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=list-scroll-20261112T062323Z
ARCHIVE="$BASE/incoming/$RELEASE.tar.gz"
TARGET="$BASE/releases/$RELEASE"
BACKUP="$BASE/backups/$RELEASE"
EXPECTED=dec211b6715607ab9ae33e8d313ae6e8c52eac9738138d146e4a1eac697ca322
printf '%s  %s\n' "$EXPECTED" "$ARCHIVE" | sha256sum -c -
[ ! -e "$TARGET" ] && [ ! -e "$BACKUP" ] || { echo 'Target exists; do not overwrite';exit 1; }
mkdir -m 700 "$TARGET" "$BACKUP"
python3 - <<'PY'
import json,hashlib,tarfile,subprocess,shutil
from pathlib import Path
base=Path('/opt/any-auto-register');release='list-scroll-20261112T062323Z';target=base/'releases'/release;backup=base/'backups'/release
original=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
assert original['Config']['Image']=='any-auto-register:ui-reliable-20261112T051329Z'
assert original['State']['Running']
(backup/'container-before.json').write_text(json.dumps(original))
(backup/'docker-compose.yml').write_bytes((base/'docker-compose.yml').read_bytes())
old=(base/'src').resolve();assert old==base/'releases/ui-reliable-20261112T051329Z/payload'
(backup/'source-path.txt').write_text(str(old)+'\n')
with tarfile.open(str(base/'incoming'/(release+'.tar.gz'))) as pack:
 for m in pack.getmembers():
  p=Path(m.name);assert m.isfile() and not m.issym() and not m.islnk() and not p.is_absolute() and '..' not in p.parts
  assert m.name=='manifest.json' or m.name.startswith('payload/')
 pack.extractall(str(target))
manifest=json.loads((target/'manifest.json').read_text())
for name,digest in manifest['files'].items():assert hashlib.sha256((target/'payload'/name).read_bytes()).hexdigest()==digest,name
# Preserve complete backend source for the next clean build; overlay only reviewed frontend bytes.
shutil.copytree(str(old),str(target/'source'))
for name in manifest['files']:
 p=target/'source'/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(str(target/'payload'/name),str(p))
print('FRONTEND_MANIFEST_AND_COMPLETE_SOURCE_READY')
PY
CURRENT=$(docker inspect any-auto-register --format '{{.Image}}')
BASE_TAG="any-auto-register:before-$RELEASE"
docker tag "$CURRENT" "$BASE_TAG"
docker build --network=none --build-arg "RUNTIME_IMAGE=$BASE_TAG" --build-arg "RELEASE_ID=$RELEASE" \
 -f "$TARGET/source/docker/Dockerfile.ui" -t "any-auto-register:$RELEASE" "$TARGET/source"
docker cp any-auto-register:/app/static/index.html "$BACKUP/index-before.html"
python3 - <<'PY'
import json,subprocess
from pathlib import Path
base=Path('/opt/any-auto-register');release='list-scroll-20261112T062323Z';backup=base/'backups'/release;source=base/'releases'/release/'source'
result=subprocess.check_output(['docker','compose','--project-directory',str(base),'-p','any-auto-register','-f',str(base/'docker-compose.yml'),'config','--format','json'])
compose=json.loads(result.decode());assert set(compose['services'])=={'app'}
app=compose['services']['app'];app['image']='any-auto-register:'+release;app['build']={'context':str(source),'dockerfile':'docker/Dockerfile.ui',
'args':{'RUNTIME_IMAGE':'any-auto-register:before-'+release,'RELEASE_ID':release}}
(backup/'compose-next.json').write_text(json.dumps(compose,ensure_ascii=False,indent=2))
print('DURABLE_RECREATE_COMPOSE_PREPARED_NO_RESTART')
PY
docker compose --project-directory "$BASE" -p any-auto-register -f "$BACKUP/compose-next.json" config --quiet
# Stage all hashed assets first; old index/assets remain valid for open browser tabs.
docker cp "$TARGET/payload/static/assets/." any-auto-register:/app/static/assets/
docker cp "$TARGET/payload/static/index.html" "any-auto-register:/app/static/index.$RELEASE.next"
docker cp "$TARGET/payload/UI-RELEASE.json" any-auto-register:/app/UI-RELEASE.json
# Verify staged resource bytes, then atomically replace only the HTML entry.
python3 - <<'PY'
import json,subprocess,hashlib
from pathlib import Path
base=Path('/opt/any-auto-register');release='list-scroll-20261112T062323Z';target=base/'releases'/release;backup=base/'backups'/release
manifest=json.loads((target/'manifest.json').read_text())
expected={k:v for k,v in manifest['files'].items() if k.startswith('static/') or k=='UI-RELEASE.json'}
code='''import sys,json,hashlib,os;from pathlib import Path
d=json.load(sys.stdin);release="list-scroll-20261112T062323Z"
for name,digest in d.items():
 p=Path("/app")/("static/index."+release+".next" if name=="static/index.html" else name)
 assert p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==digest,name
os.replace("/app/static/index."+release+".next","/app/static/index.html")
print("STATIC_ENTRY_ATOMICALLY_SWITCHED")
'''
p=subprocess.Popen(['docker','exec','-i','any-auto-register','python','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
out,err=p.communicate(json.dumps(expected).encode());assert p.returncode==0,'Staged UI verification failed'
print(out.decode().strip())
# Persist the corresponding image/complete source for future restart/recreate.
# On any persistence failure restore the previous entry and compose, never DB data.
compose=base/'docker-compose.yml';assert compose.resolve()==base/'docker-compose.yml'
source_link=base/'src';old=source_link.resolve()
try:
 next_compose=base/('docker-compose.next-'+release)
 assert not next_compose.exists()
 next_compose.write_bytes((backup/'compose-next.json').read_bytes());next_compose.chmod(0o600);next_compose.replace(compose)
 next=base/('src.next-'+release);assert not next.exists() and source_link.is_symlink()
 next.symlink_to(target/'source');next.replace(source_link)
except Exception:
 subprocess.check_call(['docker','cp',str(backup/'index-before.html'),'any-auto-register:/app/static/index.rollback-next'])
 subprocess.check_call(['docker','exec','any-auto-register','python','-c','import os;os.replace("/app/static/index.rollback-next","/app/static/index.html")'])
 compose.write_bytes((backup/'docker-compose.yml').read_bytes())
 if source_link.resolve()!=old:
  rollback=base/('src.restore-'+release);assert not rollback.exists();rollback.symlink_to(old);rollback.replace(source_link)
 raise
before=json.loads((backup/'container-before.json').read_text());after=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
assert after['Id']==before['Id'] and after['State']['StartedAt']==before['State']['StartedAt'] and after['RestartCount']==before['RestartCount']
result={'release':release,'container_unchanged':True,'container_id':after['Id'],'started_at':after['State']['StartedAt'],
'restart_count':after['RestartCount'],'running_image':after['Config']['Image'],'next_recreate_image':'any-auto-register:'+release,
'index_sha256':expected['static/index.html'],'asset_count':sum(k.startswith('static/assets/') for k in expected)}
(backup/'deployment.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
PY
curl --max-time 20 -s -o /dev/null -w 'public auth/status HTTP %{http_code}\n' https://sdk.yepoodai.cn/api/auth/status
printf '\nFRONTEND_DEPLOYED_WITHOUT_BACKEND_RESTART\n'
