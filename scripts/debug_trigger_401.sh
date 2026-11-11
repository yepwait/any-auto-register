set -eu
# Check which version is running
readlink -f /opt/any-auto-register/src
docker inspect any-auto-register --format '{{.Image}}'
# Test trigger endpoint with no secret (should be 200 with triggered:false/true, not 401)
result=$(curl -s -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:18000/api/chatgpt-web-pool/trigger)
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:18000/api/chatgpt-web-pool/trigger)
printf 'HTTP %s response: %s\n' "$code" "$result"
# Test with global auth header to check if main app auth is blocking it
printf '\n--- Check if panel password is set ---\n'
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
has_pw=bool(dict(c.execute("select key,value from configs where key='auth_password_hash'")).get('auth_password_hash',''))
print('has_panel_password:',has_pw)
print('chatgpt_web_pool_enabled:',dict(c.execute("select key,value from configs where key='chatgpt_web_pool_enabled'")).get('chatgpt_web_pool_enabled'))
c.close()
PY
