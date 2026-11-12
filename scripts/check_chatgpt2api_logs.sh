set -eu
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep chatgpt
docker logs --tail=20 chatgpt2api-yukkcat 2>&1
sleep 15
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/health?format=json)
echo "HEALTH HTTP $code"
docker ps --format '{{.Names}}\t{{.Status}}' | grep chatgpt
