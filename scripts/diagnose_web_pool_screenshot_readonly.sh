set -eu
# Strictly read-only: local SQLite metadata + remote account list GET. No config
# changes, scheduler wake-up, /check, /run, refresh, sync or upload.
docker exec -i any-auto-register python - <<'PY'
import json,sqlite3,time
from datetime import datetime,timezone
from pathlib import Path
from services.chatgpt_web_pool import WebPoolSettings,WebPoolClient,should_start_refill,PoolError
settings=WebPoolSettings.from_store()
safe=('enabled','target_mode','target_value','refill_below','interval_minutes','max_attempts_per_run','daily_registration_limit','max_consecutive_failures','delay_seconds','request_timeout','protocol')
print('CURRENT_SAVED_CONFIG',json.dumps({k:settings.model_dump()[k] for k in safe},ensure_ascii=False))
print('CURRENT_UTC',datetime.now(timezone.utc).isoformat())
for path in ('/app/RELEASE.json','/app/UI-RELEASE.json'):
 p=Path(path)
 if p.is_file():print('VERSION',p.name,json.loads(p.read_text()))
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True);c.row_factory=sqlite3.Row
rows=c.execute("select id,status,total,registered,success,progress,meta_json,created_at,updated_at from task_runs where source='chatgpt_web_pool' order by created_at desc limit 8").fetchall()
for row in rows:
 data=dict(row);meta=json.loads(data.pop('meta_json') or '{}');saved=meta.get('settings') or {}
 data['task_saved_policy']={k:saved[k] for k in safe if k in saved}
 data['trigger']=meta.get('trigger');data['reason']=str(meta.get('reason') or '')[:400]
 data['registrations']=meta.get('registrations')
 print('RECENT_WEB_TASK',json.dumps(data,ensure_ascii=False))
state=c.execute("select value from configs where key='chatgpt_web_pool_last_snapshot'").fetchone()
if state:
 value=json.loads(state[0]);print('LOCAL_CACHED_POOL',json.dumps({k:value.get(k) for k in ('protocol','checked_at','remaining_quota','quota_known','available_accounts','unknown_quota_accounts','unlimited_accounts')},ensure_ascii=False))
trigger=c.execute("select value from configs where key='chatgpt_web_pool_trigger_state'").fetchone()
if trigger:
 v=json.loads(trigger[0]);print('REALTIME_STATE',json.dumps({k:v.get(k) for k in ('pending','active_task_id','status','reason','failure_runs','last_received_at')},ensure_ascii=False))
c.close()
try:
 snapshot=WebPoolClient(settings).snapshot()
 fields=('protocol','total_accounts','normal_accounts','available_accounts','remaining_quota','known_remaining_quota','quota_known','unknown_quota_accounts','unlimited_accounts')
 print('CURRENT_REMOTE_READONLY',json.dumps({k:snapshot.get(k) for k in fields},ensure_ascii=False))
 try: print('WOULD_START_REFILL',should_start_refill(settings,snapshot))
 except PoolError as e: print('CURRENT_REFILL_DECISION',str(e))
except PoolError as e: print('CURRENT_REMOTE_READ_ERROR',str(e))
PY
