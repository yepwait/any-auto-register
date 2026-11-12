set -eu
CONTAINER=chatgpt2api-yukkcat
echo "=== mark_image_result / quota decrement ==="
docker exec $CONTAINER grep -n 'quota.*-\|mark_image\|image_result\|after.*image' /app/services/account_service.py 2>/dev/null | head -30 || echo "not in account_service"
echo "=== image_service quota code ==="
docker exec $CONTAINER grep -n 'quota.*-\|mark_image\|webhook\|replenish' /app/services/image_service.py 2>/dev/null | head -30 || echo "not in image_service"
echo "=== image_task_service quota code ==="
docker exec $CONTAINER grep -n 'quota.*-\|mark_image\|webhook\|replenish\|after_image\|on_complete' /app/services/image_task_service.py 2>/dev/null | head -30 || echo "not in image_task_service"
echo "=== all quota decrements project-wide ==="
docker exec $CONTAINER grep -rn 'quota.*-=\|quota.*- 1\|quota.*-1\|\.quota -= ' /app/services/ 2>/dev/null | grep -v '.pyc' | head -20 || echo none
echo "=== account_service.py line count ==="
docker exec $CONTAINER wc -l /app/services/account_service.py 2>/dev/null
