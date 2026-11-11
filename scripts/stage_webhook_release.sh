set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T144935Z
ARCHIVE="$BASE/incoming/$RELEASE.tar.gz"
EXPECTED=00cae4e64bd5c6fb7f7967781cdda110cc38f2cce2bff2ffb7b2c9c4133f6a7d
printf '%s  %s\n' "$EXPECTED" "$ARCHIVE" | sha256sum -c -
TARGET="$BASE/releases/$RELEASE"
[ ! -e "$TARGET" ] || { echo 'Target exists; refusing overwrite'; exit 1; }
mkdir -m 700 "$TARGET"
python3 - "$ARCHIVE" "$TARGET" <<'PY'
import tarfile,sys,json,hashlib
from pathlib import Path
archive,target=map(Path,sys.argv[1:])
with tarfile.open(str(archive)) as pack:
 for item in pack.getmembers():
  path=Path(item.name)
  if path.is_absolute() or '..' in path.parts or not item.isfile() or item.issym() or item.islnk():raise SystemExit('Unsafe archive item')
  if item.name!='manifest.json' and not item.name.startswith('payload/'):raise SystemExit('Unexpected path')
 pack.extractall(str(target))
manifest=json.loads((target/'manifest.json').read_text())
for name,digest in manifest['files'].items():
 if hashlib.sha256((target/'payload'/name).read_bytes()).hexdigest()!=digest:raise SystemExit('Checksum mismatch: '+name)
print('MANIFEST_OK',len(manifest['files']),'files')
PY
docker build --network=none --build-arg RUNTIME_IMAGE=any-auto-register:before-zero-limit-20261111 \
 -f "$TARGET/payload/docker/Dockerfile.deploy" \
 -t "any-auto-register:$RELEASE" "$TARGET/payload"
printf '\nIMAGE_OK\n'
docker image inspect "any-auto-register:$RELEASE" --format '{{.Id}} {{.Size}}'
