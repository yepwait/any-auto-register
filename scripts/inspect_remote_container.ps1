$sshConfig = 'C:\Users\admin\.dsh-devops\ssh\configs\srv_5e286275b64d0ef6.conf'
$remoteScript = @'
set -eu
printf '\n--- Container deployment ---\n'
docker inspect any-auto-register --format '{{json .Config.Labels}}'
docker inspect any-auto-register --format 'Memory={{.HostConfig.Memory}} MemorySwap={{.HostConfig.MemorySwap}} Restart={{.HostConfig.RestartPolicy.Name}}'
docker inspect any-auto-register --format '{{json .Mounts}}'
docker inspect any-auto-register --format '{{json .NetworkSettings.Ports}}'
printf '\n--- Resource environment only ---\n'
docker exec any-auto-register sh -c 'env | sort | grep -E "^(APP_ENABLE_SOLVER|SOLVER_THREADS|SOLVER_BROWSER_TYPE|APP_TASK_WORKERS|APP_UPLOAD_WORKERS|HOST|PORT|APP_RUNTIME_DIR|APP_RELOAD)=" || true'
printf '\n--- Container metrics ---\n'
docker stats --no-stream any-auto-register
printf '\n--- App processes ---\n'
docker top any-auto-register -eo pid,ppid,rss,comm
printf '\n--- Deployment directory entries ---\n'
ls -la /opt/any-auto-register | head -30
printf '\n--- Database summary (read only) ---\n'
docker exec -i any-auto-register python - <<'PY'
'@
$python = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'inspect_resource_deployment.py') -Raw
$remoteScript += "`n$python`nPY`n"
# Python sends literal UTF-8 LF bytes (PowerShell native pipes can append CRLF).
$inputScript = Join-Path $PSScriptRoot 'inspect_remote_container.generated.sh'
$remoteScript.Replace("`r`n", "`n") | Set-Content -LiteralPath $inputScript -Encoding utf8NoBOM -NoNewline
python (Join-Path $PSScriptRoot 'remote_ops.py') $inputScript
exit $LASTEXITCODE
