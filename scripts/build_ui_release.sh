set -eu
BASE=/opt/any-auto-register
RELEASE=ui-reliable-20261112T051329Z
TARGET="$BASE/releases/$RELEASE/payload"
BACKUP="$BASE/backups/$RELEASE"
BASE_TAG="any-auto-register:before-$RELEASE"
EXPECTED=$(cat "$BACKUP/image-id.txt")
ACTUAL=$(docker image inspect "$BASE_TAG" --format '{{.Id}}')
[ "$EXPECTED" = "$ACTUAL" ] || { echo 'Runtime base tag mismatch';exit 1; }
docker build --network=none --memory=768m --memory-swap=1g --cpu-period=100000 --cpu-quota=50000 \
 --build-arg "RUNTIME_IMAGE=$BASE_TAG" --build-arg "RELEASE_ID=$RELEASE" \
 -f "$TARGET/docker/Dockerfile.release" -t "any-auto-register:$RELEASE" "$TARGET"
docker image inspect "any-auto-register:$RELEASE" --format '{{.Id}} {{.Size}}'
docker run --rm --network none --entrypoint python "any-auto-register:$RELEASE" -c 'import hashlib,json;from pathlib import Path;print(json.loads(Path("/app/RELEASE.json").read_text()));print("STATIC_ENTRY_SHA256",hashlib.sha256(Path("/app/static/index.html").read_bytes()).hexdigest())'
printf '\nIMAGE_BUILD_OK\n'
