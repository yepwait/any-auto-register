# 列表滚动修复与发布记录

## 用户问题与结果

用户指出列表下滚时筛选条件一起滚走。根因是表格只设置了横向滚动，没有在有界视口内分配表格主体高度，数据行撑高了整个页面。

已修正并按本轮明确授权发布至 https://sdk.yepoodai.cn，前端版本 `list-scroll-20261112T062323Z`。

- 账号页：标题、筛选、批量操作、表头和分页固定；仅表格数据区域纵向滚动。
- 任务历史：平台筛选、表头、分页不随数据滚动。
- 代理列表：添加代理区默认折叠，展开时重新计算列表空间；批量操作与表头固定，保留原来的无分页模式。
- 其他表单/仪表盘继续在 Content 内自然滚动，不把所有页面硬套列表高度。

## 实现

- App 视口高度明确为100dvh，Content保持可收缩且独立滚动；列表页单独 opt-in `admin-list-page`。
- `useTableViewport` 实测可用表格区域、真实表头、分页及其外边距，由 ResizeObserver 合帧更新 `scroll.y`，不是固定写死 `100vh - 某个常数`。
- 日期筛选展开、多选后工具栏换行、侧栏变化、窗口缩放都会重新分配列表高度。
- 保留原来服务端分页、每页20/50/100、横向滚动、勾选及批量操作，不改任何业务API。
- 手机/矮窗口对控件区设置独立的受限滚动空间，避免工具栏占满全部列表；表头和分页仍可到达。

修改源码：

- `frontend/src/App.tsx`
- `frontend/src/index.css`
- `frontend/src/hooks/useTableViewport.ts`
- `frontend/src/pages/Accounts.tsx`
- `frontend/src/pages/TaskHistory.tsx`
- `frontend/src/pages/Proxies.tsx`

## 本地验证

- TypeScript `tsc -b` 与生产构建通过。
- 新hook/任务历史定向ESLint通过；账号/代理页既有全文件诊断未在本轮清理，不声称全仓lint全绿。
- 17项多行模拟浏览器检查通过：137个账号分页样本、90个代理、多行错误任务；每页100、日期展开、多选、横向滚动、下一页、表格滚动到底不带动父容器。
- 分辨率1440×900、1366×768、1024×620、430×932、390×844、810×450覆盖；移动端和矮窗口分页均可见。
- 14组现有管理界面模拟检查再次通过，表单保存/费用确认/未知额度等不受影响。
- 没有本轮后端代码改动，因此未为纯前端滚动修复重复运行后端全量测试；上一轮1052项通过记录仍保留，不能表述为本轮重跑。
- 模拟脚本 `scripts/check_list_scroll.py`，报告 `dist/list-scroll/report.json`；所有API拦截，无真实业务请求。

## 发布方式与保留事项

这次使用前端静态资源热更新，**没有重启或重建正在运行的后端容器**。

1. 上传并逐文件校验纯前端包；不包含账号数据库、密钥、.env或后端源码改动。
2. 新哈希资源先复制，保留旧资源供已打开页面使用，再用 `os.replace` 原子切换HTML入口。
3. 以现场已验证后端镜像为基础制作持久化UI镜像，compose和完整源码路径同步指向新版本，避免以后recreate回到旧页面。
4. 当前容器ID、StartedAt和restart_count发布前后完全一致；50个账号、业务配置/密钥未改。
5. 原域名健康HTTP200；没有执行注册、接码、支付或quota检查POST。

当前运行容器仍显示基础镜像 `any-auto-register:ui-reliable-20261112T051329Z`，其可写层静态文件已更新。后续 compose recreate 的镜像为 `any-auto-register:list-scroll-20261112T062323Z`，ID `sha256:dc5aff329459cc7ea39ced0a7d85134f04f5d0b3cddf49284d766161cf6a7b9f`；基础后端代码相同。

- 发布包SHA256：`dec211b6715607ab9ae33e8d313ae6e8c52eac9738138d146e4a1eac697ca322`。
- 入口SHA256：`390e08be0334f863c83c8b8430bd836427e9c51f0552a709d125a01fa1de9e9b`。
- 线上68项静态文件/metadata逐项校验通过，其中61个为构建资产。
- 备份：`/opt/any-auto-register/backups/list-scroll-20261112T062323Z/`，含旧HTML、原compose、源码路径与发布记录。
- 回退辅助：上述目录的 `rollback-ui.py`，只回退UI/compose指向，不重启、不回退数据库；未实际执行本轮回退演练。
- 未Git提交或推送。

## 原站只读验收

Playwright在原域名使用真实已有列表，所有请求限制为GET/HEAD/OPTIONS，不点击业务操作、不保存账号行。

- 账号桌面：主体可滚动，筛选/表头固定，分页在视口内；主体高348，数据总高5669。
- 账号手机：同样通过；主体高340，数据总高5669。
- 任务历史桌面：同样通过；主体高439，数据总高4184。
- 6个入口JS/CSS资源字节哈希匹配发布包。
- 浏览器运行错误0，业务写请求0。

报告：`dist/list-scroll-20261112T062323Z/production/verification.json`；线上发布记录：`dist/list-scroll-20261112T062323Z/deployment.json`。

若浏览器旧页面仍在缓存，请Ctrl+F5重新载入即可，无须重设筛选、密钥或业务配置。
