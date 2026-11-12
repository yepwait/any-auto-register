"""Manual code/image-only rollback, never restore account data.
Run only after reviewing the fixed release/backup paths and current work.
"""
import json
import os
from pathlib import Path
import subprocess
import time

BASE=Path('/opt/any-auto-register')
RELEASE='ui-reliable-20261112T051329Z'
BACKUP=BASE/'backups'/RELEASE/'attempt-2'
VERIFY=BASE/'incoming'/(RELEASE+'.verify.py')
GATE=BASE/'incoming'/(RELEASE+'.gate.py')
assert BASE.resolve()==Path('/opt/any-auto-register')
assert BACKUP.resolve()==Path('/opt/any-auto-register/backups/ui-reliable-20261112T051329Z/attempt-2')
assert (BACKUP/'deployment-complete.json').is_file(),'No completed release; use failure recovery procedure'
old=Path((BACKUP/'source-path.txt').read_text().strip())
assert str(old.resolve()).startswith('/opt/any-auto-register/releases/') and old.name=='payload' and old.is_dir()
assert (BASE/'src').is_symlink() and (BASE/'src').resolve()==BASE/'releases'/RELEASE/'payload'

def run(args, input=None):
    result=subprocess.run(args,input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode:raise RuntimeError('Command failed: '+str(args[:3]))
    return result.stdout

def compose(*args):return run(['docker','compose','--project-directory',str(BASE),'-p','any-auto-register','-f',str(BASE/'docker-compose.yml')]+list(args))
info=json.loads(run(['docker','inspect','any-auto-register']))[0]
assert info['Config']['Image']=='any-auto-register:'+RELEASE,'Current code differs; refusing rollback'
# Reject active work rather than stopping it. Review pending event state separately.
run(['docker','exec','-i','any-auto-register','python','-','--idle'],input=VERIFY.read_bytes())
gated=True
try:
    # Gate helper restore is idempotent; an existing backup is intentionally not overwritten.
    # A new rollback admission gate must use a fresh path.
    gate_source=GATE.read_text()
    assert 'attempt-2/caddy-before.json' in gate_source
    gate_path=BACKUP/'rollback-gate.py'
    assert not gate_path.exists(),'Previous rollback attempt requires review'
    gate_path.write_text(gate_source.replace('attempt-2/caddy-before.json','attempt-2/caddy-before-rollback.json'));gate_path.chmod(0o600)
    run(['python3',str(gate_path),'enable'])
    run(['docker','exec','-i','any-auto-register','python','-','--idle'],input=VERIFY.read_bytes())
    compose('stop','-t','120','app')
    current=json.loads(run(['docker','inspect','any-auto-register']))[0]
    logs=subprocess.run(['docker','logs','--tail','100','any-auto-register'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    log=(logs.stdout+logs.stderr).decode(errors='replace')
    assert current['State']['ExitCode'] in (0,143) and not current['State']['OOMKilled'] and not current['State'].get('Error')
    assert 'Application shutdown complete.' in log and 'Finished server process' in log,'No completed lifespan shutdown'
    idle_code="import sqlite3;c=sqlite3.connect('file:/runtime/account_manager.db?mode=ro',uri=True);assert not c.execute(\"select id from task_runs where status in ('pending','running')\").fetchall();c.close()"
    run(['docker','run','--rm','--network','none','--entrypoint','python','-v',str(BASE/'data')+':/runtime:ro','any-auto-register:'+RELEASE,'-c',idle_code])
    (BASE/'docker-compose.yml').write_bytes((BACKUP/'docker-compose.yml').read_bytes())
    next_path=BASE/('src.rollback-'+RELEASE)
    assert not next_path.exists() and (BASE/'src').is_symlink()
    next_path.symlink_to(old);os.replace(str(next_path),str(BASE/'src'))
    compose('up','-d','--no-build','--force-recreate','app')
    for _ in range(60):
        result=subprocess.run(['curl','--max-time','2','-s','-o','/dev/null','-w','%{http_code}','http://127.0.0.1:18000/api/auth/status'],stdout=subprocess.PIPE)
        if result.returncode==0 and result.stdout==b'200':break
        time.sleep(1)
    else:raise RuntimeError('Rollback health not ready; manual review required')
    print('PREVIOUS_CODE_RESTORED_ACCOUNT_DATABASE_NOT_ROLLED_BACK',flush=True)
finally:
    path=BACKUP/'rollback-gate.py'
    if path.is_file():run(['python3',str(path),'restore'])
