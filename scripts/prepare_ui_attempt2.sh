set -eu
umask 077
BASE=/opt/any-auto-register
BACKUP="$BASE/backups/ui-reliable-20261112T051329Z"
[ ! -e "$BACKUP/attempt-2" ] || { echo 'Attempt 2 exists; refusing overwrite';exit 1; }
mkdir -m 700 "$BACKUP/attempt-2"
python3 - <<'PY'
import json,subprocess
from pathlib import Path
base=Path('/opt/any-auto-register');backup=base/'backups/ui-reliable-20261112T051329Z';attempt=backup/'attempt-2'
current=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
original=json.loads((backup/'container-before.json').read_text())
assert current['Image']==original['Image']
(attempt/'container-before.json').write_text(json.dumps(current))
(attempt/'docker-compose.yml').write_bytes((base/'docker-compose.yml').read_bytes())
(attempt/'source-path.txt').write_text(str((base/'src').resolve())+'\n')
for p in attempt.iterdir():p.chmod(0o600)
print('ATTEMPT2_PREPARED_ORIGINAL_VERSION_IN_SERVICE')
PY
