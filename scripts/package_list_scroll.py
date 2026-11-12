"""Allow-listed frontend-only update; does not include runtime data or secrets."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,io,json,tarfile

root=Path(__file__).resolve().parents[1]
release=datetime.now(timezone.utc).strftime('list-scroll-%Y%m%dT%H%M%SZ')
files={}
for folder in ('frontend','static'):
 for path in (root/folder).rglob('*'):
  rel=path.relative_to(root)
  if not path.is_file() or path.is_symlink() or any(p in {'node_modules','__pycache__','dist','.vite'} or p.startswith('.') for p in rel.parts):continue
  if path.suffix in {'.tsbuildinfo','.log','.db','.key','.pem'}:continue
  files[rel.as_posix()]=path.read_bytes()
files['UI-RELEASE.json']=json.dumps({'ui_release_id':release,'base_release':'ui-reliable-20261112T051329Z',
 'change':'Fixed filter/header/pagination with table-body-only list scrolling','validation':'17 multi-row scroll checks and 14 admin UI checks'},ensure_ascii=False,indent=2).encode()
files['docker/Dockerfile.ui']=b'''ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
ARG RELEASE_ID
LABEL cn.yepoodai.ui-release=${RELEASE_ID}
COPY static/ /app/static/
COPY UI-RELEASE.json /app/UI-RELEASE.json
'''
manifest={'release_id':release,'files':{k:hashlib.sha256(v).hexdigest() for k,v in sorted(files.items())}}
out=root/'dist';archive=out/(release+'.tar.gz');assert not archive.exists()
with tarfile.open(archive,'w:gz') as pack:
 for name,data in sorted(files.items()):
  m=tarfile.TarInfo('payload/'+name);m.size=len(data);m.mode=0o644;pack.addfile(m,io.BytesIO(data))
 data=json.dumps(manifest,ensure_ascii=False,indent=2).encode();m=tarfile.TarInfo('manifest.json');m.size=len(data);m.mode=0o644;pack.addfile(m,io.BytesIO(data))
record={'release_id':release,'archive':str(archive),'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
 'bytes':archive.stat().st_size,'file_count':len(files),'index_sha256':manifest['files']['static/index.html']}
(out/(release+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(record,ensure_ascii=False))
