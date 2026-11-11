set -eu
printf '\n--- Effective compose (non-secret resource fields only) ---\n'
python3 - <<'PY'
from pathlib import Path
p=Path('/opt/any-auto-register/docker-compose.yml')
print('compose:', p.resolve())
for line in p.read_text().splitlines():
    if any(key in line for key in ('image:', 'build:', 'context:', 'dockerfile:', 'command:', 'entrypoint:', 'mem_limit:', 'memswap_limit:', 'cpus:', 'APP_ENABLE_SOLVER:', 'SOLVER_THREADS:', 'APP_RUNTIME', 'CREDENTIAL_ENCRYPTION_KEY_FILE:', 'APP_PORT_BIND', '18000:', '18889:', 'stop_grace_period:')):
        print(line)
print('source:', Path('/opt/any-auto-register/src').resolve())
PY
printf '\n--- Source metadata ---\n'
ls -la /opt/any-auto-register/src/ | head -20
printf '\n--- Container base runtime ---\n'
docker exec any-auto-register python -c 'import sys,fastapi,pydantic,sqlmodel; print(sys.version); print(fastapi.__version__,pydantic.__version__,sqlmodel.__version__)'
docker exec any-auto-register node --version
printf '\n--- Deploy parent disk ---\n'
df -h /opt/any-auto-register
printf '\n--- App route health ---\n'
curl --max-time 10 -s -o /dev/null -w 'app /api/auth/status HTTP %{http_code}\n' http://127.0.0.1:18000/api/auth/status
