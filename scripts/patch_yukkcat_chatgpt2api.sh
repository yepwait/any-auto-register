set -eu
CONTAINER=chatgpt2api-yukkcat
VOLUME_DIR=/var/lib/docker/volumes/chatgpt2api-yukkcat_chatgpt2api-runtime/_data
FILE="$VOLUME_DIR/services/account_service.py"

echo "=== Check already patched ==="
grep -c '_fire_replenish_webhook' "$FILE" 2>/dev/null && echo ALREADY_PATCHED && exit 0 || echo NOT_YET_PATCHED

echo "=== Backup ==="
cp -p "$FILE" "${FILE}.bak-webhook-$(date +%Y%m%d)"
echo BACKUP_OK

echo "=== Apply patch ==="
# After the existing "saved = self._save_accounts(...)" block but before the final return,
# read total quota and fire webhook. We inject the helper method and the call.
python3 - "$FILE" <<'PY'
import sys, re
from pathlib import Path
src = Path(sys.argv[1]).read_text()

# 1. Add the helper method just before mark_image_result def
helper = '''
    def _fire_replenish_webhook(self, total_quota: int) -> None:
        """Non-blocking quota-low webhook to any-auto-register. Never raises."""
        try:
            from services.config import config
            url = str(getattr(config, "data", {}).get("replenish_webhook_url") or "").strip()
            if not url:
                return
            threshold = int(getattr(config, "data", {}).get("replenish_quota_threshold") or 0)
            if threshold <= 0 or total_quota >= threshold:
                return
            secret = str(getattr(config, "data", {}).get("replenish_webhook_secret") or "").strip()
        except Exception:
            return
        import threading, json as _json, urllib.request as _req
        def _post():
            try:
                hdrs = {"Content-Type": "application/json"}
                if secret:
                    hdrs["X-Replenish-Secret"] = secret
                body = _json.dumps({"total_quota": total_quota, "threshold": threshold}).encode()
                r = _req.Request(url, data=body, headers=hdrs, method="POST")
                _req.urlopen(r, timeout=10)
            except Exception:
                pass
        threading.Thread(target=_post, daemon=True).start()

    def _get_total_quota(self) -> int:
        """Sum of quota for all 正常 accounts. Must be called with self._image_slot_condition held."""
        return sum(max(0, int(a.get("quota") or 0)) for a in self._accounts.values() if a.get("status") == "正常")

'''

# Insert helper before mark_image_result
marker = "    def mark_image_result("
if "_fire_replenish_webhook" in src:
    print("already patched")
    raise SystemExit(0)
if marker not in src:
    raise SystemExit("marker not found: " + marker)
src = src.replace(marker, helper + marker, 1)

# 2. After the _save_accounts call inside mark_image_result, add webhook fire
# Target: the "access_token = self._resolve_access_token_locked(access_token)" line after saved check
old_block = """                saved = self._save_accounts(
                    expected_credential_generation=expected_generation,
                )
                if not saved:
                    should_verify_after_failure = False
                    return None
                access_token = self._resolve_access_token_locked(access_token)"""
new_block = """                saved = self._save_accounts(
                    expected_credential_generation=expected_generation,
                )
                if not saved:
                    should_verify_after_failure = False
                    return None
                # Fire webhook if quota dropped below threshold (non-blocking, never raises).
                if consumed_quota and not image_quota_unknown:
                    _total_quota = self._get_total_quota()
                else:
                    _total_quota = None
                access_token = self._resolve_access_token_locked(access_token)"""
if old_block not in src:
    raise SystemExit("save_accounts block not found, patch aborted")
src = src.replace(old_block, new_block, 1)

# 3. After "result = dict(persisted)" and outside the with block, fire the webhook
# We inject it after "return result" is not right - we need it after the lock is released
# The result = dict(persisted) is the last thing inside finally: notify_all()
# Then outside the with block: should_verify_after_failure check, then return result
# We add webhook fire between lock release and return
old_return = """        if should_verify_after_failure:
            scheduled = self._schedule_account_refresh_after_image_failure(
                access_token,
                force=True,
            )
            if not scheduled:
                self._record_remote_check_error(
                    access_token,
                    "image_failure",
                    "Account verification could not be scheduled.",
                )
        return result"""
new_return = """        if should_verify_after_failure:
            scheduled = self._schedule_account_refresh_after_image_failure(
                access_token,
                force=True,
            )
            if not scheduled:
                self._record_remote_check_error(
                    access_token,
                    "image_failure",
                    "Account verification could not be scheduled.",
                )
        if _total_quota is not None:
            self._fire_replenish_webhook(_total_quota)
        return result"""
if old_return not in src:
    raise SystemExit("return block not found, patch aborted")
src = src.replace(old_return, new_return, 1)

Path(sys.argv[1]).write_text(src)
print("PATCH_APPLIED_OK")
PY

echo "=== Verify patch ==="
grep -n '_fire_replenish_webhook\|_get_total_quota\|_total_quota' "$FILE"
grep -c '_fire_replenish_webhook' "$FILE"

echo "=== Restart container to load new code ==="
docker restart $CONTAINER
python3 - <<'PYHEALTH'
import subprocess, time
for i in range(45):
 p=subprocess.run(['curl','--max-time','3','-s','-o','/dev/null','-w','%{http_code}','http://127.0.0.1:3000/health?format=json'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 if p.returncode==0 and p.stdout=='200':print('CHATGPT2API_HEALTH_OK');break
 time.sleep(1)
else:raise SystemExit('health check failed')
PYHEALTH

echo "=== Write replenish config to config.json ==="
docker exec -i $CONTAINER python3 - <<'PY'
import json
from pathlib import Path
import secrets

cfg_path = Path('/app/config.json')
raw = cfg_path.read_bytes()
# Handle BOM
if raw.startswith(b'\xef\xbb\xbf'):
    raw = raw[3:]
cfg = json.loads(raw.decode('utf-8'))

existing_secret = str(cfg.get('replenish_webhook_secret') or '').strip()
if not existing_secret:
    existing_secret = secrets.token_urlsafe(24)
    print('SECRET_GENERATED')
else:
    print('SECRET_KEPT')

cfg['replenish_webhook_url'] = 'http://any-auto-register:8000/api/chatgpt-web-pool/trigger'
cfg['replenish_quota_threshold'] = 2300
cfg['replenish_webhook_secret'] = existing_secret

cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
Path('/tmp/replenish_secret.txt').write_text(existing_secret)
print('webhook_url:', cfg['replenish_webhook_url'])
print('threshold:', cfg['replenish_quota_threshold'])
print('CONFIG_WRITTEN')
PY

echo "=== Sync secret to any-auto-register ==="
SECRET=$(docker exec $CONTAINER cat /tmp/replenish_secret.txt)
docker exec -i any-auto-register python3 - "$SECRET" <<'PY'
import sys, sqlite3
c = sqlite3.connect('/runtime/account_manager.db', timeout=15)
secret = sys.argv[1]
c.execute("insert into configs(key,value) values('chatgpt_web_pool_trigger_secret',?) on conflict(key) do update set value=excluded.value", (secret,))
c.commit()
verify = c.execute("select value from configs where key='chatgpt_web_pool_trigger_secret'").fetchone()
print('SECRET_STORED len=%d' % len(verify[0]))
c.close()
PY

echo "=== E2E test: trigger endpoint with correct secret ==="
SECRET2=$(docker exec $CONTAINER cat /tmp/replenish_secret.txt)
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18000/api/chatgpt-web-pool/trigger \
  -H 'Content-Type: application/json' \
  -H "X-Replenish-Secret: $SECRET2" -d '{}')
body=$(curl -s -X POST http://127.0.0.1:18000/api/chatgpt-web-pool/trigger \
  -H 'Content-Type: application/json' \
  -H "X-Replenish-Secret: $SECRET2" -d '{}')
echo "TRIGGER HTTP $code: $body"
[ "$code" = "200" ] || { echo E2E_FAILED; exit 1; }

echo "=== Wrong secret test ==="
bad=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18000/api/chatgpt-web-pool/trigger \
  -H 'Content-Type: application/json' -H 'X-Replenish-Secret: wrong' -d '{}')
echo "WRONG SECRET HTTP $bad (expect 401)"
[ "$bad" = "401" ] || { echo WRONG_SECRET_NOT_BLOCKED; exit 1; }

echo "YUKKCAT_WEBHOOK_LINKUP_OK"
