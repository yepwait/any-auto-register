set -eu
printf '\n--- Credential continuity metadata only ---\n'
docker exec -i any-auto-register python - <<'PY'
import os,sqlite3,json
from pathlib import Path
result={'key_env_configured':bool(os.getenv('CREDENTIAL_ENCRYPTION_KEY')),'configured_key_path':os.getenv('CREDENTIAL_ENCRYPTION_KEY_FILE')}
paths=[Path('/runtime/.secrets/credential_key'),Path('/app/.secrets/credential_key')]
result['key_file_exists']={str(p):p.is_file() for p in paths}
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if 'icloud_accounts' in tables:
 result['encrypted_icloud_count']=c.execute("SELECT count(*) FROM icloud_accounts WHERE credentials_cipher!=''").fetchone()[0]
c.close()
card=Path('/app/platforms/chatgpt/payment_channels/cards.db')
result['legacy_card_file_exists']=card.is_file()
result['runtime_card_file_exists']=Path('/runtime/payment_cards.db').is_file()
print(json.dumps(result))
PY
printf '\n--- Current tasks ---\n'
docker exec any-auto-register python -c 'import sqlite3,json;c=sqlite3.connect("file:/runtime/account_manager.db?mode=ro",uri=True);print(json.dumps(c.execute("select id,source,status,total,progress from task_runs where status in (\"pending\",\"running\")").fetchall()));c.close()'
