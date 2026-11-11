set -eu
# Find chatgpt2api container location
docker inspect chatgpt2api --format 'Image={{.Image}} Status={{.State.Status}}' 2>/dev/null || docker ps | grep chatgpt2api
docker inspect chatgpt2api --format '{{json .Mounts}}' 2>/dev/null
printf '\n--- Check if already patched ---\n'
docker exec chatgpt2api grep -c '_fire_replenish_webhook' /app/services/account_service.py 2>/dev/null && echo 'ALREADY_PATCHED' || echo 'NOT_YET_PATCHED'
