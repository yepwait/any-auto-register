set -eu
# Service is confirmed running from logs. Check port mapping.
docker inspect chatgpt2api-yukkcat --format '{{json .NetworkSettings.Ports}}'
# Find the actual host port
HOST_PORT=$(docker inspect chatgpt2api-yukkcat --format '{{range $k,$v := .NetworkSettings.Ports}}{{if $v}}{{(index $v 0).HostPort}}{{end}}{{end}}')
echo "HOST_PORT: $HOST_PORT"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HOST_PORT}/health?format=json")
echo "HEALTH HTTP $code"
# Verify backup is in place (patch reverted)
grep -c '_fire_replenish_webhook' /var/lib/docker/volumes/chatgpt2api-yukkcat_chatgpt2api-runtime/_data/services/account_service.py && echo STILL_PATCHED || echo CLEAN_BACKUP_RUNNING
