set -eu
python3 - <<'PY'
import json,subprocess
from pathlib import Path
base=Path('/opt/any-auto-register');release='ui-reliable-20261112T051329Z'
manifest=json.loads((base/'releases'/release/'manifest.json').read_text())
selected={k:v for k,v in manifest['files'].items() if k.split('/')[0] in ('api','core','services','platforms','mail','static') or k in ('main.py','requirements.txt','RELEASE.json','docker/entrypoint.server.sh')}
code='''import json,hashlib,sys;from pathlib import Path;data=json.load(sys.stdin);bad=[]
for name,digest in data.items():
 p=Path("/app")/name
 if not p.is_file():bad.append({"path":name,"missing":True});continue
 b=p.read_bytes();actual=hashlib.sha256(b).hexdigest()
 if actual!=digest:bad.append({"path":name,"expected":digest,"actual":actual,"bytes":len(b),"crlf":b.count(bytes([13,10]))})
print(json.dumps({"files":len(data),"mismatches":bad}))
'''
p=subprocess.Popen(['docker','exec','-i','any-auto-register','python','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
out,err=p.communicate(json.dumps(selected).encode())
assert p.returncode==0,'Diff helper failed'
print(out.decode())
p=base/'releases'/release/'payload/docker/entrypoint.server.sh'
print('ENTRYPOINT_RELEASE_CRLF',p.read_bytes().count(b'\r\n'))
PY
