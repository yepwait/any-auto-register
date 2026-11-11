set -eu
umask 077
BASE=/opt/any-auto-register
RELEASE=resource-20261111T132254Z
ARCHIVE="$BASE/incoming/$RELEASE.tar.gz"
EXPECTED=16f05d136d5876754dd75647b9c4cf0b40e9eaff73b7d991b4971b8d1cde720e
printf '%s  %s\n' "$EXPECTED" "$ARCHIVE" | sha256sum -c -
TARGET="$BASE/releases/$RELEASE"
[ ! -e "$TARGET" ] || { echo 'Release target already exists; refusing overwrite'; exit 1; }
mkdir -m 700 "$TARGET"
python3 - "$ARCHIVE" "$TARGET" <<'PY'
import tarfile,sys,hashlib,json
from pathlib import Path
archive,target=Path(sys.argv[1]),Path(sys.argv[2])
with tarfile.open(archive) as pack:
    for member in pack.getmembers():
        path=Path(member.name)
        if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk() or not member.isfile():
            raise SystemExit('Unsafe release entry')
        if member.name != 'manifest.json' and not member.name.startswith('payload/'):
            raise SystemExit('Unexpected archive path')
    pack.extractall(target)
manifest=json.loads((target/'manifest.json').read_text())
for name,digest in manifest['files'].items():
    data=(target/'payload'/name).read_bytes()
    if hashlib.sha256(data).hexdigest()!=digest:
        raise SystemExit('Source checksum mismatch: '+name)
print('SOURCE_MANIFEST_OK files='+str(len(manifest['files'])))
PY
# Tag the exact current image as a stable build/rollback base. Never overwrite it.
BASE_IMAGE=any-auto-register:rollback-before-resource-20261111
if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  CURRENT=$(docker inspect any-auto-register --format '{{.Image}}')
  docker tag "$CURRENT" "$BASE_IMAGE"
fi
# This build copies ~4MB and reuses the current verified runtime; no npm/pip/network.
docker build --network=none --memory=768m --memory-swap=1g --cpu-period=100000 --cpu-quota=50000 \
  --build-arg "RUNTIME_IMAGE=$BASE_IMAGE" -f "$TARGET/payload/docker/Dockerfile.deploy" \
  -t "any-auto-register:$RELEASE" "$TARGET/payload"
printf '\nSTAGED_IMAGE_OK\n'
docker image inspect "any-auto-register:$RELEASE" --format '{{.Id}} {{.Size}}'
