"""Sanitized, GET-only release verification inside the container.
Never generates secrets or calls business POST/check/refresh routes.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
import requests

parser=argparse.ArgumentParser()
parser.add_argument('--idle',action='store_true')
parser.add_argument('--compare')
parser.add_argument('--snapshot')
parser.add_argument('--allow-trigger-state-change',action='store_true')
parser.add_argument('--site',default='http://127.0.0.1:8000')
args=parser.parse_args()
connection=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
assert connection.execute('pragma quick_check').fetchone()[0]=='ok'
configs=dict(connection.execute('select key,value from configs'))

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str).encode()).hexdigest()

state={'configs_digest':digest(sorted(configs.items())),
       'configs_without_trigger_state_digest':digest(sorted((k,v) for k,v in configs.items() if k!='chatgpt_web_pool_trigger_state')),
       'accounts_digest':digest(connection.execute('select * from accounts order by id').fetchall()),
       'counts':{t:connection.execute('select count(*) from '+t).fetchone()[0] for t in ('accounts','task_runs','task_logs','task_run_logs')},
       'db_active':connection.execute("select id,source,status,progress from task_runs where status in ('pending','running')").fetchall()}
key=Path(os.getenv('CREDENTIAL_ENCRYPTION_KEY_FILE','/runtime/.secrets/credential_key'))
state['key_present']=key.is_file()
state['key_digest']=hashlib.sha256(key.read_bytes()).hexdigest() if key.is_file() else None
if args.snapshot:
    p=Path(args.snapshot);assert not p.exists(),'refuse overwrite'
    p.write_text(json.dumps(state,ensure_ascii=False,indent=2));p.chmod(0o600)
if args.compare:
    before=json.loads(Path(args.compare).read_text())
    for key in ('accounts_digest','key_present','key_digest'):
        assert state[key]==before[key], 'Data preservation mismatch: '+key
    compare_config='configs_without_trigger_state_digest' if args.allow_trigger_state_change else 'configs_digest'
    assert state[compare_config]==before[compare_config], 'Configuration preservation mismatch'
    assert state['counts']['accounts']==before['counts']['accounts'],'Account count changed'
    print('ACCOUNT_CONFIG_KEY_PRESERVATION_OK')

# config_store reads through to env/.env without writes. Do not call create_token.
from core.config_store import config_store
headers={}
if config_store.get('auth_password_hash',''):
    secret=os.getenv('APP_JWT_SECRET') or config_store.get('auth_jwt_secret','')
    assert secret,'Panel authentication enabled but no effective signing secret'
    encode=lambda value:base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    head,body=encode({'alg':'HS256','typ':'JWT'}),encode({'sub':'admin','iat':int(time.time()),'exp':int(time.time())+300})
    sig=base64.urlsafe_b64encode(hmac.new(secret.encode(),(head+'.'+body).encode(),hashlib.sha256).digest()).rstrip(b'=').decode()
    headers['Authorization']='Bearer '+head+'.'+body+'.'+sig

session=requests.Session();session.trust_env=False
results={}
for route in ('/api/auth/status','/api/accounts/stats','/api/tasks/resources','/api/tasks/page?status=active&limit=20',
              '/api/tasks/page?status=finished&limit=10','/api/inspection/status','/api/sms-stock/status','/api/chatgpt-web-pool/status','/api/chatgpt-web-pool/config'):
    before=time.monotonic();r=session.get(args.site+route,headers=headers,timeout=30)
    assert r.status_code==200, 'GET returned HTTP '+str(r.status_code)+' for '+route
    data=r.json();results[route]={'http':r.status_code,'bytes':len(r.content),'ms':round((time.monotonic()-before)*1000,1)}
    if route.endswith('/resources'):
        results['resources']=data
        if args.idle:
            for key in ('active_tasks','upload_pending','upload_queued','tasklog_pending','log_buffer_lines','log_buffer_bytes'):
                assert data.get(key)==0, 'Not idle: '+key
    if 'status=active' in route:
        results['active_total']=data['total']
        if args.idle: assert data['total']==0,'Persisted tasks not idle'
    if '/tasks/page' in route:
        assert all(not item.get('logs') and not item.get('errors') for item in data['items']),'Heavy task summary'
    if route.endswith('/status') and route not in ('/api/auth/status',):
        if args.idle: assert not data.get('active'),'Automatic task active: '+route
    if route=='/api/chatgpt-web-pool/status':
        results['web_realtime']={k:(data.get('realtime') or {}).get(k) for k in ('pending','active_task_id','status','secret_configured')}
    if route=='/api/chatgpt-web-pool/config':
        assert 'admin_token' not in data,'Secret leaked by config endpoint'
        results['web_policy']={k:data.get(k) for k in ('enabled','target_mode','target_value','refill_below','max_attempts_per_run','max_consecutive_failures','daily_registration_limit','protocol')}
if args.idle: assert not state['db_active'],'Database tasks not idle'
index=session.get(args.site+'/',timeout=20);assert index.status_code==200
results['static_entry_sha256']=hashlib.sha256(index.content).hexdigest()
release=Path('/app/RELEASE.json');results['release']=json.loads(release.read_text()) if release.is_file() else None
results.update(state)
connection.close()
print(json.dumps(results,ensure_ascii=False))
