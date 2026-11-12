set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=ui-reliable-20261112T051329Z
ARCHIVE="$BASE/incoming/$RELEASE.tar.gz"
TARGET="$BASE/releases/$RELEASE"
BACKUP="$BASE/backups/$RELEASE"
EXPECTED=de24db05c6ddfb40d1f77f99251b6fce4b57cdd15a1243fba225b32bdc096f97
printf '%s  %s\n' "$EXPECTED" "$ARCHIVE" | sha256sum -c -
[ ! -e "$TARGET" ] && [ ! -e "$BACKUP" ] || { echo 'Release/backup exists; refusing overwrite'; exit 1; }
mkdir -m 700 "$TARGET" "$BACKUP"
python3 - "$ARCHIVE" "$TARGET" "$BACKUP" <<'PY'
import json,sys,hashlib,tarfile,subprocess
from pathlib import Path
archive,target,backup=map(Path,sys.argv[1:])
assert target.resolve()==Path('/opt/any-auto-register/releases/ui-reliable-20261112T051329Z')
with tarfile.open(str(archive)) as pack:
 for m in pack.getmembers():
  p=Path(m.name)
  assert m.isfile() and not m.issym() and not m.islnk() and not p.is_absolute() and '..' not in p.parts
  assert m.name=='manifest.json' or m.name.startswith('payload/')
 pack.extractall(str(target))
manifest=json.loads((target/'manifest.json').read_text())
for name,digest in manifest['files'].items():
 assert hashlib.sha256((target/'payload'/name).read_bytes()).hexdigest()==digest,name
print('MANIFEST_VERIFIED',len(manifest['files']))
old=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
(backup/'container-before.json').write_text(json.dumps(old))
(backup/'image-id.txt').write_text(old['Image']+'\n')
(backup/'source-path.txt').write_text(str(Path('/opt/any-auto-register/src').resolve())+'\n')
(backup/'docker-compose.yml').write_bytes(Path('/opt/any-auto-register/docker-compose.yml').read_bytes())
for name in ('container-before.json','image-id.txt','source-path.txt','docker-compose.yml'): (backup/name).chmod(0o600)
print('BASE_IMAGE',old['Image'])
PY
CURRENT=$(docker inspect any-auto-register --format '{{.Image}}')
docker tag "$CURRENT" "any-auto-register:before-$RELEASE"
docker build --network=none --memory=768m --memory-swap=1g --cpu-period=100000 --cpu-quota=50000 \
  --build-arg "RUNTIME_IMAGE=$CURRENT" --build-arg "RELEASE_ID=$RELEASE" \
  -f "$TARGET/payload/docker/Dockerfile.release" -t "any-auto-register:$RELEASE" "$TARGET/payload"
printf '\n--- Image and new static entry ---\n'
docker image inspect "any-auto-register:$RELEASE" --format '{{.Id}} {{.Size}}'
docker run --rm --network none --entrypoint python "any-auto-register:$RELEASE" -c 'import hashlib,json;from pathlib import Path;print(json.loads(Path("/app/RELEASE.json").read_text()));print("STATIC_ENTRY_SHA256",hashlib.sha256(Path("/app/static/index.html").read_bytes()).hexdigest())'
printf '\nSTAGE_OK\n'
