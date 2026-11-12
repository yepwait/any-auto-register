"""Reviewed host-side cutover, no database flag modifications.
Waits for existing work to finish, gates public writes, backs up quiescent data,
validates new image with scheduler=0, then restores original scheduler settings.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE=Path('/opt/any-auto-register')
RELEASE='ui-reliable-20261112T051329Z'
BACKUP=BASE/'backups'/RELEASE/'attempt-2'
TARGET=BASE/'releases'/RELEASE/'payload'
CONTAINER='any-auto-register'
IMAGE='any-auto-register:'+RELEASE
EXPECTED_INDEX='54b94678613e7d47f18dab05a51b235893cc8843a677cfdc3a18b0d669367196'
VERIFY=BASE/'incoming'/(RELEASE+'.verify.py')
GATE=BASE/'incoming'/(RELEASE+'.gate.py')
os.umask(0o077)
assert BASE.resolve()==Path('/opt/any-auto-register')
assert TARGET.resolve()==Path('/opt/any-auto-register/releases/ui-reliable-20261112T051329Z/payload')
assert (BASE/'src').is_symlink()
assert BACKUP.is_dir() and VERIFY.is_file() and GATE.is_file()
assert not (BACKUP/'cutover-started.json').exists(),'Cutover already attempted; review before retry'


def run(command, input=None, check=True):
    result=subprocess.run(command,input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if check and result.returncode:
        # Avoid leaking full effective compose/environment or request secrets.
        raise RuntimeError('Command failed ('+str(result.returncode)+'): '+str(command[:3]))
    return result


def compose(*args):
    return run(['docker','compose','--project-directory',str(BASE),'-p','any-auto-register','-f',str(BASE/'docker-compose.yml')]+list(args))


def inspect():
    return json.loads(run(['docker','inspect',CONTAINER]).stdout)[0]


def private_json(path,value):
    assert path.resolve().parent==BACKUP.resolve()
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2));path.chmod(0o600)


def health(timeout=60):
    for _ in range(timeout):
        result=run(['curl','--max-time','2','-s','-o','/dev/null','-w','%{http_code}','http://127.0.0.1:18000/api/auth/status'],check=False)
        if result.returncode==0 and result.stdout==b'200':return
        time.sleep(1)
    raise RuntimeError('New service health wait failed')


def verify(idle=False,compare=None):
    command=['docker','exec','-i',CONTAINER,'python','-']
    if idle: command.append('--idle')
    if compare:command.extend(['--compare',compare])
    result=run(command,input=VERIFY.read_bytes(),check=False)
    if result.returncode:
        return None,result
    # The verifier prints a separate preservation confirmation before JSON.
    data=json.loads(result.stdout.decode().strip().splitlines()[-1])
    return data,result


# Read-only prepare: same resources/networks/mounts/environment, only image/build differ.
original=json.loads((BACKUP/'container-before.json').read_text())
assert inspect()['Image']==original['Image'],'Production image changed since staging'
effective=json.loads(compose('config','--format','json').stdout.decode())
assert set(effective['services'])=={'app'},'Unexpected service topology'
final=json.loads(json.dumps(effective))
service=final['services']['app'];service['image']=IMAGE
service['build']={'context':str(TARGET),'dockerfile':'docker/Dockerfile.release',
    'args':{'RUNTIME_IMAGE':'any-auto-register:before-'+RELEASE,'RELEASE_ID':RELEASE}}
maintenance=json.loads(json.dumps(final))
maintenance['services']['app'].setdefault('environment',{})['APP_ENABLE_SCHEDULER']='0'
private_json(BACKUP/'final-compose.json',final)
private_json(BACKUP/'maintenance-compose.json',maintenance)
for name in ('final-compose.json','maintenance-compose.json'):
    run(['docker','compose','--project-directory',str(BASE),'-p','any-auto-register','-f',str(BACKUP/name),'config','--quiet'])
print('CANDIDATE_COMPOSE_VALIDATED_ORIGINAL_FLAGS_PRESERVED',flush=True)

# Wait for tasks and queues, not a fixed timed restart. No writes or new work.
deadline=time.monotonic()+3600
while True:
    data,result=verify(idle=True)
    if data is not None:
        if (data.get('web_realtime') or {}).get('pending') or (data.get('web_realtime') or {}).get('active_task_id'):
            raise RuntimeError('Realtime pending work needs review before cutover')
        private_json(BACKUP/'idle-before-gate.json',data)
        print('PRODUCTION_TASKS_AND_QUEUES_IDLE',flush=True)
        break
    error=result.stderr.decode(errors='replace').strip().splitlines()
    if not error or not ('Not idle:' in error[-1] or 'not idle' in error[-1] or 'Automatic task active:' in error[-1]):
        print('IDLE_VERIFY_FAILED',error[-1:] if error else 'no diagnostic',flush=True)
        raise RuntimeError('Pre-cutover verifier error; production untouched')
    if time.monotonic()>=deadline:
        raise RuntimeError('Existing task did not finish within one hour; no cutover performed')
    print('WAITING_FOR_EXISTING_TASK_NO_INTERRUPTION',time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),error[-1],flush=True)
    time.sleep(30)

stage='ready';gated=False;old_stopped=False;final_automation=False
try:
    gated=True  # /load may succeed even if the client loses its response.
    gate=run(['python3',str(GATE),'enable'])
    print(gate.stdout.decode().strip(),flush=True)
    # Recheck after write gating. A scheduler race here aborts without stopping work.
    data,result=verify(idle=True)
    if data is None:raise RuntimeError('Task started during cutover admission check; old service remains running')
    private_json(BACKUP/'idle-at-cutover.json',data)
    private_json(BACKUP/'cutover-started.json',{'release':RELEASE,'epoch':int(time.time()),'previous_image':original['Image']})
    old_ids=set(row[0] for row in data.get('db_active',[]));assert not old_ids
    compose('stop','-t','120','app');old_stopped=True;stage='old-stopped'
    stopped=inspect()
    private_json(BACKUP/'old-exit.json',{k:stopped['State'].get(k) for k in ('Status','ExitCode','OOMKilled','Error','FinishedAt')})
    # Uvicorn propagates SIGTERM as 143 on this runtime even after completing
    # lifespan cleanup. Require positive shutdown markers as well as no kill/OOM.
    logs=run(['docker','logs','--tail','100',CONTAINER],check=False)
    log_data=(logs.stdout+logs.stderr).decode(errors='replace')
    clean_markers=('Application shutdown complete.','Finished server process')
    clean=all(marker in log_data for marker in clean_markers)
    private_json(BACKUP/'shutdown-check.json',{'exit_code':stopped['State']['ExitCode'],
        'oom':stopped['State']['OOMKilled'],'lifespan_shutdown_complete':clean})
    assert stopped['State']['Status']=='exited' and stopped['State']['ExitCode'] in (0,143) and not stopped['State']['OOMKilled'] and clean,'Old service did not complete graceful shutdown'
    print('OLD_SERVICE_LIFESPAN_SHUTDOWN_VERIFIED',flush=True)
    # Quiescent byte-for-byte backup, no live SQLite copy races.
    assert (BASE/'data').resolve()==Path('/opt/any-auto-register/data')
    archive=BACKUP/'runtime-data.tar.gz';assert not archive.exists()
    run(['tar','-czf',str(archive),'-C',str(BASE),'data'])
    archive.chmod(0o600)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    (BACKUP/'runtime-data.sha256').write_text(digest+'  runtime-data.tar.gz\n')
    # Snapshot accounts/configs from the now stopped production database using the
    # same stdlib only. No imports of app modules or DB flags touched.
    snapshot_code=r'''
import sqlite3,json,hashlib,os
from pathlib import Path
c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True)
assert c.execute('pragma quick_check').fetchone()[0]=='ok'
active=c.execute("select id,source,status,progress from task_runs where status in ('pending','running')").fetchall()
assert not active,'Tasks remained after old service shutdown'
digest=lambda v:hashlib.sha256(json.dumps(v,ensure_ascii=False,separators=(',',':'),default=str).encode()).hexdigest()
config=dict(c.execute('select key,value from configs'))
p=Path('/runtime/.secrets/credential_key')
data={'configs_digest':digest(sorted(config.items())), 'configs_without_trigger_state_digest':digest(sorted((k,v) for k,v in config.items() if k!='chatgpt_web_pool_trigger_state')),
'accounts_digest':digest(c.execute('select * from accounts order by id').fetchall()),
'counts':{t:c.execute('select count(*) from '+t).fetchone()[0] for t in ('accounts','task_runs','task_logs','task_run_logs')},
'key_present':p.is_file(),'key_digest':hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None}
print(json.dumps(data));c.close()
'''
    result=run(['docker','run','--rm','-i','--network','none','--entrypoint','python','-v',str(BASE/'data')+':/runtime:ro',IMAGE,'-'],input=snapshot_code.encode())
    baseline=json.loads(result.stdout.decode());private_json(BACKUP/'quiescent-baseline.json',baseline)
    print('QUIESCENT_BACKUP_COMPLETE accounts='+str(baseline['counts']['accounts']),flush=True)
    assert (BASE/'docker-compose.yml').resolve()==BASE/'docker-compose.yml'
    (BASE/'docker-compose.yml').write_bytes((BACKUP/'maintenance-compose.json').read_bytes())
    compose('up','-d','--no-build','--force-recreate','app');stage='maintenance-started'
    health()
    # Send baseline via /tmp (never write it into the business database).
    run(['docker','cp',str(BACKUP/'quiescent-baseline.json'),CONTAINER+':/tmp/ui-before.json'])
    data,result=verify(idle=True,compare='/tmp/ui-before.json')
    if data is None:raise RuntimeError('Maintenance GET/data verification failed')
    assert data['static_entry_sha256']==EXPECTED_INDEX,'New static entry mismatch'
    assert data['release']['release_id']==RELEASE,'Wrong application code'
    private_json(BACKUP/'maintenance-verification.json',data)
    print('MAINTENANCE_PHASE_VERIFIED_ACCOUNT_CONFIG_KEY_UNCHANGED',flush=True)
    # Final step restores precisely the original scheduler env (including absence).
    (BASE/'docker-compose.yml').write_bytes((BACKUP/'final-compose.json').read_bytes())
    compose('up','-d','--no-build','--force-recreate','app');final_automation=True;stage='automation-restored'
    health()
    live=inspect();live_env=dict(v.split('=',1) for v in live['Config']['Env'] if '=' in v)
    old_env=dict(v.split('=',1) for v in original['Config']['Env'] if '=' in v)
    assert live_env.get('APP_ENABLE_SCHEDULER')==old_env.get('APP_ENABLE_SCHEDULER'),'Scheduler not restored exactly'
    for key in ('APP_ENABLE_SOLVER','APP_TASK_WORKERS','APP_MAX_ACTIVE_TASKS','APP_UPLOAD_WORKERS','DATABASE_URL','CREDENTIAL_ENCRYPTION_KEY_FILE'):
        assert live_env.get(key)==old_env.get(key),'Runtime environment changed: '+key
    mounts=lambda value:sorted((m['Source'],m['Destination'],m['RW']) for m in value['Mounts'])
    assert mounts(live)==mounts(original),'Runtime mount changed'
    assert live['HostConfig']['Memory']==original['HostConfig']['Memory'] and live['HostConfig']['NanoCpus']==original['HostConfig']['NanoCpus']
    data,result=verify()
    if data is None:raise RuntimeError('Final read-only runtime validation failed')
    assert data['static_entry_sha256']==EXPECTED_INDEX
    private_json(BACKUP/'production-verification.json',data)
    source_next=BASE/('src.next-'+RELEASE)
    assert not source_next.exists() and (BASE/'src').is_symlink()
    source_next.symlink_to(TARGET);os.replace(str(source_next),str(BASE/'src'))
    public=run(['curl','--max-time','20','-s','-o','/dev/null','-w','%{http_code}','https://sdk.yepoodai.cn/api/auth/status'])
    assert public.stdout==b'200','Public health failed'
    gate_result=run(['python3',str(GATE),'restore'])
    print(gate_result.stdout.decode().strip(),flush=True);gated=False
    private_json(BACKUP/'deployment-complete.json',{'release':RELEASE,'epoch':int(time.time()),'image_id':live['Image'],
        'old_exit':stopped['State']['ExitCode'],'static_index_sha256':EXPECTED_INDEX,'public_http':200,
        'scheduler_env_restored':True,'mounts_preserved':True,'accounts_before_automation':baseline['counts']['accounts']})
    print('DEPLOYMENT_COMPLETE '+RELEASE+' PUBLIC_HTTP_200',flush=True)
    print('BACKUP '+str(BACKUP),flush=True)
except Exception as exc:
    print('CUTOVER_FAILURE stage='+stage+' reason='+str(exc),flush=True)
    if old_stopped and not final_automation:
        # Only code/image rollback. Never restore an older database over accounts.
        (BASE/'docker-compose.yml').write_bytes((BACKUP/'docker-compose.yml').read_bytes())
        compose('up','-d','--no-build','--force-recreate','app')
        health();print('PREVIOUS_CODE_RESTORED_DATA_NOT_ROLLED_BACK',flush=True)
    elif final_automation:
        print('AUTOMATION_ALREADY_RESTORED_REQUIRE_REVIEW_BEFORE_ANY_ROLLBACK',flush=True)
    raise
finally:
    if gated:
        result=run(['python3',str(GATE),'restore'],check=False)
        if result.returncode:
            print('URGENT_WRITE_GATE_RESTORE_FAILED run python3 '+str(GATE)+' restore',flush=True)
            raise RuntimeError('Write gate restoration requires immediate review')
        else:print(result.stdout.decode().strip(),flush=True)
