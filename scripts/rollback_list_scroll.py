"""Frontend-only rollback. Fixed paths; no restart or business database writes."""
import json,subprocess
from pathlib import Path
BASE=Path('/opt/any-auto-register')
RELEASE='list-scroll-20261112T062323Z'
BACKUP=BASE/'backups'/RELEASE
assert BACKUP.resolve()==Path('/opt/any-auto-register/backups/list-scroll-20261112T062323Z')
assert (BACKUP/'index-before.html').is_file() and (BACKUP/'docker-compose.yml').is_file()
old=Path((BACKUP/'source-path.txt').read_text().strip())
assert old==BASE/'releases/ui-reliable-20261112T051329Z/payload' and old.is_dir()
info=json.loads(subprocess.check_output(['docker','inspect','any-auto-register']).decode())[0]
assert info['Config']['Image']=='any-auto-register:ui-reliable-20261112T051329Z','Container has since been recreated; review rollback instead'
subprocess.check_call(['docker','cp',str(BACKUP/'index-before.html'),'any-auto-register:/app/static/index.rollback-next'])
code='import os;os.replace("/app/static/index.rollback-next","/app/static/index.html");print("OLD_STATIC_ENTRY_RESTORED")'
subprocess.check_call(['docker','exec','any-auto-register','python','-c',code])
compose=BASE/'docker-compose.yml';assert compose.resolve()==BASE/'docker-compose.yml'
compose.write_bytes((BACKUP/'docker-compose.yml').read_bytes())
assert (BASE/'src').is_symlink()
next=BASE/('src.rollback-'+RELEASE);assert not next.exists()
next.symlink_to(old);next.replace(BASE/'src')
subprocess.check_call(['docker','exec','any-auto-register','python','-c','import json;from pathlib import Path;Path("/app/UI-RELEASE.json").write_text(json.dumps({"ui_release_id":"ui-reliable-20261112T051329Z","rolled_back_from":"list-scroll-20261112T062323Z"}))'])
print('FRONTEND_ROLLED_BACK_NO_BACKEND_RESTART_NO_DATA_CHANGES')
