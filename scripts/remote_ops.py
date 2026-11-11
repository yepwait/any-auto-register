"""Run an explicitly supplied UTF-8 shell script via the saved DevOps profile.

No key contents/passwords are read. The SSH client uses the plugin's pinned
host key and identity. This helper does not install/configure anything by itself.
"""
from pathlib import Path
import subprocess
import sys

profile = Path.home() / ".dsh-devops/ssh/configs/srv_5e286275b64d0ef6.conf"
if len(sys.argv) != 2:
    raise SystemExit("Usage: python scripts/remote_ops.py path/to/reviewed-script.sh")
script = Path(sys.argv[1]).read_text(encoding="utf-8").replace("\r\n", "\n").rstrip() + "\n"
process = subprocess.run(
    ["ssh", "-F", str(profile), "-o", "BatchMode=yes", "dsh-devops-target", "sh -s"],
    input=script.encode("utf-8"), stdout=sys.stdout, stderr=sys.stderr,
)
raise SystemExit(process.returncode)
