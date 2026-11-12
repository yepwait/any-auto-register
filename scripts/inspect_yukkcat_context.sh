set -eu
CONTAINER=chatgpt2api-yukkcat
echo "=== mark_image_result context ==="
docker exec $CONTAINER sed -n '4044,4140p' /app/services/account_service.py
echo "=== runtime volume path on host ==="
docker inspect $CONTAINER --format '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}'
