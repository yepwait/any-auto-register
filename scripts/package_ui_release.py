"""Package reviewed runtime/frontend files, never local data or secrets."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import re
import hashlib
import io
import json
import tarfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--prefix', default='ui-reliable')
args = parser.parse_args()
assert re.fullmatch(r'[a-z][a-z0-9-]{1,32}', args.prefix)
release = args.prefix + datetime.now(timezone.utc).strftime('-%Y%m%dT%H%M%SZ')
output = ROOT / 'dist'
output.mkdir(exist_ok=True)
archive = output / (release + '.tar.gz')
assert not archive.exists()
files = {}
# Use a meaningful release mtime; epoch-zero same-size frontend entries can be
# missed by fast filesystem diffing when layering over an older local image.
release_mtime = int(datetime.now(timezone.utc).timestamp())
blocked = {'__pycache__','.git','.venv','.pytest_cache','.secrets','node_modules','external_logs','outputs','dist','.vite'}
for folder in ('api','core','services','platforms','mail','static','frontend','tests','docs'):
    for path in (ROOT/folder).rglob('*'):
        relative=path.relative_to(ROOT)
        if not path.is_file() or path.is_symlink() or any(part in blocked or part.startswith('.') for part in relative.parts): continue
        if path.suffix.lower() in {'.db','.sqlite','.sqlite3','.pyc','.pyo','.log','.key','.pem','.tsbuildinfo'} or '.db-' in path.name: continue
        files[relative.as_posix()]=path.read_bytes()
for name in ('main.py','requirements.txt','docker/entrypoint.server.sh'):
    files[name]=(ROOT/name).read_bytes()
files['docker/Dockerfile.release'] = b'''ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
ARG RELEASE_ID
LABEL cn.yepoodai.release=${RELEASE_ID}
WORKDIR /app
COPY api/ /app/api/
COPY core/ /app/core/
COPY services/ /app/services/
COPY platforms/ /app/platforms/
COPY mail/ /app/mail/
COPY static/ /app/static/
COPY main.py requirements.txt /app/
COPY RELEASE.json UI-RELEASE.json /app/
COPY docker/entrypoint.server.sh /app/docker/entrypoint.server.sh
RUN sed -i 's/\\r$//' /app/docker/entrypoint.server.sh && chmod +x /app/docker/entrypoint.server.sh \\
 && node --version && python -c "import fastapi,sqlmodel,requests,curl_cffi; import api.chatgpt_web_pool, services.web_pool_events, api.payments"
ENTRYPOINT ["/app/docker/entrypoint.server.sh"]
'''
files['RELEASE.json']=json.dumps({'release_id':release,'base_commit':'54e4323','source_state':'tested-local-changes',
    'deployed_features':['admin-ui-refresh','web-pool-reliability','yukkcat-v3','credential-resync','payment-resource-guards',
                         'fixed-list-scroll','web-pool-zero-unlimited','task-time-policy','quota-blocked-outcome']},ensure_ascii=False,indent=2).encode()
files['UI-RELEASE.json']=json.dumps({'ui_release_id':release,'base_release':release,
    'change':'Web pool zero limits, saved-vs-task policy, blocked quota outcome; fixed list scrolling retained'},ensure_ascii=False,indent=2).encode()
manifest={'release_id':release,'files':{name:hashlib.sha256(data).hexdigest() for name,data in sorted(files.items())}}
with tarfile.open(archive,'w:gz') as package:
    for name,data in sorted(files.items()):
        info=tarfile.TarInfo('payload/'+name);info.size=len(data);info.mode=0o644;info.mtime=release_mtime
        package.addfile(info,io.BytesIO(data))
    data=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    info=tarfile.TarInfo('manifest.json');info.size=len(data);info.mode=0o644
    package.addfile(info,io.BytesIO(data))
result={'release_id':release,'archive':str(archive),'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
    'bytes':archive.stat().st_size,'file_count':len(files),'static_index_sha256':manifest['files']['static/index.html']}
record=output/(release+'.json');assert not record.exists()
record.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
