set -eu
BASE=/opt/any-auto-register
RELEASE=resource-20261111T132254Z
# Resume only verification of the already-created isolated container.
python3 - <<'PY'
import subprocess,time
for attempt in range(30):
    result=subprocess.run(['docker','exec','any-auto-register-resource-preflight','python','-c',
        'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/api/auth/status",timeout=2); print(r.status)'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
    if result.returncode==0 and result.stdout.strip()=='200':
        print('ISOLATED_PREFLIGHT_HEALTH_OK');break
    time.sleep(1)
else:
    raise SystemExit('Preflight health failed; production untouched')
PY
printf '\n--- Preflight memory ---\n'
docker stats --no-stream any-auto-register-resource-preflight
printf '\n--- Isolated process list ---\n'
docker top any-auto-register-resource-preflight -eo pid,ppid,rss,comm
printf '\n--- Current app Caddy hostnames/upstreams only ---\n'
python3 - <<'PY'
import subprocess,json
p=subprocess.run(['docker','exec','caddy','caddy','adapt','--config','/etc/caddy/Caddyfile','--adapter','caddyfile'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
if p.returncode==0:
    try:
        data=json.loads(p.stdout)
        def visit(node,hosts=()):
            if isinstance(node,dict):
                for item in node.get('match') or []:
                    if isinstance(item,dict) and item.get('host'):hosts=tuple(item['host'])
                for item in node.get('upstreams') or []:
                    dial=str(item.get('dial') or '')
                    if 'any-auto-register' in dial or ':18000' in dial: print(json.dumps({'hosts':hosts,'upstream':dial}))
                for value in node.values():visit(value,hosts)
            elif isinstance(node,list):
                for value in node:visit(value,hosts)
        visit(data)
    except Exception as exc: print('Caddy route parse:',type(exc).__name__)
else:print('Caddy adapter route not available; use local upstream health')
PY
