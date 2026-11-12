set -eu
printf '\n--- Deployment identity and resources ---\n'
python3 - <<'PY'
import json,subprocess
from pathlib import Path
p=Path('/opt/any-auto-register')
print('base',str(p.resolve()))
print('source',str((p/'src').resolve()))
d=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
keys=('APP_ENABLE_SOLVER','SOLVER_THREADS','APP_TASK_WORKERS','APP_MAX_ACTIVE_TASKS','APP_UPLOAD_WORKERS','APP_LOG_QUEUE_LINES','APP_LOG_QUEUE_BYTES','APP_LOG_BATCH_LINES','APP_ENABLE_SCHEDULER','APP_RUNTIME_DIR','DATABASE_URL','CREDENTIAL_ENCRYPTION_KEY_FILE','HOST','PORT','APP_RELOAD')
env=dict(item.split('=',1) for item in d['Config']['Env'] if '=' in item)
print(json.dumps({'image_id':d['Image'],'image':d['Config']['Image'],'state':d['State']['Status'],'restart_count':d['RestartCount'],
 'started':d['State']['StartedAt'],'mounts':[{'source':v['Source'],'dest':v['Destination'],'rw':v['RW'],'type':v['Type']} for v in d['Mounts']],
 'safe_env':{k:env[k] for k in keys if k in env},'networks':list(d['NetworkSettings']['Networks']),
 'resources':{'memory':d['HostConfig']['Memory'],'swap':d['HostConfig']['MemorySwap'],'cpus':d['HostConfig']['NanoCpus']},
 'compose_labels':{k:v for k,v in d['Config'].get('Labels',{}).items() if k.startswith('com.docker.compose')}},ensure_ascii=False))
print('compose_sha256',subprocess.check_output(['sha256sum',str(p/'docker-compose.yml')]).decode().split()[0])
PY
printf '\n--- Database counters, safe settings and hashes ---\n'
docker exec -i any-auto-register python - <<'PY'
import sqlite3,json,hashlib,os
from pathlib import Path
p=Path('/runtime/account_manager.db');c=sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True)
tables={r[0] for r in c.execute("select name from sqlite_master where type='table'")}
result={'db_path':str(p),'db_bytes':p.stat().st_size,'quick_check':c.execute('pragma quick_check').fetchone()[0]}
for t in ('accounts','task_runs','task_logs','task_run_logs'):
 if t in tables:result[t+'_count']=c.execute('select count(*) from '+t).fetchone()[0]
result['active_tasks']=c.execute("select id,source,status,progress from task_runs where status in ('pending','running') limit 30").fetchall()
config=dict(c.execute('select key,value from configs'))
safe=('inspection_enabled','inspection_concurrency','inspection_interval_minutes','sms_stock_enabled','sms_stock_interval_minutes',
      'chatgpt_web_pool_enabled','chatgpt_web_pool_interval_minutes','chatgpt_web_pool_target_mode','chatgpt_web_pool_target_value','chatgpt_web_pool_refill_below',
      'chatgpt_web_pool_daily_registration_limit','chatgpt_web_pool_protocol','cpa_cleanup_enabled','default_executor')
result['safe_settings']={k:config[k] for k in safe if k in config}
result['trigger_secret_configured']=bool(config.get('chatgpt_web_pool_trigger_secret','').strip())
state=config.get('chatgpt_web_pool_trigger_state','')
if state:
 try:
  parsed=json.loads(state);result['event_state']={k:parsed.get(k) for k in ('pending','active_task_id','status','refill_latched','next_attempt_at')}
 except Exception:result['event_state']='unparseable'
result['config_rows']=len(config)
result['configs_digest']=hashlib.sha256(json.dumps(sorted(config.items()),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
rows=c.execute('select * from accounts order by id').fetchall()
result['accounts_digest']=hashlib.sha256(json.dumps(rows,ensure_ascii=False,separators=(',',':'),default=str).encode()).hexdigest()
key=Path(os.getenv('CREDENTIAL_ENCRYPTION_KEY_FILE','/runtime/.secrets/credential_key'))
result['key_present']=key.is_file()
if key.is_file(): result['key_digest']=hashlib.sha256(key.read_bytes()).hexdigest()
c.close();print(json.dumps(result,ensure_ascii=False))
PY
printf '\n--- Runtime storage directories (names only) ---\n'
docker exec -i any-auto-register python - <<'PY'
from pathlib import Path
import json
for d in ('/runtime','/runtime/.secrets','/app/platforms/chatgpt/payment_channels','/app/services'):
 p=Path(d)
 print(d,json.dumps([{'name':x.name,'is_dir':x.is_dir(),'bytes':x.stat().st_size if x.is_file() else None} for x in p.iterdir() if d!='/app/services' or x.name.endswith('.db') or 'external' in x.name],ensure_ascii=False) if p.is_dir() else 'not present')
PY
printf '\n--- Compose structure (safe fields) ---\n'
python3 - <<'PY'
from pathlib import Path
for line in Path('/opt/any-auto-register/docker-compose.yml').read_text().splitlines():
 if any(k in line for k in ('image:','build:','context:','dockerfile:','container_name:','mem_limit:','memswap_limit:','cpus:','18000:','stop_grace_period:','APP_ENABLE_SCHEDULER:','APP_ENABLE_SOLVER:','APP_RUNTIME_DIR:')):print(line)
PY
printf '\n--- Runtime versions ---\n'
docker exec any-auto-register python -c 'import sys,fastapi,pydantic,sqlmodel;print(sys.version.split()[0],fastapi.__version__,pydantic.__version__,sqlmodel.__version__)'
docker exec any-auto-register node --version
printf '\n--- Capacity ---\n'
df -h /opt/any-auto-register
free -m
docker stats --no-stream any-auto-register
printf '\n--- Existing site GET health ---\n'
curl --max-time 15 -s -o /dev/null -w 'local auth/status HTTP %{http_code}\n' http://127.0.0.1:18000/api/auth/status
curl --max-time 20 -s -o /dev/null -w 'public auth/status HTTP %{http_code}\n' https://sdk.yepoodai.cn/api/auth/status
