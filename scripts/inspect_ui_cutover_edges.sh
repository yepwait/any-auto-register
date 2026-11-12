set -eu
printf '\n--- Target proxy route and in-container key paths (metadata only) ---\n'
python3 - <<'PY'
import json,subprocess
p=subprocess.Popen(['docker','exec','caddy','wget','-qO-','http://127.0.0.1:2019/config/'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
out,err=p.communicate()
if p.returncode: print('CADDY_ADMIN_UNAVAILABLE',p.returncode)
else:
 d=json.loads(out.decode())
 for name,server in d.get('apps',{}).get('http',{}).get('servers',{}).items():
  def visit(node):
   if isinstance(node,dict):
    hosts=[h for m in node.get('match',[]) for h in m.get('host',[])]
    if 'sdk.yepoodai.cn' in hosts:
     upstreams=[]
     def ups(x):
      if isinstance(x,dict):
       upstreams.extend([v.get('dial') for v in x.get('upstreams',[])])
       for v in x.values():ups(v)
      elif isinstance(x,list):
       for v in x:ups(v)
     ups(node); print(json.dumps({'server':name,'listen':server.get('listen'),'target_hosts':hosts,'upstreams':upstreams}))
    for v in node.values():visit(v)
   elif isinstance(node,list):
    for v in node:visit(v)
  visit(server.get('routes',[]))
PY
docker exec -i any-auto-register python - <<'PY'
import os,json,hashlib
from pathlib import Path
for name in ('CREDENTIAL_ENCRYPTION_KEY','APP_JWT_SECRET'):
 print(name,'env_present',bool(os.getenv(name)))
for name in ('/app/.secrets','/root/.secrets'):
 p=Path(name)
 print(name, 'exists',p.exists(),'file_names',[v.name for v in p.iterdir()] if p.is_dir() else [])
PY
