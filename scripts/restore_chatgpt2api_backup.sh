set -eu
CONTAINER=chatgpt2api-yukkcat
VOLUME_DIR=/var/lib/docker/volumes/chatgpt2api-yukkcat_chatgpt2api-runtime/_data
FILE="$VOLUME_DIR/services/account_service.py"

echo "=== Restore from backup ==="
BACKUP=$(ls -t ${FILE}.bak-webhook-* 2>/dev/null | head -1)
if [ -n "$BACKUP" ]; then
  cp -p "$BACKUP" "$FILE"
  echo "RESTORED from $BACKUP"
else
  echo "NO BACKUP FOUND - manual recovery needed"
  exit 1
fi

echo "=== Restart container ==="
docker restart $CONTAINER
sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/health?format=json)
echo "HEALTH HTTP $code"
[ "$code" = "200" ] && echo "SERVICE_RESTORED" || echo "STILL_FAILING $code"
docker ps --format '{{.Names}}\t{{.Status}}' | grep chatgpt
