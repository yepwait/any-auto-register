"""Create an allow-listed source/static release with no database or secrets."""
from pathlib import Path
import hashlib
import json
import tarfile
from datetime import datetime, timezone

root = Path(__file__).resolve().parents[1]
release_id = datetime.now(timezone.utc).strftime("resource-%Y%m%dT%H%M%SZ")
output_dir = root / "dist"
output_dir.mkdir(exist_ok=True)
archive = output_dir / (release_id + ".tar.gz")
if archive.exists():
    raise SystemExit("Release archive already exists; do not overwrite")
allowed = []
for directory in ("api", "core", "services", "platforms", "mail", "docker", "static", "tests", "docs"):
    for path in (root / directory).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in {"__pycache__", ".pytest_cache", "node_modules", "external_logs", ".secrets", "outputs"} for part in relative.parts):
            continue
        if path.suffix.lower() in {".pyc", ".log", ".db", ".sqlite", ".pem", ".key"} or path.name == ".env":
            continue
        if any(part.startswith(".") and part not in {".dockerignore"} for part in relative.parts):
            continue
        allowed.append(path)
for name in ("main.py", "requirements.txt", "Dockerfile", "Dockerfile.server", "docker-compose.yml", "docker-compose.server.yml", ".dockerignore"):
    allowed.append(root / name)
# Keep the complete frontend source for the next clean build, excluding local deps.
for path in (root / "frontend").rglob("*"):
    if path.is_file() and not path.is_symlink() and not any(part in {"node_modules", "dist", ".vite"} for part in path.relative_to(root).parts) and path.suffix != ".tsbuildinfo":
        allowed.append(path)
manifest = {"release_id": release_id, "files": {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(allowed))}}
with tarfile.open(archive, "w:gz") as package:
    for path in sorted(set(allowed)):
        package.add(path, arcname="payload/" + path.relative_to(root).as_posix(), recursive=False)
    data = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    import io
    info = tarfile.TarInfo("manifest.json")
    info.size = len(data)
    package.addfile(info, io.BytesIO(data))
print(json.dumps({"release_id": release_id, "archive": str(archive), "bytes": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": len(allowed)}, ensure_ascii=False))
