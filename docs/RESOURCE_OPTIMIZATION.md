# Resource optimization and production deployment — 2026-11-11

## Deployment result

- Target: `43.173.92.11`, application `https://sdk.yepoodai.cn`.
- Access: existing Remote Operations plugin SSH profile; no passwords/private-key contents printed.
- Release: `resource-20261111T132254Z`.
- Image: `any-auto-register:resource-20261111T132254Z`.
- Image ID: `sha256:4f83fb04359e0322e33aa2a4525c17bfb399be352760fe7aa7cb96e12a5e7a7c`.
- Source: `/opt/any-auto-register/releases/resource-20261111T132254Z/payload`.
- Cutover backup: `/opt/any-auto-register/backups/resource-20261111T132254Z-cutover`.
  - `runtime-data.tar.gz`: stopped-app snapshot of the data directory (about 19MB compressed).
  - `docker-compose.yml`: exact previous deployment configuration.
  - `source-path.txt`, `image-id.txt`: previous release/image identity.
- Previous image preserved as `any-auto-register:rollback-before-resource-20261111`.
- Original runtime, external targets, external logs, app_net alias and localhost ports retained.
- Existing automation flags restored after temporary maintenance pause. No paid registration/SMS test was initiated by the deployment procedure.

## What changed

### Protocol runtime

- Solver defaults off; explicitly set `APP_ENABLE_SOLVER=1` to opt in.
- If opted in, default browser count is one. Solver now has bounded admission, queue/task timeouts, reliable context cleanup, tracked shutdown and bounded expiring results.
- Production uses the server entrypoint: no Xvfb/Firefox/Camoufox processes start for protocol-only tasks. Node remains installed for Sentinel.
- Sentinel timeout timers are cleared on early success; per-account VM isolation is retained.

### Task resource budgets

- `APP_TASK_WORKERS=1`: process-wide account slot shared by registration, RT/2FA batches, inspection and existing-account SMS supplementation.
- Per-request workers are clipped to the shared cap; only a C-sized window of Futures is submitted, not every account at once.
- `APP_MAX_ACTIVE_TASKS=4`: bounded top-level task admission; children do not consume a separate top-level slot.
- Request validation rejects invalid/absurd counts, concurrency and nonfinite delays.
- Uploads use one bounded worker and a queue of 16; TaskLog writes have an independent single worker. Full queues apply backpressure instead of silently dropping accepted jobs.
- Shutdown closes admission, requests cooperative stop, waits for producers, drains accepted uploads/TaskLogs and flushes append logs. Timed-out network work is best-effort; late critical TaskLogs use a synchronous path instead of becoming registration retry failures.

### Log storage and reads

- Additive `task_run_logs(task_id, sequence, line, created_at)` table stores new logs incrementally. `task_runs` gains counters/version fields; legacy JSON is preserved unchanged.
- Append buffer flushes about once a second or at 256 lines/1MiB, with terminal-state/normal shutdown flush.
- Durable log count and append lines commit atomically. Failed ordinary commits retain the append buffer for retry.
- GET/detail/SSE do not persist state. SSE uses absolute cursors, bounded pages and thread-offloaded SQLite reads; pending evicted lines remain readable before the writer tick.
- Memory tails: production 2,000 lines / approximately 512KiB per task. Finished task records are evicted from memory; full database history remains available.
- Task summaries are SQL projections with source/status filtering and pagination; they exclude log and error bodies. TaskLog totals use SQL COUNT.
- No historical task/credential records were deleted, and no VACUUM or speculative SQLite/WAL tuning was performed.

### Frontend

- Active tasks and finished history have independent server pagination.
- Log rendering batches incoming events and retains 2,000 visible lines; complete log download is explicit and paginated.
- Status polling is deduplicated, paused while hidden and cancelled on unmount. Repeated polls no longer re-fetch or overwrite editing forms.
- Pages are lazy-loaded. The main entry chunk changed from about 1.61MB to about 362KB (shared/page chunks still load on demand; this is not a claim about total JS size).

### Other fixes discovered during verification

- Legacy proxy strings are normalized before adding `http://`, preventing malformed `@//host` URLs and avoidable failed retries.
- A MailAPI backend routing test now mocks its OAuth capability probe instead of contacting a real provider during the test suite.
- Configuration reads do not parse `.env`/copy all environment variables when SQLite already contains the requested key.

## Production verification

Observed shortly before/after cutover (different snapshots, not a controlled sustained benchmark):

| Metric | Before | After with existing inspection running |
|---|---:|---:|
| Container memory | 824.4MiB | 95.06MiB |
| Docker PIDS | 344 | 9 |
| Browser instances | 4 Camoufox trees + Xvfb | none in app container |
| Host available memory | 812MiB | 1,631MiB |
| Container restarts after deployment | — | 0 |

- Container RAM limit remains 2GiB; RAM+swap limit remains 3GiB. CPU ceiling is 1 core; Docker logs rotate at 10MiB × 3.
- New task logs observed in append table; new task legacy JSON remained `[]` (2 bytes), not a growing historical blob.
- SQLite `PRAGMA quick_check`: `ok`.
- Account count: 38 before and after; existing task/history retained.
- Existing scheduled inspection completed **37/37**, producing **255** new append-log lines normally.
- Final verification snapshot: **96.86MiB**, Docker PIDS **7**, host available memory **1,615MiB**, restart count **0**, `OOMKilled=false`.
- Public main entry, task page and log component assets all returned HTTP 200. Unauthenticated access to the resources endpoint returned HTTP 401.
- Tasks/resources, task pages, old log pages, inspection/SMS/Web-pool status: HTTP 200.
- Public app status HTTP 200; gpt health 200; sub 200; cli 302 (login redirect).
- No traceback, IntegrityError or `database is locked` signatures in the new app logs during the verification window.
- No persistent credential key was present before deployment and encrypted iCloud credential count was zero; no key was replaced. The configured runtime key path is unchanged and will persist if a future feature creates it.

## Tests/build

- Full offline suite: **885 passed**.
- Resource/storage/solver/key-persistence focused suite: **104 passed**.
- TypeScript compilation and production Vite build passed using Node v24.19.0.
- Syntax and whitespace checks passed. Browser DOM/end-to-end interaction was not performed; deployed HTML/assets and API paths were checked.

## Maintenance and limitations

- The deployment image inherits the existing verified runtime to avoid pip/npm/browser downloads or long builds on a 3.6GB host. It **still contains old browser binaries on disk**; lower RSS does not mean a smaller image. A later clean `Dockerfile.server` build can reduce image disk use.
- Old log JSON remains in the database. Retention/deletion is intentionally not automatic; agree a retention policy and back up before deleting history.
- Memory/log queues are not crash-durable. A hard power loss or SIGKILL can lose the latest approximately one-second append buffer; account credentials and terminal/normal-stop results are flushed through existing durable paths.
- Background TaskLog/upload errors are recorded, not a durable retry-outbox guarantee during database/network failure.
- Limits are per Python process. Do not start multiple uvicorn workers without cross-process task coordination.
- 95MiB is an observed short-window inspection load, not a promised maximum for registration/Sentinel spikes or a multi-day leak test.

## Rollback

Use the saved compose, source link and previous image. Stop/complete active tasks first. Prefer rollback of **code/image only**, keeping the additive database schema and newly created accounts intact. Do not restore the data archive over a newer database unless the user explicitly accepts loss of post-backup changes.

A release-specific rollback helper is provided in `scripts/rollback_resource_release.sh`; it rejects active tasks, keeps current data and re-materializes new append logs into legacy JSON for the old application before switching. Review remote paths and current release before executing it.
