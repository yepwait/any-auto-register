set -eu
BASE=/opt/any-auto-register
RELEASE=ui-reliable-20261112T051329Z
docker exec -i any-auto-register python - --idle < "$BASE/incoming/$RELEASE.verify.py"
