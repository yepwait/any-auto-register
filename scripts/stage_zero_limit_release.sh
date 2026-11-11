set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T140633Z
ARCHIVE="$BASE/incoming/$RELEASE.tar.gz"
TARGET="$BASE/releases/$RELEASE"
BACKUP="$BASE/backups/$RELEASE"
printf '%s  %s\n' ae84d823f04f6b6285ce93d31687b2f0684d3bb4f344308080ac514f0d588cef "$ARCHIVE" | sha256sum -c -
[ ! -e "$TARGET" ] && [ ! -e "$BACKUP" ] || { echo 'Version path exists; do not overwrite'; exit 1; }
mkdir -m 700 "$TARGET" "$BACKUP" "$BACKUP/preflight-data"
cp -p "$BASE/docker-compose.yml" "$BACKUP/docker-compose.yml"
readlink -f "$BASE/src" > "$BACKUP/source-path.txt"
docker inspect any-auto-register --format '{{.Image}}' > "$BACKUP/image-id.txt"
python3 - "$ARCHIVE" "$TARGET" "$BASE/src" <<'PY'
import tarfile,sys,json,hashlib
from pathlib import Path
archive,target,old=map(Path,sys.argv[1:])
with tarfile.open(str(archive)) as package:
 for item in package.getmembers():
  path=Path(item.name)
  if path.is_absolute() or '..' in path.parts or not item.isfile() or item.issym() or item.islnk():raise SystemExit('Unsafe archive item')
  if item.name!='manifest.json' and not item.name.startswith('payload/'):raise SystemExit('Unexpected release path')
 package.extractall(str(target))
manifest=json.loads((target/'manifest.json').read_text())
changed=[]
for name,digest in manifest['files'].items():
 new=(target/'payload'/name)
 if hashlib.sha256(new.read_bytes()).hexdigest()!=digest:raise SystemExit('Release checksum mismatch: '+name)
 previous=old/name
 if previous.is_file() and hashlib.sha256(previous.read_bytes()).hexdigest()!=digest:changed.append(name)
# No unrelated business changes are permitted in this small feature release.
allowed={'services/chatgpt_web_pool.py','api/chatgpt_web_pool.py','tests/test_chatgpt_web_pool.py','docs/CHATGPT_WEB_POOL.md','frontend/src/pages/ChatgptWebPool.tsx'}
unexpected=[name for name in changed if name not in allowed and not name.startswith('static/') and name!='frontend/tsconfig.app.tsbuildinfo']
if unexpected:raise SystemExit('Unexpected source changes, review required: '+str(unexpected))
print('RELEASE_MANIFEST_OK',len(manifest['files']),'files; changed source:',[n for n in changed if not n.startswith('static/')])
PY
CURRENT=$(cat "$BACKUP/image-id.txt")
docker tag "$CURRENT" "any-auto-register:before-zero-limit-20261111"
docker build --network=none --build-arg RUNTIME_IMAGE=any-auto-register:before-zero-limit-20261111 \
 -f "$TARGET/payload/docker/Dockerfile.deploy" -t "any-auto-register:$RELEASE" "$TARGET/payload"
# Snapshot live DB safely with the container Python (host Python is too old).
docker exec -i any-auto-register python - "$RELEASE" <<'PY'
import sqlite3,sys
from pathlib import Path
backup=Path('/runtime')/(sys.argv[1]+'-preflight.sqlite')
if backup.exists():raise SystemExit('Refuse to overwrite preflight database')
source=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
target=sqlite3.connect(str(backup));source.backup(target)
assert target.execute('pragma quick_check').fetchone()[0]=='ok'
source.close();target.close()
print('LIVE_SAFE_DB_SNAPSHOT_OK')
PY
cp -p "$BASE/data/$RELEASE-preflight.sqlite" "$BACKUP/preflight-data/account_manager.db"
[ ! -d "$BASE/data/.secrets" ] || cp -a "$BASE/data/.secrets" "$BACKUP/preflight-data/.secrets"
docker run -d --name any-auto-register-zero-limit-preflight --init --network none --memory 512m --memory-swap 768m --cpus 0.5 \
 -e APP_ENABLE_SOLVER=0 -e APP_ENABLE_SCHEDULER=0 -e APP_TASK_WORKERS=1 \
 -e APP_RUNTIME_DIR=/runtime -e DATABASE_URL=sqlite:////runtime/account_manager.db \
 -e CREDENTIAL_ENCRYPTION_KEY_FILE=/runtime/.secrets/credential_key \
 -v "$BACKUP/preflight-data:/runtime" "any-auto-register:$RELEASE"
python3 - <<'PY'
import subprocess,time
for attempt in range(30):
 p=subprocess.run(['docker','exec','any-auto-register-zero-limit-preflight','python','-c','import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8000/api/auth/status",timeout=2).status)'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 if p.returncode==0 and p.stdout.strip()=='200':print('ISOLATED_HEALTH_OK');break
 time.sleep(1)
else:raise SystemExit('Isolated health failed; production untouched')
PY
docker exec -i any-auto-register-zero-limit-preflight python - < "$BASE/incoming/verify_zero_limit_runtime.py"
printf '\nSTAGING_AND_ZERO_LIMIT_PREFLIGHT_OK\n'
