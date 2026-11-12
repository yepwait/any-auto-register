set -eu
VOLUME_DIR=/var/lib/docker/volumes/chatgpt2api-yukkcat_chatgpt2api-runtime/_data
FILE="$VOLUME_DIR/services/account_service.py"
BACKUP=$(ls -t ${FILE}.bak-webhook-* 2>/dev/null | head -1)

echo "=== Test patch syntax before applying ==="
# Use the backup as source, apply patch to temp file, then syntax check
cp "$BACKUP" /tmp/account_service_test.py

python3 - /tmp/account_service_test.py /tmp/account_service_patched.py <<'PY'
import sys
from pathlib import Path

src = Path(sys.argv[1]).read_text()

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
        """Sum of quota for all 正常 accounts. Must hold self._image_slot_condition."""
        return sum(max(0, int(a.get("quota") or 0)) for a in self._accounts.values() if a.get("status") == "正常")

'''

marker = "    def mark_image_result("
assert "_fire_replenish_webhook" not in src
assert marker in src, "marker not found"
src = src.replace(marker, helper + marker, 1)

old_block = """                saved = self._save_accounts(
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

# Not present on first run, build the new block from scratch:
old_clean = """                saved = self._save_accounts(
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
assert old_clean in src, "save_accounts block not found"
src = src.replace(old_clean, new_block, 1)

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
assert old_return in src, "return block not found"
src = src.replace(old_return, new_return, 1)

Path(sys.argv[2]).write_text(src)
print("PATCH_WRITTEN")
PY

echo "=== Syntax check ==="
python3 -m py_compile /tmp/account_service_patched.py && echo SYNTAX_OK || echo SYNTAX_FAIL

echo "=== Apply if syntax OK ==="
cp /tmp/account_service_patched.py "$FILE"
echo FILE_UPDATED

echo "=== Restart and check ==="
docker restart chatgpt2api-yukkcat
sleep 20
docker logs --tail=5 chatgpt2api-yukkcat 2>&1
docker ps --format '{{.Names}}\t{{.Status}}' | grep chatgpt2api-yukkcat
