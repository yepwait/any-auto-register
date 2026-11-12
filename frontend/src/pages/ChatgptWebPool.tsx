import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, App, Badge, Button, Card, Col, Collapse, Descriptions, Drawer, Form,
  Input, InputNumber, Modal, Row, Select, Space, Spin, Switch, Table, Tag, Tooltip, Typography,
} from 'antd'
import { FileTextOutlined, PlayCircleOutlined, ReloadOutlined, SaveOutlined, SettingOutlined, InfoCircleOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import type { TaskKind } from '@/components/TaskLogPanel'
import { AdminPageHeader, MetricCard } from '@/components/admin/AdminPage'

const { Paragraph, Text } = Typography
const API_PATH = '/chatgpt-web-pool'
const POLL_INTERVAL = 5000

type PoolSettings = {
  enabled: boolean
  base_url: string
  admin_token: string
  protocol: 'auto' | 'legacy' | 'yukkcat_v3'
  target_mode: 'quota' | 'available_accounts'
  target_value: number
  refill_below: number
  interval_minutes: number
  max_attempts_per_run: number
  daily_registration_limit: number
  max_consecutive_failures: number
  delay_seconds: number
  request_timeout: number
}

type PoolConfig = Omit<PoolSettings, 'admin_token'> & { admin_token_configured: boolean }
type PoolConfigResponse = Omit<PoolConfig, 'refill_below' | 'protocol'> & {
  refill_below?: number
  protocol?: PoolSettings['protocol']
}

type PoolSnapshot = {
  total_accounts: number
  normal_accounts: number
  available_accounts: number
  remaining_quota: number | null
  quota_known?: boolean
  known_remaining_quota?: number
  unknown_quota_accounts?: number
  unlimited_accounts?: number
  protocol?: string
  limited_accounts: number
  abnormal_accounts: number
  disabled_accounts: number
  checked_at: number | string | null
}

type PoolTask = {
  id: string
  source?: string
  kind?: string
  status: string
  progress?: string
  success?: number
  registered?: number
  total?: number
  skipped?: number
  errors?: string[]
  error_count?: number
  created_at?: number | string | null
  meta?: { reason?: string; phase?: string; trigger?: string; registrations?: number; outcome?: string;
    attempt_limit_unlimited?: boolean; settings?: Partial<PoolSettings> }
}

type PoolRealtime = {
  secret_configured: boolean
  pending: boolean
  active_task_id: string
  status: 'idle' | 'pending' | 'processing' | 'waiting_busy' | 'cooldown' | 'paused' | 'error'
  reason: string
  last_received_at: number
  last_started_at: number
  last_finished_at: number
  next_attempt_at: number
  received: number
  coalesced: number
  failure_runs: number
  refill_latched: boolean
}

type PoolStatus = {
  enabled: boolean
  snapshot: PoolSnapshot | null
  pending_uploads: number
  daily_registrations_used: number
  daily_registrations_remaining: number | null
  daily_registration_unlimited?: boolean
  budget_date_utc: string
  active: PoolTask | null
  tasks: PoolTask[]
  realtime?: PoolRealtime
}

type Confirmation = { type: 'save'; values: PoolSettings } | { type: 'run' }
type Operation = 'save' | 'check' | 'run'
type LogSelection = { id: string; kind: TaskKind }

const limitLabel = (value: number | null | undefined) => value === 0 ? '不限' : value == null ? '未记录' : `${value} 次`

function isQuotaBlocked(task: PoolTask) {
  return task.meta?.outcome === 'quota_unknown'
}

function poolTaskStatus(task: PoolTask) {
  return isQuotaBlocked(task) ? <Tag color="warning">{(task.registered ?? 0) > 0 ? '已暂停：额度未知' : '未执行：额度未知'}</Tag> : taskStatus(task.status)
}

const DEFAULTS: PoolSettings = {
  enabled: false,
  base_url: '',
  admin_token: '',
  protocol: 'auto',
  target_mode: 'quota',
  target_value: 100,
  refill_below: 0,
  interval_minutes: 30,
  max_attempts_per_run: 5,
  daily_registration_limit: 20,
  max_consecutive_failures: 3,
  delay_seconds: 5,
  request_timeout: 60,
}

const TASK_STATUSES: Record<string, { color: string; label: string }> = {
  pending: { color: 'default', label: '等待中' },
  running: { color: 'processing', label: '运行中' },
  done: { color: 'success', label: '已结束' },
  failed: { color: 'error', label: '失败' },
  stopped: { color: 'warning', label: '已停止' },
}

const REALTIME_STATUSES: Record<PoolRealtime['status'], { color: string; label: string }> = {
  idle: { color: 'default', label: '空闲' },
  pending: { color: 'processing', label: '等待复核' },
  processing: { color: 'processing', label: '处理中' },
  waiting_busy: { color: 'warning', label: '等待其他任务结束' },
  cooldown: { color: 'warning', label: '等待重试' },
  paused: { color: 'default', label: '已暂停' },
  error: { color: 'error', label: '异常' },
}

function taskStatus(value: string) {
  const item = TASK_STATUSES[value] || { color: 'default', label: value || '未知' }
  return <Tag color={item.color}>{item.label}</Tag>
}

function taskKind(task: PoolTask): TaskKind {
  return task.kind === 'chatgpt_web_pool_register' || task.source === 'chatgpt_web_pool_register'
    ? 'chatgpt_web_pool_register'
    : 'chatgpt_web_pool'
}

function formatTime(value: number | string | null | undefined) {
  if (value === null || value === undefined || value === '') return '未记录'
  const numeric = typeof value === 'number' ? value : Number(value)
  const date = Number.isFinite(numeric)
    ? new Date(numeric > 1_000_000_000_000 ? numeric : numeric * 1000)
    : new Date(value)
  return Number.isNaN(date.getTime()) ? '未记录' : date.toLocaleString('zh-CN')
}

function formatRealtimeTime(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '未记录'
  const date = new Date(value * 1000)
  return Number.isNaN(date.getTime()) ? '未记录' : date.toLocaleString('zh-CN')
}

// 只取可编辑字段；不把服务端的密钥或派生字段合并进表单及 PUT 请求。
function toSettings(config: PoolConfigResponse): PoolSettings {
  return {
    enabled: config.enabled,
    base_url: config.base_url,
    admin_token: '',
    protocol: config.protocol ?? 'auto',
    target_mode: config.target_mode,
    target_value: config.target_value,
    refill_below: config.refill_below ?? 0,
    interval_minutes: config.interval_minutes,
    max_attempts_per_run: config.max_attempts_per_run,
    daily_registration_limit: config.daily_registration_limit,
    max_consecutive_failures: config.max_consecutive_failures,
    delay_seconds: config.delay_seconds,
    request_timeout: config.request_timeout,
  }
}

function toConfig(config: PoolConfigResponse): PoolConfig {
  const settings = toSettings(config)
  return {
    enabled: settings.enabled,
    base_url: settings.base_url,
    protocol: settings.protocol,
    target_mode: settings.target_mode,
    target_value: settings.target_value,
    refill_below: settings.refill_below,
    interval_minutes: settings.interval_minutes,
    max_attempts_per_run: settings.max_attempts_per_run,
    daily_registration_limit: settings.daily_registration_limit,
    max_consecutive_failures: settings.max_consecutive_failures,
    delay_seconds: settings.delay_seconds,
    request_timeout: settings.request_timeout,
    admin_token_configured: Boolean(config.admin_token_configured),
  }
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback
}

function knownQuota(snapshot: PoolSnapshot | null | undefined) {
  return snapshot?.quota_known !== false
    && typeof snapshot?.remaining_quota === 'number'
    && Number.isFinite(snapshot.remaining_quota)
    && snapshot.remaining_quota >= 0
}

// 反向代理路径前缀是允许的；只拦截会拼出错误接口的具体端点路径。
function isForbiddenBasePath(pathname: string) {
  const path = pathname.replace(/\/{2,}/g, '/').replace(/\/+$/, '') || ''
  return path === '/health' || path === '/api/accounts' || path.startsWith('/api/accounts/')
}

export default function ChatgptWebPool() {
  const { message } = App.useApp()
  const [form] = Form.useForm<PoolSettings>()
  const [config, setConfig] = useState<PoolConfig | null>(null)
  const [status, setStatus] = useState<PoolStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [initialError, setInitialError] = useState('')
  const [statusError, setStatusError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [dirty, setDirty] = useState(false)
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const [logTask, setLogTask] = useState<LogSelection | null>(null)
  const [connectionOpen, setConnectionOpen] = useState<string[]>([])
  const [advancedOpen, setAdvancedOpen] = useState<string[]>([])
  const [diagnosticsOpen, setDiagnosticsOpen] = useState<string[]>([])
  const lifecycleRef = useRef<AbortController | null>(null)
  const statusRequestRef = useRef<Promise<void> | null>(null)
  const operationRef = useRef<Operation | null>(null)
  const initializingRef = useRef(false)

  const initialize = useCallback(async (signal: AbortSignal) => {
    if (signal.aborted || initializingRef.current) return
    initializingRef.current = true
    setLoading(true)
    setInitialError('')
    try {
      const [nextConfig, nextStatus] = await Promise.all([
        apiFetch(`${API_PATH}/config`, { signal }) as Promise<PoolConfigResponse>,
        apiFetch(`${API_PATH}/status`, { signal }) as Promise<PoolStatus>,
      ])
      if (signal.aborted) return
      setConfig(toConfig(nextConfig))
      setStatus(nextStatus)
      form.setFieldsValue({ ...DEFAULTS, ...toSettings(nextConfig) })
      setConnectionOpen(nextConfig.base_url && nextConfig.admin_token_configured ? [] : ['connection'])
      setDirty(false)
      setStatusError('')
    } catch (error) {
      if (!signal.aborted) setInitialError(errorMessage(error, '加载 Web 池配置和本地状态失败'))
    } finally {
      if (!signal.aborted) {
        initializingRef.current = false
        setLoading(false)
      }
    }
  }, [form])

  // 所有刷新入口共用一个请求；轮询永远不读取配置、不触碰编辑中的表单。
  const refreshStatus = useCallback((): Promise<void> => {
    const signal = lifecycleRef.current?.signal
    if (!signal || signal.aborted || initializingRef.current) return Promise.resolve()
    if (statusRequestRef.current) return statusRequestRef.current
    setRefreshing(true)
    const request = (async () => {
      try {
        const nextStatus = await apiFetch(`${API_PATH}/status`, { signal }) as PoolStatus
        if (!signal.aborted) {
          setStatus(nextStatus)
          setStatusError('')
        }
      } catch (error) {
        if (!signal.aborted) setStatusError(errorMessage(error, '刷新本地状态失败'))
      } finally {
        if (!signal.aborted) {
          statusRequestRef.current = null
          setRefreshing(false)
        }
      }
    })()
    statusRequestRef.current = request
    return request
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    lifecycleRef.current = controller
    initializingRef.current = false
    statusRequestRef.current = null
    let timer: number | undefined
    const schedulePoll = () => {
      window.clearTimeout(timer)
      if (!controller.signal.aborted && !document.hidden) timer = window.setTimeout(() => void poll(), POLL_INTERVAL)
    }
    const poll = async () => {
      if (controller.signal.aborted || document.hidden) return
      if (!operationRef.current) await refreshStatus()
      schedulePoll()
    }
    const onVisibilityChange = () => {
      window.clearTimeout(timer)
      if (!document.hidden) void poll()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    const initialTimer = window.setTimeout(() => {
      void initialize(controller.signal).then(schedulePoll)
    }, 0)
    return () => {
      controller.abort()
      window.clearTimeout(initialTimer)
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
      if (lifecycleRef.current === controller) lifecycleRef.current = null
    }
  }, [initialize, refreshStatus])

  const perform = async (action: Operation, request: (signal: AbortSignal) => Promise<void>) => {
    const signal = lifecycleRef.current?.signal
    if (!signal || signal.aborted || initializingRef.current || operationRef.current) return
    operationRef.current = action
    setOperation(action)
    try {
      // 等待已经在途的状态请求，避免旧快照覆盖检查/保存后的结果。
      await statusRequestRef.current
      if (signal.aborted) return
      await request(signal)
    } catch (error) {
      if (!signal.aborted) message.error(errorMessage(error, '操作失败，请重试'))
    } finally {
      if (!signal.aborted) {
        operationRef.current = null
        setOperation(null)
      }
    }
  }

  const persist = (values: PoolSettings, confirmed: boolean) => perform('save', async signal => {
    const previousEnabled = config?.enabled ?? false
    const result = await apiFetch(`${API_PATH}/config`, {
      method: 'PUT',
      signal,
      body: JSON.stringify({ config: values, confirm_registration: confirmed }),
    }) as { ok: boolean; config: PoolConfigResponse }
    if (signal.aborted) return
    if (!result.ok) throw new Error('配置未保存，请检查后重试')
    setConfig(toConfig(result.config))
    form.setFieldsValue(toSettings(result.config))
    setDirty(false)
    setConfirmation(null)
    message.success(!previousEnabled && result.config.enabled
      ? '配置已保存，自动维护（实时通知+定时兜底）已启用；是否创建任务以本地状态为准'
      : '配置已保存；本次保存不会额外唤醒调度器')
    await refreshStatus()
  })

  const save = async () => {
    if (operationRef.current || confirmation || !config) return
    try {
      await form.validateFields()
      const values = { ...toSettings(config), ...form.getFieldsValue(true) } as PoolSettings
      if (lifecycleRef.current?.signal.aborted !== false) return
      const settings: PoolSettings = {
        ...values,
        protocol: values.protocol ?? 'auto',
        refill_below: values.refill_below ?? 0,
        base_url: values.base_url.trim().replace(/\/+$/, ''),
        admin_token: (values.admin_token || '').trim(),
      }
      if (settings.enabled) {
        setConfirmation({ type: 'save', values: settings })
      } else {
        await persist(settings, false)
      }
    } catch (error) {
      // Errors in folded fields must be visible, while all stored values remain preserved.
      const fields = (error as { errorFields?: { name: (string | number)[] }[] }).errorFields || []
      const names = fields.map(field => String(field.name[0]))
      if (names.some(name => ['base_url', 'admin_token'].includes(name))) setConnectionOpen(['connection'])
      if (names.some(name => ['protocol', 'interval_minutes', 'max_attempts_per_run', 'max_consecutive_failures', 'delay_seconds', 'request_timeout'].includes(name))) setAdvancedOpen(['advanced'])
    }
  }

  const checkSaved = () => perform('check', async signal => {
    const result = await apiFetch(`${API_PATH}/check`, {
      method: 'POST', signal, body: JSON.stringify({}),
    }) as { snapshot: PoolSnapshot }
    if (signal.aborted) return
    setStatus(previous => previous ? { ...previous, snapshot: result.snapshot } : previous)
    message.success('已读取远端记录；未保存配置，也未启动注册')
  })

  const runSaved = () => perform('run', async signal => {
    const result = await apiFetch(`${API_PATH}/run`, {
      method: 'POST', signal, body: JSON.stringify({ confirm_registration: true }),
    }) as { triggered: boolean; task_id?: string; reason?: string }
    if (signal.aborted) return
    setConfirmation(null)
    if (result.triggered && result.task_id) {
      setLogTask({ id: result.task_id, kind: 'chatgpt_web_pool' })
      message.success('Web 池任务已创建；执行结果请查看日志')
    } else {
      message.info(result.reason || '本次未创建 Web 池任务')
    }
    await refreshStatus()
  })

  const onLogDone = useCallback(() => {
    if (!operationRef.current) void refreshStatus()
  }, [refreshStatus])

  const busy = operation !== null
  const snapshot = status?.snapshot
  const realtime = status?.realtime
  const realtimeStatus = realtime ? REALTIME_STATUSES[realtime.status] : undefined
  const quotaIsKnown = knownQuota(snapshot)
  const availableIsKnown = typeof snapshot?.available_accounts === 'number'
    && Number.isInteger(snapshot.available_accounts) && snapshot.available_accounts >= 0
  const targetIsKnown = config?.target_mode === 'available_accounts' ? availableIsKnown : quotaIsKnown
  const availableDescription = snapshot?.protocol === 'yukkcat_v3'
    ? '按远端 available，可能包含额度未知或无限账号'
    : snapshot?.protocol === 'legacy'
      ? '正常且 quota > 0'
      : '按远端协议口径统计'
  const savedConnection = Boolean(config?.base_url && config.admin_token_configured)
  const canRun = savedConnection && targetIsKnown && !status?.active
  const tasks = status?.tasks || []
  const confirmSettings = confirmation?.type === 'save' ? confirmation.values : config
  const enableTransition = confirmation?.type === 'save' && !config?.enabled

  if (loading) return <div style={{ padding: 80, textAlign: 'center' }}><Spin size="large" /></div>
  if (initialError || !config) return <Alert
    type="error" showIcon message="无法读取 Web 池配置" description={initialError || '配置尚未就绪；未执行任何注册操作。'}
    action={<Button onClick={() => {
      const signal = lifecycleRef.current?.signal
      if (signal) void initialize(signal)
    }}>重新加载</Button>}
  />

  let connectionLabel = config.base_url || '尚未配置服务'
  try { connectionLabel = new URL(config.base_url).host } catch { /* use saved text */ }
  const activeTask = status?.active
  const quotaBlocked = savedConnection && !!snapshot && !targetIsKnown
  const latestTask = tasks[0]
  const stateLabel = activeTask
    ? (activeTask.status === 'pending' ? '等待执行' : '正在检查 / 补号')
    : !config.enabled ? '已关闭'
      : quotaBlocked ? '额度未知，待确认'
      : realtime?.status === 'waiting_busy' ? '等待其他任务'
        : realtime?.status === 'cooldown' ? '冷却等待'
          : realtime?.status === 'error' ? '需要处理'
            : realtime?.pending ? '等待检查'
              : '监控中'
  const stateTone = activeTask ? 'accent' : realtime?.status === 'error' ? 'danger' : quotaBlocked ? 'warning' : config.enabled ? 'success' : 'default'
  const savedThreshold = config.refill_below || config.target_value
  const unit = config.target_mode === 'available_accounts' ? '个账号' : '次图片额度'
  const historyTriggers: Record<string, string> = { webhook: '实时通知', schedule: '定时检查', manual: '手动执行' }

  return <div className="admin-page">
    <AdminPageHeader title="Web 账号池" kicker="WEB POOL"
      description="额度不足时自动注册并同步，账号统一在这里管理。"
      extra={<>
        <Tooltip title="仅刷新本地运行状态，不访问远端"><Button icon={<ReloadOutlined />} loading={refreshing} disabled={busy || Boolean(confirmation)} onClick={() => void refreshStatus()} aria-label="刷新运行状态" /></Tooltip>
        <Button loading={operation === 'check'} disabled={busy || !savedConnection || Boolean(confirmation)} onClick={() => void checkSaved()}>检查额度</Button>
        <Button type="primary" icon={<PlayCircleOutlined />} loading={operation === 'run'} disabled={busy || !canRun || Boolean(confirmation)} onClick={() => setConfirmation({ type: 'run' })}>立即补号</Button>
      </>} />

    {statusError ? <Alert type="error" showIcon message="状态更新失败" description={statusError} /> : null}
    {quotaBlocked ? <Alert type="warning" showIcon message="当前额度无法确定，自动注册已暂停判断"
      description={`已知部分 ${snapshot?.known_remaining_quota ?? '—'}；${snapshot?.unknown_quota_accounts ?? 0} 个账号额度未知，${snapshot?.unlimited_accounts ?? 0} 个为无限额度。这不是 0 额度或注册失败；请先确认远端数据，或选择可用账号数模式并另设账号目标。`} /> : null}
    {config.enabled && realtime && !realtime.secret_configured ? <Alert type="warning" showIcon message="实时通知未连接，当前仍有定时检查兜底"
      description="尚未配置触发密钥；请先完成服务端联动配置。" /> : null}
    {realtime?.status === 'error' ? <Alert type="error" showIcon message="实时补号需要处理" description={realtime.reason || '请查看诊断详情或任务日志。'} /> : null}

    <div className="admin-metrics">
      <MetricCard label={<Space size={6}>图片剩余额度<Tooltip title="远端上次记录的图片额度，不是 Codex 余额；未知与无限不换算成 0。"><InfoCircleOutlined /></Tooltip></Space>}
        value={quotaIsKnown ? snapshot!.remaining_quota!.toLocaleString() : (snapshot?.unlimited_accounts ?? 0) > 0 ? '含无限额度' : '未知'} tone="accent"
        hint={snapshot ? `最近读取 ${formatTime(snapshot.checked_at)}` : '尚未检查远端额度'} />
      <MetricCard label="可用账号" value={availableIsKnown ? snapshot!.available_accounts.toLocaleString() : '未知'}
        hint={<Tooltip title={availableDescription}><span>总账号 {snapshot?.total_accounts ?? '—'} · 待同步 {status?.pending_uploads ?? '—'}</span></Tooltip>} />
      <MetricCard label="今日注册尝试 / 每日上限" value={<>{status?.daily_registrations_used ?? '—'}<span style={{ fontSize: 15, fontWeight: 400, color: 'var(--text-muted)' }}> / {config.daily_registration_limit === 0 ? '不限' : config.daily_registration_limit}</span></>}
        hint={<Tooltip title="按 UTC 日计算，北京时间每天早上 8 点进入新一天；失败尝试也计数。"><span>{config.daily_registration_limit === 0 ? '已取消每日次数限制' : `今日剩余 ${status?.daily_registrations_remaining ?? '—'} 次`}</span></Tooltip>} />
      <MetricCard label="运行状态" value={<span style={{ fontSize: 21 }}>{stateLabel}</span>} tone={stateTone}
        hint={activeTask ? `本轮进度 ${activeTask.progress || '准备中'}` : realtime?.last_received_at ? `最近通知 ${formatRealtimeTime(realtime.last_received_at)}` : '暂无实时通知'} />
    </div>

    <div className="admin-settings-grid">
      <Card className="admin-panel" title="补号策略" extra={dirty ? <Tag color="gold">未保存</Tag> : <Text type="secondary" style={{ fontSize: 12 }}>已保存</Text>}>
        <Form form={form} layout="vertical" preserve
          disabled={busy || Boolean(confirmation)} onValuesChange={() => setDirty(true)}>
          <div className="admin-section-header" style={{ marginBottom: 22 }}>
            <div><Text strong>自动补号</Text><div className="admin-note">实时通知触发，定时检查兜底</div></div>
            <Form.Item name="enabled" valuePropName="checked" style={{ margin: 0 }}><Switch aria-label="自动补号" checkedChildren="开启" unCheckedChildren="关闭" /></Form.Item>
          </div>
          <Form.Item name="target_mode" label="补号依据" rules={[{ required: true }]}>
            <Select options={[{ value: 'quota', label: '图片剩余额度' }, { value: 'available_accounts', label: '可用账号数' }]} />
          </Form.Item>
          <Row gutter={16}>
            <Col xs={24} sm={12}><Form.Item name="refill_below" label="低于此值时开始补号" tooltip="0 表示与补足目标相同；无需预留缓冲时保持 0。" dependencies={['target_value']} rules={[
              { required: true, type: 'integer', min: 0, max: 1000000, message: '请输入 0～1000000 的整数' },
              ({ getFieldValue }) => ({ validator: (_, value: number | null | undefined) => {
                const target = getFieldValue('target_value') as number | undefined
                return typeof value === 'number' && value > 0 && typeof target === 'number' && value > target
                  ? Promise.reject(new Error('开始阈值不能高于补足目标')) : Promise.resolve()
              } }),
            ]}><InputNumber min={0} max={1000000} precision={0} style={{ width: '100%' }} placeholder="0：沿用补足目标" /></Form.Item></Col>
            <Col xs={24} sm={12}><Form.Item name="target_value" label="补足目标" rules={[{ required: true, type: 'integer', min: 1, max: 1000000, message: '请输入至少 1 的整数目标' }]}>
              <InputNumber min={1} max={1000000} precision={0} style={{ width: '100%' }} />
            </Form.Item></Col>
          </Row>
          <Form.Item noStyle shouldUpdate={(prev, next) => prev.target_value !== next.target_value || prev.refill_below !== next.refill_below || prev.target_mode !== next.target_mode}>
            {({ getFieldValue }) => <div className="admin-note" style={{ marginTop: -8, marginBottom: 20 }}>
              低于 {getFieldValue('refill_below') || getFieldValue('target_value') || '—'} 开始，补到 {getFieldValue('target_value') || '—'} 停止（{getFieldValue('target_mode') === 'available_accounts' ? '可用账号数' : '图片额度'}）。
            </div>}
          </Form.Item>
          <Form.Item name="daily_registration_limit" label="每日注册上限" tooltip="按 UTC 日累计注册尝试次数，成功或失败都会计数；修改上限不清零当天用量。" extra="0 表示不限。" rules={[{ required: true, type: 'integer', min: 0, max: 1000, message: '请输入 0～1000 的整数，0 表示不限' }]}>
            <InputNumber min={0} max={1000} precision={0} style={{ width: '100%' }} />
          </Form.Item>

          <Collapse className="admin-collapse" ghost activeKey={advancedOpen} onChange={keys => setAdvancedOpen(typeof keys === 'string' ? [keys] : keys)} items={[{
            key: 'advanced', label: <Space><SettingOutlined /><span>高级设置</span></Space>, forceRender: true,
            children: <>
              <Row gutter={16}>
                <Col xs={24} sm={12}><Form.Item name="interval_minutes" label="定时兜底间隔（分钟）" tooltip="实时通知不等待此间隔。通知漏发时由定时检查兜底。" rules={[{ required: true, type: 'integer', min: 1, max: 10080 }]}><InputNumber min={1} max={10080} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
                <Col xs={24} sm={12}><Form.Item name="max_attempts_per_run" label="每轮最多尝试次数" extra="0 表示不限次数，仍在达到目标或手动停止时结束。" rules={[{ required: true, type: 'integer', min: 0, max: 100, message: '请输入 0～100 的整数，0 表示不限' }]}><InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
                <Col xs={24} sm={12}><Form.Item name="max_consecutive_failures" label="连续失败停止本轮" extra="0 表示不按连续失败次数停止，失败尝试仍可能产生邮箱费用。" rules={[{ required: true, type: 'integer', min: 0, max: 10, message: '请输入 0～10 的整数，0 表示不限' }]}><InputNumber min={0} max={10} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
                <Col xs={24} sm={12}><Form.Item name="delay_seconds" label="尝试间隔（秒）" rules={[{ required: true, type: 'number', min: 0, max: 3600 }]}><InputNumber min={0} max={3600} style={{ width: '100%' }} /></Form.Item></Col>
                <Col xs={24} sm={12}><Form.Item name="request_timeout" label="远端请求超时（秒）" rules={[{ required: true, type: 'integer', min: 5, max: 180 }]}><InputNumber min={5} max={180} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
                <Col xs={24} sm={12}><Form.Item name="protocol" label="远端接口版本"><Select options={[{ value: 'auto', label: '自动识别' }, { value: 'legacy', label: '原版 chatgpt2api' }, { value: 'yukkcat_v3', label: 'yukkcat v3' }]} /></Form.Item></Col>
              </Row>
              <p className="admin-note">两项均支持 0＝不限。若每日上限也为 0，可能持续尝试并产生费用；远端未知或请求异常仍会停止，手动停止始终可用。</p>
            </>,
          }]} />

          <div className="admin-save-bar">
            <Text type="secondary" style={{ fontSize: 12 }}>{dirty ? '有未保存修改：尚未用于后台任务' : '所有参数已保存'}</Text>
            <Button type="primary" icon={<SaveOutlined />} loading={operation === 'save'} onClick={() => void save()}>保存修改</Button>
          </div>
          <div className="admin-note" style={{ marginTop: 12 }}>补号可能产生邮箱费用。开启或手动执行前会再次确认。</div>

          <Collapse className="admin-collapse" ghost style={{ marginTop: 12 }} activeKey={connectionOpen} onChange={keys => setConnectionOpen(typeof keys === 'string' ? [keys] : keys)} items={[{
            key: 'connection', label: <span>连接设置 <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>{savedConnection ? connectionLabel : '首次使用请配置'}</Text></span>, forceRender: true,
            children: <>
              <Form.Item name="base_url" label="服务地址" extra="填写服务根地址，可含反代路径；不要填写 /api/accounts。" rules={[
                { required: true, whitespace: true, message: '请输入远端服务根地址' },
                { validator: (_, value: string) => {
                  if (!value?.trim()) return Promise.resolve()
                  try {
                    const url = new URL(value.trim())
                    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || isForbiddenBasePath(url.pathname)) throw new Error()
                    return Promise.resolve()
                  } catch { return Promise.reject(new Error('请输入不含凭据及查询参数的 HTTP(S) 服务根地址')) }
                } },
              ]}><Input placeholder="https://gpt.example.com" autoComplete="off" /></Form.Item>
              <Form.Item name="admin_token" label="管理员密钥" extra={config.admin_token_configured ? '已保存；留空保留。更换地址时需重新输入。' : '首次填写管理员密钥，不是普通用户 Key。'} dependencies={['base_url']} rules={[
                ({ getFieldValue }) => ({ validator: (_, value: string) => {
                  if (value?.trim()) return Promise.resolve()
                  const address = String(getFieldValue('base_url') || '').trim().replace(/\/+$/, '')
                  if (config.admin_token_configured && address !== config.base_url) return Promise.reject(new Error('更换服务地址时请重新输入管理员密钥'))
                  return config.admin_token_configured ? Promise.resolve() : Promise.reject(new Error('请填写管理员密钥'))
                } }),
              ]}><Input.Password placeholder="无需 Bearer 前缀" autoComplete="new-password" visibilityToggle={false} /></Form.Item>
            </>,
          }]} />
        </Form>
      </Card>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
        <Card className="admin-panel" title="运行概况" extra={<Badge status={config.enabled ? 'processing' : 'default'} text={config.enabled ? '自动补号开启' : '已关闭'} />}>
          <div className="admin-state-row"><span>已保存策略</span><span>低于 {savedThreshold} → 补至 {config.target_value}</span></div>
          <div className="admin-state-row"><span>目标口径</span><span>{unit}</span></div>
          <div className="admin-state-row"><span>已保存每轮上限</span><span>{limitLabel(config.max_attempts_per_run)}</span></div>
          <div className="admin-state-row"><span>已保存连续失败上限</span><span>{limitLabel(config.max_consecutive_failures)}</span></div>
          {dirty ? <div className="admin-note" style={{ marginTop: 12, marginBottom: 8 }}>左侧是待保存编辑值；后台和“立即补号”仍使用这里的已保存策略。已创建任务按创建时配置执行。</div> : null}
          <div className="admin-state-row"><span>当前任务</span><span>{activeTask ? poolTaskStatus(activeTask) : '无活动任务'}</span></div>
          <div className="admin-state-row"><span>最近检查结果</span><span>{latestTask ? poolTaskStatus(latestTask) : '尚无任务记录'}</span></div>
          <div className="admin-state-row"><span>实时通知</span><span>{realtime ? realtime.secret_configured ? '已配置密钥' : '未配置密钥' : '状态未提供'}</span></div>
          <div className="admin-state-row"><span>最近检查</span><span>{formatTime(snapshot?.checked_at)}</span></div>
          <div className="admin-state-row"><span>最近通知</span><span>{realtime ? formatRealtimeTime(realtime.last_received_at) : '未记录'}</span></div>
          {activeTask ? <Button block style={{ marginTop: 16 }} icon={<FileTextOutlined />} onClick={() => setLogTask({ id: activeTask.id, kind: taskKind(activeTask) })}>查看任务 / 停止 / 跳过</Button> : null}
          {realtime?.pending || realtime?.status === 'cooldown' ? <div className="admin-note" style={{ marginTop: 14 }}>{realtime.reason || '通知已接收，等待额度复核。'}</div> : null}
        </Card>
        <Card className="admin-panel" title="连接服务" extra={<Button type="text" size="small" onClick={() => setConnectionOpen(connectionOpen.length ? [] : ['connection'])}>修改</Button>}>
          <div className="admin-connection-host">{connectionLabel}</div>
          <Space size={6} wrap style={{ marginTop: 9 }}>
            <Tag color={savedConnection ? 'default' : 'warning'}>{savedConnection ? '密钥已保存' : '待配置'}</Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>{snapshot ? '有历史读取记录' : '尚未验证连接'}</Text>
          </Space>
          <Collapse className="admin-collapse" ghost activeKey={diagnosticsOpen} onChange={keys => setDiagnosticsOpen(typeof keys === 'string' ? [keys] : keys)} items={[{
            key: 'diagnostics', label: '诊断详情', children: <Descriptions column={1} size="small">
              <Descriptions.Item label="接口版本">{snapshot?.protocol ?? config.protocol}</Descriptions.Item>
              <Descriptions.Item label="已知图片额度">{snapshot?.known_remaining_quota ?? snapshot?.remaining_quota ?? '未知'}</Descriptions.Item>
              <Descriptions.Item label="未知 / 无限账号">{snapshot?.unknown_quota_accounts ?? '—'} / {snapshot?.unlimited_accounts ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="限流 / 异常 / 禁用">{snapshot?.limited_accounts ?? '—'} / {snapshot?.abnormal_accounts ?? '—'} / {snapshot?.disabled_accounts ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="通知状态">{realtimeStatus ? realtimeStatus.label : '未提供'}</Descriptions.Item>
              <Descriptions.Item label="通知 / 合并">{realtime?.received ?? '—'} / {realtime?.coalesced ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="失败轮次">{realtime?.failure_runs ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="下一次重试">{realtime ? formatRealtimeTime(realtime.next_attempt_at) : '未记录'}</Descriptions.Item>
              <Descriptions.Item label="最近处理结束">{realtime ? formatRealtimeTime(realtime.last_finished_at) : '未记录'}</Descriptions.Item>
              <Descriptions.Item label="补足阶段">{realtime?.refill_latched ? '持续补足至目标' : '未进入'}</Descriptions.Item>
              <Descriptions.Item label="当前原因">{realtime?.reason || '未记录'}</Descriptions.Item>
            </Descriptions>,
          }, {
            key: 'help', label: '工作方式', children: <div className="admin-note">
              注册在本项目完成，账号同步给远端使用。Web 注册只取 AT，不接手机短信，不补 RT，不自动外传其他平台。<br />
              收到通知后保存并合并待办，忙后复核；定时检查每 {config.interval_minutes} 分钟兜底。通知被接受不等于补号成功。<br />
              图片额度来自远端最后记录，未知不会按 0 处理；发送端阈值应与这里的策略一致。
            </div>,
          }]} />
        </Card>
      </div>
    </div>

    <Card className="admin-panel" title="最近补号记录" extra={<Text type="secondary" style={{ fontSize: 12 }}>{tasks.length} 条记录</Text>}>
      <div className="admin-note" style={{ marginBottom: 12 }}>以下为各任务创建时的记录，不代表当前表单配置；历史错误日志会保留。</div>
      <Table<PoolTask> className="admin-table" rowKey="id" size="small" dataSource={tasks} pagination={{ pageSize: 8, hideOnSinglePage: true }} scroll={{ x: 760 }} locale={{ emptyText: '暂无补号记录' }} columns={[
        { title: '时间', dataIndex: 'created_at', render: (value, task) => <Tooltip title={task.id}><span>{formatTime(value)}</span></Tooltip>, width: 178 },
        { title: '触发方式', render: (_, task) => historyTriggers[task.meta?.trigger || ''] || (taskKind(task) === 'chatgpt_web_pool_register' ? '注册子任务' : '维护检查'), width: 100 },
        { title: '状态', render: (_, task) => poolTaskStatus(task), width: 150 },
        { title: '注册尝试 / 已确认', render: (_, task) => `${task.meta?.registrations ?? '—'} / ${task.success ?? '—'}`, width: 140 },
        { title: '任务当时限制', render: (_, task) => <Text type="secondary" style={{ fontSize: 12 }}>每轮 {limitLabel(task.meta?.settings?.max_attempts_per_run ?? task.total)}<br />连续失败 {limitLabel(task.meta?.settings?.max_consecutive_failures)}</Text>, width: 145 },
        { title: '结果', render: (_, task) => <Text type={task.status === 'failed' ? 'danger' : 'secondary'}>{task.meta?.reason || task.meta?.phase || task.progress || '等待执行'}</Text> },
        { title: '', render: (_, task) => <Button type="link" size="small" onClick={() => setLogTask({ id: task.id, kind: taskKind(task) })}>查看日志</Button>, width: 92 },
      ]} />
    </Card>

    <Modal title={confirmation?.type === 'run' ? '确认补号' : enableTransition ? '开启自动补号' : '保存补号策略'}
      open={Boolean(confirmation)} confirmLoading={busy} closable={!busy} maskClosable={false} keyboard={!busy}
      cancelText="取消" okText={confirmation?.type === 'run' ? '确认并执行' : '确认并保存'} cancelButtonProps={{ disabled: busy }}
      onCancel={() => { if (!busy) setConfirmation(null) }} onOk={() => {
        if (confirmation?.type === 'save') void persist(confirmation.values, true)
        else if (confirmation?.type === 'run') void runSaved()
      }}>
      <Paragraph>将使用全局邮箱服务注册账号，可能产生邮箱费用。本流程不接手机短信、不补 RT。</Paragraph>
      <Descriptions size="small" column={1}>
        <Descriptions.Item label="策略">低于 {confirmSettings?.refill_below || confirmSettings?.target_value} 开始，补至 {confirmSettings?.target_value}</Descriptions.Item>
        <Descriptions.Item label="每日上限">{confirmSettings?.daily_registration_limit === 0 ? '不限' : `${confirmSettings?.daily_registration_limit} 次尝试（UTC）`}</Descriptions.Item>
        <Descriptions.Item label="每轮上限">{limitLabel(confirmSettings?.max_attempts_per_run)}</Descriptions.Item>
        <Descriptions.Item label="连续失败上限">{confirmSettings?.max_consecutive_failures === 0 ? '不限（不按连续失败次数停止）' : `${limitLabel(confirmSettings?.max_consecutive_failures)}后停止本轮`}</Descriptions.Item>
      </Descriptions>
      {confirmSettings?.max_attempts_per_run === 0 && confirmSettings.max_consecutive_failures === 0 ? <Paragraph type="warning" style={{ marginTop: 12 }}>每轮与连续失败均不限，可能持续注册并重复产生邮箱费用；每日上限为 0 时也不限制当天次数。目标达标、手动停止、配置变更或额度/请求异常仍会结束。</Paragraph> : null}
      <Text type="secondary" style={{ fontSize: 12 }}>{confirmation?.type === 'run' ? '使用已保存配置，不会保存当前修改。' : enableTransition ? '保存后启用实时通知与定时兜底，额度不足时可能开始补号。' : '保存后后续轮次使用新策略，不额外触发一次任务。'}</Text>
    </Modal>
    <Drawer title="Web 池任务日志" open={Boolean(logTask)} onClose={() => setLogTask(null)} width={800} destroyOnClose>
      {logTask ? <TaskLogPanel key={logTask.id} taskId={logTask.id} kind={logTask.kind} onDone={onLogDone} /> : null}
    </Drawer>
  </div>
}
