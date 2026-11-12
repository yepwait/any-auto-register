set -eu
echo '=== All running containers ==='
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
echo '=== chatgpt2api-related compose dirs ==='
find /opt -maxdepth 2 -name 'docker-compose.yml' 2>/dev/null | xargs -I{} sh -c 'echo {}; grep -l chatgpt2api {} 2>/dev/null || true'
echo '=== chatgpt2api source dirs ==='
ls /opt/ 2>/dev/null
