# 账号巡检 / RT / 补库修复实施记录（2026-11-11）

配套审查报告：[account-lifecycle-review-2026-11-11.md](<D:/work/apps/项目/11_注册机/gpt/any-auto-register/docs/reviews/account-lifecycle-review-2026-11-11.md>)。本轮只改本地工作区代码与测试，**未部署服务器、未改生产配置、未运行真实注册/租号/测活**，因此线上问题在部署前仍然存在。

## 一、修复的核心口径

“可用库存”改为：**账号主状态正常（registered/trial/subscribed）+ 持有 refresh_token + 没有明确的失效/停用证据**。短信验证保留为 `sms_verification` 独立事实，不再是入库的必要条件。

- 没有 add-phone、直接拿到有效 RT → 成功。
- 有 add-phone、完成必要验证再拿到 RT → 成功，并保留短信事实。
- 只有短信记录、没有 RT → 不计入可用库存。
- 明确停用/删除 → 退出自动恢复与库存。

## 二、改动清单（按文件）

| 文件 | 改动 |
| --- | --- |
| `services/chatgpt_account_state.py` | 新增 `account_refresh_token`、`account_probe_state`、`account_rt_ready`、`account_rt_recovery_pending`、`NORMAL_ACCOUNT_STATUSES`，作为库存与候选的统一判定。 |
| `services/sms_stock_service.py` | `_in_stock`/`_pending` 改用 RT 就绪判定；`supplement_account` 不再强制 `chatgpt_require_sms_verified`，改为补 RT 落库后做一次后置测活（`_probe_after_backfill`），成功以库存口径为准；测活异常也保留已轮换凭证（`merge_account_credentials`）；补库成功日志改为“可用 RT 账号”。 |
| `services/chatgpt_rt_backfill.py` | `apply_backfill_result` 补 RT 成功只把 `invalid/deleted` 洗回 `registered`，**保留 `trial/subscribed` 订阅状态**；新增 `merge_account_credentials`，供“刷新成功但后续异常”单独保存凭证。 |
| `platforms/chatgpt/rt_backfill.py` | RT 已到手但末段报“账号已删除/停用”时，`account_deleted=True` 优先于成功；`RefreshTokenBackfiller` 接收并传递 `task_control` 给接码 controller。 |
| `platforms/chatgpt/status_probe.py` | 刷新凭证后**重新解析 Account ID** 并更新本次结果，避免“刷新后新 AT 带 ID 仍报缺 ID”。 |
| `services/inspection_service.py` | `_backfill_and_persist` **先落库再响应停止**；测活异常时用 `merge_account_credentials` 补存已轮换凭证；后置测活失败不再用旧 probe 结果拼出“valid/usable 未通过”的矛盾提示；`inspect_account` 加账号级锁。 |
| `api/tasks.py` | 新注册子任务校验改为 `chatgpt_require_rt_ready`（不再要求短信）；手动补 RT 加账号级锁；任务日志代理 URL 脱敏（`_mask_proxy_url`）。 |
| `api/sms_stock.py` | 注册子任务传 `chatgpt_require_rt_ready=1`，不再传 `chatgpt_require_sms_verified=1`。 |
| `services/sms_service.py` | 新增 `SmsProviderFatalError`：`NO_BALANCE/BAD_KEY/INVALID_KEY/NO_KEY/WRONG_KEY` 等平台级错误不再换号重试；`wait_for_code/get_code` 支持 `stop_check`；`PhoneCallbackController` 接收 `task_control`，租号锁等待与等码循环都可响应停止；日志不再打印完整验证码。 |
| `platforms/chatgpt/protocol/auth_flow.py` | 接码循环遇到平台级致命错误时停止继续换号。 |
| `services/account_locks.py`（新增） | 按账号 ID 的进程内 RLock，串行化会轮换凭证的操作。 |

## 三、验证结果

- 相关测试模块（含新增回归与注册/协议测试）共 **267 项通过**，无网络、无真实账号。
- 新增回归：[tests/test_account_lifecycle_fixes.py](<D:/work/apps/项目/11_注册机/gpt/any-auto-register/tests/test_account_lifecycle_fixes.py>)，覆盖：无短信的 RT 成功入库、短信-only 不入库、订阅状态保留、刷新后重算 ID、停止后仍保存 RT、测活异常仍保存轮换凭证、停用信号优先、NO_BALANCE 只尝试一次、代理脱敏。
- 更新原有测试：`tests/test_sms_stock.py` 中“RT 无短信必须失败”的旧断言改为新口径，并补充 `_probe_after_backfill` 模拟；注册子任务校验消息改为“未取得有效 RT”。
- 离线自检脚本：[docs/reviews/account_lifecycle_repro.py](<D:/work/apps/项目/11_注册机/gpt/any-auto-register/docs/reviews/account_lifecycle_repro.py>) 8 项检查全部通过。

运行方式（本地，禁止外部网络）：

```powershell
& 'D:\work\util\Python312\python.exe' docs/reviews/account_lifecycle_repro.py
# 或跑测试
& 'D:\work\util\Python312\python.exe' -m pytest -q tests/test_account_lifecycle_fixes.py tests/test_sms_stock.py
```

## 四、上线前需要你确认/执行

1. **部署**：改动尚未部署。服务器为 `43.173.92.11` 的 `/opt/any-auto-register`，需按既有流程构建镜像并重启容器（本轮未执行）。
2. **配置迁移**：旧口径下开启的 `inspection_only_sms_verified=1` 会继续漏掉“无短信但有 RT”的账号，建议关闭或改为按新口径筛选；`sms_stock_register_flow` 仍是用户配置项，不需要强制改成手机注册。
3. **历史数据**：建议上线后先跑一次只读统计，确认“可用 RT 账号”数量与缺口，再决定是否补号；不要因为口径变化就批量新增。
4. **安全**：历史任务日志中可能已有代理密码/验证码/2FA 密钥残留，需另行制定清理或轮换计划（本轮未删除数据）。

## 五、本轮未做（后续建议）

- P2 性能项：任务状态页分页读取、日志增量/分批持久化、上传失败独立重试队列（当前仍按任务整体扫描与整份日志提交）。
- 多实例部署的分布式账号租约与凭证版本校验：当前账号锁仅限单进程。
- 余额/费用维度统计（账号尝试数、租号请求数、结算金额分开记录）。
- 短信 controller 的更深层停止传播（provider 内部 HTTP 请求级中断）仍需按实际网络耗时压测后细化。
