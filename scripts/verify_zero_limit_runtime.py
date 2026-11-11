"""Read-only deployed validation + mock zero-limit semantics, no real signup/SMS."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch
import requests

c = sqlite3.connect('file:/runtime/account_manager.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row
config = dict(c.execute('select key,value from configs'))
result = {
    'accounts': c.execute('select count(*) from accounts').fetchone()[0],
    'saved_daily_limit': config.get('chatgpt_web_pool_daily_registration_limit'),
    'active_tasks': c.execute("select count(*) from task_runs where status in ('pending','running')").fetchone()[0],
    'quick_check': c.execute('pragma quick_check').fetchone()[0],
}
headers = {}
if config.get('auth_password_hash'):
    secret = os.getenv('APP_JWT_SECRET') or config.get('auth_jwt_secret')
    if not secret:
        raise SystemExit('Missing configured signing secret')
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b'=').decode()
    header, payload = encode({'alg':'HS256','typ':'JWT'}), encode({'sub':'admin','exp':int(time.time())+180,'iat':int(time.time())})
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(),f'{header}.{payload}'.encode(),hashlib.sha256).digest()).rstrip(b'=').decode()
    headers['Authorization'] = f'Bearer {header}.{payload}.{sig}'
session = requests.Session()
session.trust_env = False
for endpoint in ('/api/chatgpt-web-pool/config','/api/chatgpt-web-pool/status','/api/tasks/resources'):
    r = session.get('http://127.0.0.1:8000'+endpoint,headers=headers,timeout=20)
    r.raise_for_status()
    data = r.json()
    if endpoint.endswith('/config'):
        assert data['daily_registration_limit'] == int(config.get('chatgpt_web_pool_daily_registration_limit','20'))
    if endpoint.endswith('/status'):
        assert 'daily_registration_unlimited' in data
        expected = int(config.get('chatgpt_web_pool_daily_registration_limit','20')) == 0
        assert data['daily_registration_unlimited'] == expected
        if expected: assert data['daily_registrations_remaining'] is None
    result[endpoint] = {'http':r.status_code}
# API accepts 0 in model; do not PUT it to the live configuration.
from api.chatgpt_web_pool import ConfigUpdate, get_status
from services import chatgpt_web_pool as pool
candidate = ConfigUpdate(config=pool.WebPoolSettings(daily_registration_limit=0))
assert candidate.config.daily_registration_limit == 0
saved = {pool.PREFIX+'budget_date':pool.today(),pool.PREFIX+'budget_used':'5000',pool.PREFIX+'daily_registration_limit':'0'}
with patch.object(pool.config_store,'get',side_effect=lambda key,default='':saved.get(key,default)), patch.object(pool.config_store,'set_many',side_effect=saved.update):
    for _ in range(3): assert pool.reserve_registration(candidate.config)
    assert pool.daily_used() == 5003
    unlimited_status = get_status()
    assert unlimited_status['daily_registration_unlimited'] is True
    assert unlimited_status['daily_registrations_remaining'] is None
result['zero_limit_model_budget_status'] = 'PASS (mock only; persisted settings untouched)'
index = Path('/app/static/index.html').read_text()
assert 'index-DOI_liSL.js' in index
web_asset = Path('/app/static/assets/ChatgptWebPool-CuH9R645.js')
assert web_asset.is_file() and '每日不限' in web_asset.read_text()
result['frontend_zero_limit_asset'] = 'present'
c.close()
print(json.dumps(result,ensure_ascii=False))
