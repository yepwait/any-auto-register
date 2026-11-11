import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, App, Badge, Button, Card, Col, Descriptions, Divider, Drawer, Form,
  Input, InputNumber, Modal, Row, Select, Space, Spin, Statistic, Switch, Table, Tag, Typography,
} from 'antd'
import { FileTextOutlined, PlayCircleOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import type { TaskKind } from '@/components/TaskLogPanel'

const { Title, Paragraph, Text } = Typography
const API_PATH = '/chatgpt-web-pool'
const POLL_INTERVAL = 5000

type PoolSettings = {
  enabled: boolean
  base_url: string
  admin_token: string
  target_mode: 'quota' | 'available_accounts'
  target_value: number
  interval_minutes: number
  max_attempts_per_run: number
  daily_registration_limit: number
  max_consecutive_failures: number
  delay_seconds: number
  request_timeout: number
}

type PoolConfig = Omit<PoolSettings, 'admin_token'> & { admin_token_configured: boolean }

type PoolSnapshot = {
  total_accounts: number
  normal_accounts: number
  available_accounts: number
  remaining_quota: number | null
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
  meta?: { reason?: string; phase?: string }
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
}

type Confirmation = { type: 'save'; values: PoolSettings } | { type: 'run' }
type Operation = 'save' | 'check' | 'run'
type LogSelection = { id: string; kind: TaskKind }

const DEFAULTS: PoolSettings = {
  enabled: false,
  base_url: '',
  admin_token: '',
  target_mode: 'quota',
  target_value: 100,
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

// 只取可编辑字段；不把服务端的密钥或派生字段合并进表单及 PUT 请求。
function toSettings(config: PoolConfig): PoolSettings {
  return {
    enabled: config.enabled,
    base_url: config.base_url,
    admin_token: '',
    target_mode: config.target_mode,
    target_value: config.target_value,
    interval_minutes: config.interval_minutes,
    max_attempts_per_run: config.max_attempts_per_run,
    daily_registration_limit: config.daily_registration_limit,
    max_consecutive_failures: config.max_consecutive_failures,
    delay_seconds: config.delay_seconds,
    request_timeout: config.request_timeout,
  }
}

function toConfig(config: PoolConfig): PoolConfig {
  const settings = toSettings(config)
  return {
    enabled: settings.enabled,
    base_url: settings.base_url,
    target_mode: settings.target_mode,
    target_value: settings.target_value,
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
  return typeof snapshot?.remaining_quota === 'number'
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
  const targetMode = Form.useWatch('target_mode', form) as PoolSettings['target_mode'] | undefined
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
        apiFetch(`${API_PATH}/config`, { signal }) as Promise<PoolConfig>,
        apiFetch(`${API_PATH}/status`, { signal }) as Promise<PoolStatus>,
      ])
      if (signal.aborted) return
      setConfig(toConfig(nextConfig))
      setStatus(nextStatus)
      form.setFieldsValue(toSettings(nextConfig))
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
    }) as { ok: boolean; config: PoolConfig }
    if (signal.aborted) return
    if (!result.ok) throw new Error('配置未保存，请检查后重试')
    setConfig(toConfig(result.config))
    form.setFieldsValue(toSettings(result.config))
    setDirty(false)
    setConfirmation(null)
    message.success(!previousEnabled && result.config.enabled
      ? '配置已保存，自动巡检已启用；是否创建任务以本地状态为准'
      : '配置已保存；本次保存不会额外唤醒调度器')
    await refreshStatus()
  })

  const save = async () => {
    if (operationRef.current || confirmation || !config) return
    try {
      const values = await form.validateFields()
      if (lifecycleRef.current?.signal.aborted !== false) return
      const settings: PoolSettings = {
        ...values,
        base_url: values.base_url.trim().replace(/\/+$/, ''),
        admin_token: (values.admin_token || '').trim(),
      }
      if (settings.enabled) {
        setConfirmation({ type: 'save', values: settings })
      } else {
        await persist(settings, false)
      }
    } catch {
      // 字段校验失败只在表单中提示，不发起写入请求。
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
  const quotaIsKnown = knownQuota(snapshot)
  const savedConnection = Boolean(config?.base_url && config.admin_token_configured)
  const canRun = savedConnection && quotaIsKnown && !status?.active
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

  return <Space direction="vertical" size={16} style={{ width: '100%' }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
      <div>
        <Title level={3} style={{ margin: 0 }}>ChatGPT Web 池巡检</Title>
        <Paragraph type="secondary" style={{ margin: '4px 0 0' }}>
          独立维护 Web 图片账号池；与原 Codex／RT 巡检、接码补库分离。打开或刷新页面只读取本地配置与状态。
        </Paragraph>
      </div>
      <Space wrap>
        <Button icon={<ReloadOutlined />} loading={refreshing} disabled={busy || Boolean(confirmation)} onClick={() => void refreshStatus()}>刷新本地状态</Button>
        <Button loading={operation === 'check'} disabled={busy || !savedConnection || Boolean(confirmation)} onClick={() => void checkSaved()}>检查已保存配置</Button>
        <Button type="primary" icon={<PlayCircleOutlined />} loading={operation === 'run'} disabled={busy || !canRun || Boolean(confirmation)} onClick={() => setConfirmation({ type: 'run' })}>按已保存配置执行一次</Button>
      </Space>
    </div>

    <Alert type="warning" showIcon message="这是远端剩余图片额度，不是 Codex 余额，也不是实时上游验证"
      description="展示的是远端服务最后记录的账号状态和额度，不代表当前上游真实余额。可用账号 = 状态正常且 quota > 0；额度未知或无法可靠读取时停止补号，不把未知当作 0。只读检查不会保存表单，也不会启动注册。" />
    <Alert type="info" showIcon message="仅邮箱注册 + AT-only；禁止手机注册与 SMS，不补 RT，不走 CPA／Sub2API"
      description="先补传本用途待上传账号，再在目标不足时注册。注册可能消耗邮箱费用，受每轮尝试上限、每日注册上限（UTC）和连续失败上限约束；启用和手动执行均需确认。关闭页面不会停止后台任务，请使用任务日志中的停止／跳过控制。" />
    {dirty ? <Alert type="warning" showIcon message="有未保存修改：检查和手动执行仍使用服务器上已保存的配置，不会替你保存或启用。" /> : null}
    {statusError ? <Alert type="error" showIcon message="本地状态刷新失败，以下可能是旧数据" description={statusError} /> : null}

    <Row gutter={[16, 16]}>
      <Col xs={24} sm={12} xl={6}><Card><Statistic title="远端剩余图片额度（非 Codex）" value={quotaIsKnown ? snapshot!.remaining_quota! : '未知'} /><Text type="secondary">远端记录，非实时上游</Text></Card></Col>
      <Col xs={24} sm={12} xl={6}><Card><Statistic title="远端可用账号" value={snapshot?.available_accounts ?? '未知'} /><Text type="secondary">正常且 quota &gt; 0</Text></Card></Col>
      <Col xs={24} sm={12} xl={6}><Card><Statistic title="本用途待上传账号" value={status?.pending_uploads ?? '未知'} /><Text type="secondary">先补传，再按需注册</Text></Card></Col>
      <Col xs={24} sm={12} xl={6}><Card><Statistic title="今日注册已用 / 已保存上限" value={status?.daily_registrations_used ?? '未知'} suffix={`/ ${config.daily_registration_limit === 0 ? '不限' : config.daily_registration_limit}`} /><Text type="secondary">{status?.daily_registration_unlimited || config.daily_registration_limit === 0 ? '每日不限，仍记录用量' : `剩余 ${status?.daily_registrations_remaining ?? '未知'}`}；UTC {status?.budget_date_utc || '未记录'}</Text></Card></Col>
    </Row>

    <Row gutter={[16, 16]}>
      <Col xs={24} xl={16}>
        <Card title="连接与有界补号策略" extra={dirty ? <Tag color="warning">未保存</Tag> : <Text type="secondary">已保存配置，不代表连接已验证</Text>}>
          <Form form={form} layout="vertical" initialValues={DEFAULTS} disabled={busy || Boolean(confirmation)} onValuesChange={() => setDirty(true)}>
            <Form.Item name="base_url" label="远端 Root URL" extra="可包含反向代理路径前缀；不要填写 /api/accounts 或 /health 等具体接口路径，只使用你信任的 HTTPS／内网服务。" rules={[
              { required: true, whitespace: true, message: '请输入远端服务根地址' },
              { validator: (_, value: string) => {
                if (!value?.trim()) return Promise.resolve()
                try {
                  const url = new URL(value.trim())
                  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || isForbiddenBasePath(url.pathname)) throw new Error()
                  return Promise.resolve()
                } catch {
                  return Promise.reject(new Error('请输入 http(s) 根地址（可带端口和反代路径前缀），不要填写 /api/accounts、/health 等具体接口路径，也不要带凭据、查询或锚点'))
                }
              } },
            ]}>
              <Input placeholder="https://web-pool.example.com" autoComplete="off" />
            </Form.Item>
            <Form.Item name="admin_token" label="管理员 Bearer 密钥（非用户 Key）" extra={config.admin_token_configured ? '服务器已保存管理员密钥（不代表已验证）；留空保留，不回显原值。' : '尚未配置管理员密钥；保存后不回显，后续留空保留。'} rules={[
              { validator: (_, value: string) => config.admin_token_configured || value?.trim()
                ? Promise.resolve()
                : Promise.reject(new Error('首次配置请填写管理员 Bearer 密钥')) },
            ]}>
              <Input.Password placeholder="填写密钥值，无需 Bearer 前缀；留空保留" autoComplete="new-password" visibilityToggle={false} />
            </Form.Item>
            <Divider orientation="left">调度与目标</Divider>
            <Row gutter={16}>
              <Col xs={24} sm={12}><Form.Item name="enabled" label="启用定时 Web 池巡检" valuePropName="checked" extra="切换开关不会立即生效，需保存并确认邮箱费用。"><Switch checkedChildren="开启" unCheckedChildren="关闭" /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="interval_minutes" label="巡检间隔（分钟）" rules={[{ required: true, type: 'integer', min: 1, message: '请输入至少 1 分钟的整数' }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="target_mode" label="目标口径" rules={[{ required: true }]}><Select options={[
                { value: 'quota', label: '远端剩余图片额度' },
                { value: 'available_accounts', label: '远端可用账号数（正常且 quota > 0）' },
              ]} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="target_value" label={targetMode === 'available_accounts' ? '目标可用账号数' : '目标剩余图片额度（非 Codex）'} rules={[{ required: true, type: 'integer', min: 1, message: '请输入至少 1 的整数目标' }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="max_attempts_per_run" label="每轮最多注册尝试数" rules={[{ required: true, type: 'integer', min: 1, message: '请输入至少 1 次的有限上限' }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="daily_registration_limit" label="每日注册上限（UTC）" extra="0 表示每日不限；仍受每轮次数、连续失败和目标额度限制，可能持续产生邮箱费用。" rules={[{ required: true, type: 'integer', min: 0, max: 1000, message: '请输入 0～1000 的整数，0 表示每日不限' }]}><InputNumber min={0} max={1000} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="max_consecutive_failures" label="连续失败多少次停止本轮" rules={[{ required: true, type: 'integer', min: 1, message: '请输入至少 1 次的整数' }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="delay_seconds" label="两次注册尝试间隔（秒）" rules={[{ required: true, type: 'number', min: 0, message: '请输入不小于 0 的秒数' }]}><InputNumber min={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="request_timeout" label="远端请求超时（秒）" rules={[{ required: true, type: 'number', min: 1, message: '请输入至少 1 秒的超时' }]}><InputNumber min={1} style={{ width: '100%' }} /></Form.Item></Col>
            </Row>
            <Space wrap><Button type="primary" icon={<SaveOutlined />} loading={operation === 'save'} onClick={() => void save()}>保存配置</Button><Text type="secondary">保存仅在关闭 → 开启时唤醒调度器，不代表任务已开始。保持开启时保存不会额外触发。</Text></Space>
          </Form>
        </Card>
      </Col>
      <Col xs={24} xl={8}>
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Card title="本地运行状态（每 5 秒刷新）">
            <Descriptions column={1} size="small">
              <Descriptions.Item label="定时开关"><Badge status={status?.enabled ? 'processing' : 'default'} text={status ? (status.enabled ? '已启用（不代表任务运行中）' : '已关闭') : '未知'} /></Descriptions.Item>
              <Descriptions.Item label="已保存目标">{config.target_value} {config.target_mode === 'quota' ? '远端剩余图片额度' : '远端可用账号'}</Descriptions.Item>
              <Descriptions.Item label="已保存间隔">{config.interval_minutes} 分钟</Descriptions.Item>
              <Descriptions.Item label="当前任务">{status?.active ? taskStatus(status.active.status) : status ? '无活动任务' : '未知'}</Descriptions.Item>
              <Descriptions.Item label="执行阶段">{status?.active?.meta?.phase || '未记录'}</Descriptions.Item>
              <Descriptions.Item label="注册限制">每轮 {config.max_attempts_per_run} 次；{config.daily_registration_limit === 0 ? '每日不限' : `每日 ${config.daily_registration_limit} 次（UTC）`}；连续失败 {config.max_consecutive_failures} 次停止</Descriptions.Item>
            </Descriptions>
            {status?.active ? <Button style={{ marginTop: 12 }} icon={<FileTextOutlined />} onClick={() => setLogTask({ id: status.active!.id, kind: taskKind(status.active!) })}>查看日志／停止／跳过</Button> : null}
          </Card>
          <Card title="最近远端记录（不是实时上游）">
            <Descriptions column={1} size="small">
              <Descriptions.Item label="读取时间">{formatTime(snapshot?.checked_at)}</Descriptions.Item>
              <Descriptions.Item label="总账号">{snapshot?.total_accounts ?? '未知'}</Descriptions.Item>
              <Descriptions.Item label="正常账号">{snapshot?.normal_accounts ?? '未知'}</Descriptions.Item>
              <Descriptions.Item label="可用账号">{snapshot?.available_accounts ?? '未知'}（正常且 quota &gt; 0）</Descriptions.Item>
              <Descriptions.Item label="受限账号">{snapshot?.limited_accounts ?? '未知'}</Descriptions.Item>
              <Descriptions.Item label="异常账号">{snapshot?.abnormal_accounts ?? '未知'}</Descriptions.Item>
              <Descriptions.Item label="禁用账号">{snapshot?.disabled_accounts ?? '未知'}</Descriptions.Item>
            </Descriptions>
            <Divider style={{ margin: '12px 0' }} />
            {!quotaIsKnown ? <Text type="warning">{snapshot ? '剩余图片额度未知，停止补号；请检查远端数据。' : '尚无远端快照。保存连接配置后，可手动只读检查；不会自动检查或注册。'}</Text> : <Text type="secondary">本地刷新只读取缓存。点击“检查已保存配置”才会只读请求远端，不会验证实时上游。</Text>}
          </Card>
        </Space>
      </Col>
    </Row>

    <Card title="Web 池任务记录" extra={<Text type="secondary">父任务及注册子任务使用同一日志组件</Text>}>
      <Table<PoolTask> rowKey="id" size="small" dataSource={tasks} pagination={{ pageSize: 10, hideOnSinglePage: true }} scroll={{ x: 1000 }} locale={{ emptyText: '暂无 Web 池任务；读取页面不会创建任务' }} columns={[
        { title: '任务', dataIndex: 'id', render: (id: string) => <Text code style={{ fontSize: 11 }}>{id}</Text> },
        { title: '来源', render: (_, task) => taskKind(task) === 'chatgpt_web_pool_register' ? 'Web 池邮箱注册子任务' : 'Web 池巡检' },
        { title: '状态', dataIndex: 'status', render: taskStatus },
        { title: '进度', render: (_, task) => task.progress || `${task.registered ?? 0} / ${task.total ?? 0}` },
        { title: '成功 / 跳过 / 错误', render: (_, task) => `${task.success ?? 0} / ${task.skipped ?? 0} / ${task.error_count ?? task.errors?.length ?? 0}` },
        { title: '阶段 / 结束原因', render: (_, task) => task.meta?.reason || task.meta?.phase || '未记录' },
        { title: '创建时间', dataIndex: 'created_at', render: formatTime },
        { title: '操作', render: (_, task) => <Button size="small" onClick={() => setLogTask({ id: task.id, kind: taskKind(task) })}>查看日志</Button> },
      ]} />
    </Card>

    <Modal title={confirmation?.type === 'run' ? '确认按已保存配置执行一次？' : enableTransition ? '确认启用定时 Web 池巡检？' : '确认保存已启用的 Web 池策略？'}
      open={Boolean(confirmation)} confirmLoading={busy} closable={!busy} maskClosable={false} keyboard={!busy}
      cancelText="取消" okText={confirmation?.type === 'run' ? '确认邮箱费用并执行' : '确认邮箱费用并保存'} cancelButtonProps={{ disabled: busy }}
      onCancel={() => { if (!busy) setConfirmation(null) }} onOk={() => {
        if (confirmation?.type === 'save') void persist(confirmation.values, true)
        else if (confirmation?.type === 'run') void runSaved()
      }}>
      <Paragraph>目标不足时会使用全局邮箱服务进行邮箱注册，可能产生邮箱费用。只获取 Access Token（AT-only），禁止手机／SMS，不补 RT，也不上传 CPA／Sub2API。</Paragraph>
      <Paragraph>先补传本用途待上传账号；每轮最多 {confirmSettings?.max_attempts_per_run} 次注册尝试，{confirmSettings?.daily_registration_limit === 0 ? '每日不限（仍可能持续产生邮箱费用）' : `每日上限 ${confirmSettings?.daily_registration_limit} 次（UTC）`}，连续失败 {confirmSettings?.max_consecutive_failures} 次停止。远端额度未知时停止补号。</Paragraph>
      <Paragraph>目标：{confirmSettings?.target_value} {confirmSettings?.target_mode === 'available_accounts' ? '个远端可用账号' : '远端剩余图片额度（非 Codex）'}。</Paragraph>
      <Text type="warning">{confirmation?.type === 'run'
        ? '本次只使用已保存配置，不保存当前表单，不更改定时开关。'
        : enableTransition
          ? '保存会从关闭切换为开启并唤醒调度器，可能开始补传和注册；实际任务以运行状态为准。'
          : '定时巡检保持开启，后续轮次将使用新配置；本次保存不会额外唤醒调度器。'}</Text>
    </Modal>
    <Drawer title={logTask ? `Web 池任务日志 · ${logTask.id}` : 'Web 池任务日志'} open={Boolean(logTask)} onClose={() => setLogTask(null)} width={800} destroyOnClose>
      {logTask ? <TaskLogPanel key={logTask.id} taskId={logTask.id} kind={logTask.kind} onDone={onLogDone} /> : null}
    </Drawer>
  </Space>
}
