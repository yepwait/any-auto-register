"""Temporarily gate writes only for this app's exact hostname in live Caddy config.
Runs on the deployment host. Saves the original config privately for restoration;
never prints the config or changes the persisted Caddyfile/other routes.
"""
import argparse,json,subprocess
from pathlib import Path

parser=argparse.ArgumentParser();parser.add_argument('action',choices=['enable','restore']);args=parser.parse_args()
backup=Path('/opt/any-auto-register/backups/ui-reliable-20261112T051329Z/attempt-2/caddy-before.json')

def admin(method,path,data=None):
    code='import urllib.request,sys;d=sys.stdin.buffer.read();r=urllib.request.Request("http://127.0.0.1:2019'+path+'",data=d if d else None,method="'+method+'",headers={"Content-Type":"application/json"});sys.stdout.buffer.write(urllib.request.urlopen(r,timeout=15).read())'
    # Existing caddy image has wget, not Python; use a host-network helper with exact
    # runtime image, entering Caddy's network namespace and never mounting data.
    command=['docker','run','--rm','-i','--network','container:caddy','--entrypoint','python',
        'any-auto-register:ui-reliable-20261112T051329Z','-c',code]
    result=subprocess.run(command,input=data or b'',stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode: raise RuntimeError('Caddy admin request failed (details suppressed)')
    return result.stdout

if args.action=='enable':
    assert not backup.exists(),'Gate backup already exists; restore/review before reuse'
    original=admin('GET','/config/')
    data=json.loads(original)
    backup.write_bytes(original);backup.chmod(0o600)
    target=None
    for name,server in data.get('apps',{}).get('http',{}).get('servers',{}).items():
        def hosts(node):
            if isinstance(node,dict):
                if any('sdk.yepoodai.cn' in m.get('host',[]) for m in node.get('match',[])):return True
                return any(hosts(v) for v in node.values())
            return any(hosts(v) for v in node) if isinstance(node,list) else False
        if hosts(server.get('routes',[])):
            assert target is None,'Multiple app servers require review';target=server
    assert target is not None,'No app route found'
    target['routes'].insert(0,{'@id':'ui-release-write-gate','match':[{'host':['sdk.yepoodai.cn'],
        'not':[{'method':['GET','HEAD','OPTIONS']}]}],
        'handle':[{'handler':'static_response','status_code':503,'headers':{'Retry-After':['60'],'Content-Type':['application/json']},
        'body':'{"detail":"Deployment maintenance; retry shortly"}'}],'terminal':True})
    admin('POST','/load',json.dumps(data).encode())
    print('APP_WRITE_GATE_ENABLED_OTHER_HOSTS_UNCHANGED')
else:
    current=json.loads(admin('GET','/config/'))
    removed=0
    for server in current.get('apps',{}).get('http',{}).get('servers',{}).values():
        before=server.get('routes',[])
        after=[r for r in before if r.get('@id')!='ui-release-write-gate']
        removed+=len(before)-len(after);server['routes']=after
    if not removed:
        print('APP_WRITE_GATE_ALREADY_ABSENT')
    else:
        assert removed==1,'Unexpected gate count'
        admin('POST','/load',json.dumps(current).encode())
        confirmed=json.loads(admin('GET','/config/'))
        assert not any(r.get('@id')=='ui-release-write-gate' for server in confirmed.get('apps',{}).get('http',{}).get('servers',{}).values() for r in server.get('routes',[])),'Gate still present'
        print('APP_WRITE_GATE_REMOVED_OTHER_ROUTES_UNCHANGED')
