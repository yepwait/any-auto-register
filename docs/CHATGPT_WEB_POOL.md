# ChatGPT Web 池巡检（独立于 Codex／RT 巡检）

本功能为 `chatgpt2api`（截图中“ChatGPT 注册机 / 号池管理”那套服务）增加一条**独立的 Web 账号池额度检查与低额度自动补号**流程，与原有的 Codex／RT 巡检、接码自动补库完全隔离。

## 它做什么

1. 读取远端号池：`GET {root}/api/accounts`，`Authorization: Bearer <管理员密钥>`。
   - 只统计 `status == "正常"` 的账号。
   - `剩余图片额度 = Σ max(0, quota)`；`可用账号 = 正常且 quota > 0`。
   - **这是 ChatGPT Web 图片生成额度，不是 Codex 文本额度，也不是实时上游余额**（远端读的是自己缓存的记录）。
2. 低于目标时补号：
   - 先补传本地“本用途待上传”的账号（避免重复注册）。
   - 再按需用本项目**邮箱注册**（`email` + `access_token_only`），**不接手机短信、不补 RT、不做 Codex OAuth、不走 CPA／Sub2API**。
   - 上传 `POST {root}/api/accounts`，只发送 `{accounts:[{access_token, email}]}`，不发送密码、RT、Cookie 或邮箱凭据。
   - 上传后从返回的整池 `items` 里核对：必须找到该账号、`status == "正常"` 且 `quota > 0` 才算补号成功；HTTP 200 不等于账号可用。
3. 有界运行：每轮注册尝试上限、每日注册上限（UTC）、连续失败上限、两次尝试间隔。每日上限支持 **0 = 每日不限**；用量仍累计记录，每轮次数、连续失败及目标额度停止条件照常生效。
4. 结果落库：账号 `extra.account_purpose = "chatgpt_web_pool"`，并记录 `sync_statuses.chatgpt2api` 的脱敏结果。

## 关键安全边界

- 远端额度/结构读不到或不可解析时**停止补号**，绝不把“未知”当作 0 去盲目注册。
- 远端鉴权失败（401/403）、非 2xx、返回登录页 HTML、业务错误都视为失败并停止本轮。
- 不跟随重定向（避免带着管理员密钥跳转），不读取进程代理/netrc。
- 请求失败/超时时，账号保留本地，下一轮先 `GET` 复核是否其实已入池，避免重复注册。
- 日志和前端只展示脱敏统计，不打印 token、密钥或整池 `items`。

## 与其他流程的关系

- **「接码自动补库」可以把这些账号当作普通可处理账号**：Web 池账号（有 access_token、还没 RT）会进入接码补库的“已注册未接码／可补 RT”候选，补 RT 成功并拿到 RT 后会计入接码库存。手工/批量“补 RT”也会选到它们（同一套 RT 链路）。
- Web 池账号仍然**不进入「Codex／RT 巡检中心」**，也**不会自动外传到 CPA／Sub2API／CodexProxy**（注册后自动同步被跳过，账号页的上传类动作对这些账号返回拒绝），避免凭证被悄悄送到别处。如需外传请用普通账号流程。
- 账号管理页对这些账号显示“Web 号池专用 · 免手机接码”，RT 列显示“Web 不需要 RT”。

## 如何配置

进入侧边栏“Web 池巡检”页面：

- **远端 Root URL**：`chatgpt2api` 服务根地址（如 `https://your-host` 或带反代前缀），不要填 `/api/accounts` 或 `/health`。
- **管理员 Bearer 密钥**：远端管理员密钥（不是普通用户 Key），保存后不回显，留空表示保留原值；**更换地址时必须重新输入密钥**。
- **目标口径**：剩余图片额度 或 可用账号数。
- **目标值 / 间隔 / 每轮上限 / 每日上限 / 连续失败上限 / 请求超时**。
- **每日注册上限（UTC）**：`0` 表示每日不限，`1～1000` 表示当天最多发起的注册尝试数（失败也计数）。不限时仍记录当天用量，状态接口用 `daily_registration_unlimited=true`、`daily_registrations_remaining=null` 表示不限，不使用 `0` 冒充剩余次数。修改此值不会清零当天用量；启用补号仍需确认可能产生的邮箱费用。

操作：

- “检查已保存配置”：只读远端，不保存表单、不启动注册。
- “按已保存配置执行一次”：需确认邮箱费用后创建任务。
- 启用定时巡检：保存并确认邮箱费用；关闭→开启时唤醒调度器立即执行一次。

> 邮箱注册可能产生邮箱费用（取决于全局邮箱供应商）；本功能只承诺不使用手机接码。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/chatgpt-web-pool/config` | 读取配置（密钥不回显） |
| PUT | `/api/chatgpt-web-pool/config` | 保存配置；启用需 `confirm_registration` |
| GET | `/api/chatgpt-web-pool/status` | 本地状态、最近快照、待上传数、每日用量、任务历史（不访问远端） |
| POST | `/api/chatgpt-web-pool/check` | 用已保存配置只读远端额度 |
| POST | `/api/chatgpt-web-pool/run` | 需 `confirm_registration`，创建补号任务 |

## 验证

离线测试（全部 mock，不发真实网络、不注册、不接码）：

```
python -m pytest tests/test_chatgpt_web_pool.py tests/test_chatgpt_web_only.py -q
```

覆盖：额度解析/未知额度失败、鉴权与 HTTP 错误、上传只发 AT+email、上传后按账号核验、待上传优先/并发达标跳过后注册、超时先复核、每轮/每日/连续失败上限、接码补库可处理这些账号而 Codex/RT 巡检与外传仍排除、注册强制 email/AT-only/无 SMS/无 RT、phone challenge 失败即止、AT 缺失不算成功。

前端类型检查：`cd frontend && node node_modules/typescript/bin/tsc -b`。

## 每日不限选项部署记录（2026-11-11）

- 已部署至 `https://sdk.yepoodai.cn`，版本 `resource-20261111T140633Z`。
- 本次完整离线回归：891 项通过；TypeScript 与生产构建通过。
- 线上配置/状态接口及新前端资源 HTTP 200；0 的模型、预算与状态语义使用 mock 验证，没有触发真实注册或接码。
- 线上保存的每日上限仍为 **1**；所有配置值与账号数量（38）在部署前后核对一致。用户需要自行填写 0 并保存确认，才会取消每日上限。
- 备份目录：`/opt/any-auto-register/backups/resource-20261111T140633Z`，包含旧 compose、旧源码路径、旧镜像标识及停止期间的数据备份；旧镜像保留为 `any-auto-register:before-zero-limit-20261111`。
- 新容器首次核验约 84MiB，重启次数 0、无 OOM；原协议模式资源限制与自动化配置保留。

