import { useCallback, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  message,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { AdminPageHeader, MetricCard } from '@/components/admin/AdminPage'
import { usePollingRequest } from '@/hooks/usePollingRequest'

const { Text, Paragraph } = Typography

const ADVANCED_FIELDS: Partial<Record<keyof InspectionFormValues, string>> = {
  concurrency: 'rate',
  delay_seconds: 'rate',
  backfill_rt_enabled: 'recovery',
  backfill_allow_login: 'recovery',
  only_missing_rt: 'recovery',
  upload_cpa: 'upload',
  upload_sub2api: 'upload',
  chat2api_enabled: 'upload',
  chat2api_protocol: 'upload',
  chat2api_url: 'upload',
  chat2api_token: 'upload',
}

type ValidationFailure = { errorFields: { name: (string | number)[] }[] }

function isValidationFailure(error: unknown): error is ValidationFailure {
  return Boolean(error && typeof error === 'object' && 'errorFields' in error && Array.isArray(error.errorFields))
}

const STATUS_OPTIONS = [
  { label: 'registered', value: 'registered' },
  { label: 'trial', value: 'trial' },
  { label: 'subscribed', value: 'subscribed' },
  { label: 'invalid', value: 'invalid' },
  { label: 'expired', value: 'expired' },
  { label: 'deleted', value: 'deleted' },
  { label: 'banned', value: 'banned' },
]

type InspectionFormValues = {
  enabled: boolean
  interval_minutes: number
  concurrency: number
  delay_seconds: number
  probe_enabled: boolean
  backfill_rt_enabled: boolean
  backfill_allow_login: boolean
  only_missing_rt: boolean
  only_sms_verified: boolean
  upload_cpa: boolean
  upload_sub2api: boolean
  chat2api_enabled: boolean
  chat2api_protocol: 'chatgpt2api' | 'webhook'
  chat2api_url: string
  chat2api_token: string
  account_statuses: string[]
  email_filter: string
  max_accounts: number
}

type InspectionConfig = Omit<InspectionFormValues, 'chat2api_token'> & {
  chat2api_token_configured?: boolean
  chat2api_token_masked?: string
}

type TaskSnapshot = {
  id: string
  platform: string
  source: string
  status: 'pending' | 'running' | 'done' | 'failed' | 'stopped'
  total: number
  progress: string
  success: number
  registered: number
  skipped: number
  errors?: string[]
  error_count?: number
  created_at: number | string | null
  updated_at: number | string | null
}

type InspectionStatus = {
  enabled: boolean
  active: TaskSnapshot | null
  last_task: TaskSnapshot | null
  tasks: TaskSnapshot[]
}

const DEFAULTS: InspectionFormValues = {
  enabled: false,
  interval_minutes: 60,
  concurrency: 1,
  delay_seconds: 5,
  probe_enabled: true,
  backfill_rt_enabled: false,
  backfill_allow_login: false,
  only_missing_rt: true,
  only_sms_verified: false,
  upload_cpa: false,
  upload_sub2api: false,
  chat2api_enabled: false,
  chat2api_protocol: 'chatgpt2api',
  chat2api_url: '',
  chat2api_token: '',
  account_statuses: ['registered', 'trial', 'subscribed'],
  email_filter: '',
  max_accounts: 0,
}

function statusTag(status: TaskSnapshot['status']) {
  const config: Record<TaskSnapshot['status'], { color: string; label: string }> = {
    pending: { color: 'default', label: '等待中' },
    running: { color: 'processing', label: '运行中' },
    done: { color: 'success', label: '已完成' },
    failed: { color: 'error', label: '失败' },
    stopped: { color: 'warning', label: '已停止' },
  }
  const item = config[status] || config.failed
  return <Tag color={item.color}>{item.label}</Tag>
}

function formatTime(value: number | string | null | undefined) {
  if (value === null || value === undefined || value === '') return '-'
  const raw = typeof value === 'number' ? value : Number(value)
  const date = Number.isFinite(raw)
    ? new Date(raw > 1_000_000_000_000 ? raw : raw * 1000)
    : new Date(String(value))
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN')
}

function toFormValues(config: Partial<InspectionConfig>): InspectionFormValues {
  return {
    ...DEFAULTS,
    ...config,
    chat2api_token: '',
    chat2api_protocol: config.chat2api_protocol === 'webhook' ? 'webhook' : 'chatgpt2api',
    account_statuses: Array.isArray(config.account_statuses) && config.account_statuses.length > 0
      ? config.account_statuses
      : DEFAULTS.account_statuses,
    chat2api_url: String(config.chat2api_url || ''),
    email_filter: String(config.email_filter || ''),
    max_accounts: Number(config.max_accounts || 0),
  }
}

export default function Inspection() {
  const [form] = Form.useForm<InspectionFormValues>()
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)
  const [config, setConfig] = useState<InspectionConfig | null>(null)
  const [status, setStatus] = useState<InspectionStatus>({ enabled: false, active: null, last_task: null, tasks: [] })
  const [logTaskId, setLogTaskId] = useState<string | null>(null)
  const [loadError, setLoadError] = useState('')
  const [advancedKeys, setAdvancedKeys] = useState<string[]>([])

  const revealValidationErrors = useCallback((error: ValidationFailure) => {
    const sections = error.errorFields.flatMap(({ name }) => {
      const section = ADVANCED_FIELDS[name[0] as keyof InspectionFormValues]
      return section ? [section] : []
    })
    if (sections.length) setAdvancedKeys(keys => [...new Set([...keys, ...sections])])
    const firstField = error.errorFields[0]?.name
    if (firstField) requestAnimationFrame(() => form.scrollToField(firstField, { block: 'center', focus: true }))
  }, [form])

  const initialize = useCallback(async (signal: AbortSignal) => {
    const [nextConfig, nextStatus] = await Promise.all([
      apiFetch('/inspection/config', { signal }) as Promise<InspectionConfig>,
      apiFetch('/inspection/status', { signal }) as Promise<InspectionStatus>,
    ])
    return { config: nextConfig, status: nextStatus }
  }, [])
  const requestStatus = useCallback(async (signal: AbortSignal): Promise<{ config?: InspectionConfig; status: InspectionStatus }> => ({
    status: await apiFetch('/inspection/status', { signal }) as InspectionStatus,
  }), [])
  const { loading, refresh: load, getSignal } = usePollingRequest({
    initialRequest: initialize,
    request: requestStatus,
    intervalMs: 2500,
    onSuccess: result => {
      if (result.config) {
        setConfig(result.config)
        form.setFieldsValue(toFormValues(result.config))
      }
      setStatus(result.status)
      setLoadError('')
    },
    onError: error => setLoadError(error instanceof Error ? error.message : '加载巡检状态失败'),
  })

  const save = useCallback(async (): Promise<boolean> => {
    const signal = getSignal()
    if (!signal || signal.aborted) return false
    try {
      await form.validateFields()
      // Include preserved fields even when an advanced panel has never been opened.
      const values = form.getFieldsValue(true) as InspectionFormValues
      if (signal.aborted) return false
      setSaving(true)
      const data: Record<string, unknown> = {
        inspection_enabled: values.enabled ? '1' : '0',
        inspection_interval_minutes: String(values.interval_minutes),
        inspection_concurrency: String(values.concurrency),
        inspection_delay_seconds: String(values.delay_seconds),
        inspection_probe_enabled: values.probe_enabled ? '1' : '0',
        inspection_backfill_rt_enabled: values.backfill_rt_enabled ? '1' : '0',
        inspection_backfill_allow_login: values.backfill_allow_login ? '1' : '0',
        inspection_only_missing_rt: values.only_missing_rt ? '1' : '0',
        inspection_only_sms_verified: values.only_sms_verified ? '1' : '0',
        inspection_upload_cpa: values.upload_cpa ? '1' : '0',
        inspection_upload_sub2api: values.upload_sub2api ? '1' : '0',
        inspection_chat2api_enabled: values.chat2api_enabled ? '1' : '0',
        inspection_chat2api_protocol: values.chat2api_protocol === 'webhook' ? 'webhook' : 'chatgpt2api',
        inspection_chat2api_url: String(values.chat2api_url || '').trim(),
        inspection_account_statuses: Array.isArray(values.account_statuses) ? values.account_statuses.join(',') : '',
        inspection_email_filter: String(values.email_filter || '').trim(),
        inspection_max_accounts: String(values.max_accounts || 0),
      }
      // 留空表示保留服务器上已有的 token，避免刷新配置时误清空密钥。
      const token = String(values.chat2api_token || '').trim()
      if (token) data.inspection_chat2api_token = token
      const response = await apiFetch('/inspection/config', {
        method: 'PUT', signal,
        body: JSON.stringify({ data }),
      }) as { config: InspectionConfig }
      if (signal.aborted) return false
      setConfig(response.config)
      form.setFieldsValue({ ...values, chat2api_token: '' })
      message.success('巡检配置已保存')
      return true
    } catch (error) {
      if (signal.aborted) return false
      if (isValidationFailure(error)) {
        revealValidationErrors(error)
        return false
      }
      message.error(error instanceof Error ? error.message : '保存巡检配置失败')
      return false
    } finally {
      if (!signal.aborted) setSaving(false)
    }
  }, [form, getSignal, revealValidationErrors])

  const runNow = async () => {
    const signal = getSignal()
    if (!signal || signal.aborted) return
    const saved = await save()
    if (!saved || signal.aborted) return
    setRunning(true)
    try {
      const values = form.getFieldsValue(true) as InspectionFormValues
      const result = await apiFetch('/inspection/run', {
        method: 'POST', signal,
        body: JSON.stringify({
          statuses: Array.isArray(values.account_statuses) ? values.account_statuses : [],
          email_filter: String(values.email_filter || ''),
          max_accounts: values.max_accounts,
          concurrency: values.concurrency,
          delay_seconds: values.delay_seconds,
          probe_enabled: values.probe_enabled,
          backfill_rt_enabled: values.backfill_rt_enabled,
          backfill_allow_login: values.backfill_allow_login,
          only_missing_rt: values.only_missing_rt,
          only_sms_verified: values.only_sms_verified,
          upload_cpa: values.upload_cpa,
          upload_sub2api: values.upload_sub2api,
          chat2api_enabled: values.chat2api_enabled,
          chat2api_protocol: values.chat2api_protocol === 'webhook' ? 'webhook' : 'chatgpt2api',
        }),
      }) as { task_id: string }
      if (signal.aborted) return
      setLogTaskId(result.task_id)
      message.success('巡检任务已创建')
      await load()
    } catch (error) {
      if (!signal.aborted) message.error(error instanceof Error ? error.message : '创建巡检任务失败')
    } finally {
      if (!signal.aborted) setRunning(false)
    }
  }

  const activeOrLast = status.active || status.last_task
  const uploadTargets = [
    config?.upload_cpa ? 'CPA' : '',
    config?.upload_sub2api ? 'Sub2API' : '',
    config?.chat2api_enabled ? 'Chat2API' : '',
  ].filter(Boolean)
  const taskErrors = activeOrLast?.error_count ?? activeOrLast?.errors?.length ?? 0

  const columns = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 190,
      render: (value: TaskSnapshot['created_at'], row: TaskSnapshot) => (
        <Tooltip title={`任务 ID：${row.id}`}>{formatTime(value)}</Tooltip>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (value: TaskSnapshot['status']) => statusTag(value),
    },
    {
      title: '处理结果',
      key: 'result',
      render: (_: unknown, row: TaskSnapshot) => (
        <div>
          <Text>成功 {row.success || 0} · 跳过 {row.skipped || 0} · 失败 {row.error_count ?? row.errors?.length ?? 0}</Text>
          <div><Text type="secondary" style={{ fontSize: 12 }}>进度 {row.progress || `${row.registered}/${row.total}`}</Text></div>
        </div>
      ),
    },
    {
      title: '日志',
      key: 'action',
      width: 110,
      render: (_: unknown, row: TaskSnapshot) => (
        <Button size="small" onClick={() => setLogTaskId(row.id)}>查看日志</Button>
      ),
    },
  ]

  if (loading && !config) {
    return <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}><Spin size="large" /></div>
  }
  if (!config) return <Alert type="error" showIcon message="无法读取巡检配置" description={loadError}
    action={<Button onClick={() => void load()}>重试</Button>} />

  return (
    <div className="admin-page">
      <AdminPageHeader
        title="Codex／RT 巡检"
        description="定时测活、按需恢复 RT，并将通过检测的账号同步到已配置的上传目标。"
        extra={<Space wrap>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
          <Button type="primary" icon={<PlayCircleOutlined />} loading={running} onClick={runNow}>立即巡检</Button>
        </Space>}
      />

      {loadError ? <Alert type="error" showIcon message="状态刷新失败，以下可能是旧数据" description={loadError} /> : null}

      <div className="admin-metrics">
        <MetricCard label="定时巡检" value={config.enabled ? '已开启' : '已关闭'} hint={`每 ${config.interval_minutes} 分钟检查`} tone={config.enabled ? 'success' : 'default'} />
        <MetricCard label={status.active ? '当前任务' : '最近任务'} value={activeOrLast ? statusTag(activeOrLast.status) : '暂无'} hint={activeOrLast ? `进度 ${activeOrLast.progress || `${activeOrLast.registered}/${activeOrLast.total}`}` : '可立即发起一轮巡检'} tone={activeOrLast?.status === 'failed' ? 'danger' : 'default'} />
        <MetricCard label={status.active ? '本轮成功' : '最近成功'} value={activeOrLast?.success ?? '—'} hint={activeOrLast ? `跳过 ${activeOrLast.skipped || 0} · 失败 ${taskErrors}` : '任务结束后显示结果'} tone="accent" />
        <MetricCard label="上传目标" value={uploadTargets.length} hint={uploadTargets.length ? uploadTargets.join(' / ') : '在高级设置中按需启用'} />
      </div>

      <div className="admin-note">
        本页不处理 Web 池用途账号；图片额度与邮箱补号请前往 <Typography.Link href="/chatgpt-web-pool">Web 池巡检</Typography.Link>。
      </div>

      <div className="admin-settings-grid">
        <Card className="admin-panel" title="巡检策略">
          <Form form={form} layout="vertical" preserve>
            <Row gutter={16}>
              <Col xs={24} sm={12}>
                <Form.Item name="enabled" label="启用定时巡检" valuePropName="checked">
                  <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12}>
                <Form.Item name="interval_minutes" label="巡检间隔（分钟）" rules={[{ required: true, message: '请输入间隔' }]}>
                  <InputNumber min={1} max={10080} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12}>
                <Form.Item name="probe_enabled" label="自动测活" valuePropName="checked">
                  <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12}>
                <Form.Item name="max_accounts" label="每轮最多账号数" extra="0 表示不限制">
                  <InputNumber min={0} max={5000} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col xs={24}>
                <Form.Item name="account_statuses" label="巡检账号状态">
                  <Select mode="multiple" options={STATUS_OPTIONS} placeholder="选择要巡检的账号状态" />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12}>
                <Form.Item name="email_filter" label="邮箱筛选" extra="留空表示不按邮箱筛选">
                  <Input placeholder="输入邮箱关键字" allowClear />
                </Form.Item>
              </Col>
              <Col xs={24} sm={12}>
                <Form.Item name="only_sms_verified" label="仅含短信验证记录的账号" valuePropName="checked" extra="开启后会排除无短信记录但持有有效 RT 的账号">
                  <Switch checkedChildren="是" unCheckedChildren="否" />
                </Form.Item>
              </Col>
            </Row>

            <Collapse
              activeKey={advancedKeys}
              onChange={keys => setAdvancedKeys(Array.isArray(keys) ? keys : [keys])}
              items={[
                {
                  key: 'rate', label: '高级 · 并发与限速', forceRender: true,
                  children: <Row gutter={16}>
                    <Col xs={24} sm={12}>
                      <Form.Item name="concurrency" label="并发账号数">
                        <InputNumber min={1} max={20} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} sm={12}>
                      <Form.Item name="delay_seconds" label="账号启动间隔（秒）">
                        <InputNumber min={0} max={3600} step={0.5} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  </Row>,
                },
                {
                  key: 'recovery', label: '高级 · RT 恢复', forceRender: true,
                  children: <>
                    <Paragraph type="secondary">认证失效时先重测，再按下方开关恢复；恢复后必须再次测活通过才会上传。</Paragraph>
                    <Row gutter={16}>
                      <Col xs={24} sm={12}>
                        <Form.Item name="backfill_rt_enabled" label="自动补 RT／恢复失效认证" valuePropName="checked">
                          <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} sm={12}>
                        <Form.Item name="only_missing_rt" label="只处理缺 RT 账号" valuePropName="checked">
                          <Switch checkedChildren="是" unCheckedChildren="否" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} sm={12}>
                        <Form.Item name="backfill_allow_login" label="补 RT 允许重新登录" valuePropName="checked">
                          <Switch checkedChildren="允许" unCheckedChildren="禁止" />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>,
                },
                {
                  key: 'upload', label: '高级 · 自动上传与接收凭证', forceRender: true,
                  children: <>
                    <Paragraph type="secondary">CPA、Sub2API 复用全局 URL / API Key；Chat2API 按下方协议发送账号快照，只填写可信的 HTTPS／内网地址并安全保管凭证。</Paragraph>
                    <Row gutter={16}>
                      <Col xs={24} sm={8}>
                        <Form.Item name="upload_cpa" label="上传 CPA" valuePropName="checked">
                          <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} sm={8}>
                        <Form.Item name="upload_sub2api" label="上传 Sub2API" valuePropName="checked">
                          <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} sm={8}>
                        <Form.Item name="chat2api_enabled" label="上传 Chat2API" valuePropName="checked">
                          <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                        </Form.Item>
                      </Col>
                      <Col xs={24}>
                        <Form.Item name="chat2api_protocol" label="Chat2API 协议">
                          <Select options={[
                            { label: 'chatgpt2api（批量 accounts）', value: 'chatgpt2api' },
                            { label: 'webhook（单账号 JSON）', value: 'webhook' },
                          ]} />
                        </Form.Item>
                      </Col>
                      <Col xs={24}>
                        <Form.Item name="chat2api_url" label="Chat2API JSON 接收地址">
                          <Input placeholder="https://your-chat2api.example/api/import" />
                        </Form.Item>
                      </Col>
                      <Col xs={24}>
                        <Form.Item name="chat2api_token" label="Chat2API Token" extra={config.chat2api_token_configured ? `已配置 ${config.chat2api_token_masked || ''}；留空则保持不变` : '可选，保存后不会回显'}>
                          <Input.Password placeholder="留空表示不修改现有 Token" autoComplete="off" />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>,
                },
              ]}
            />

            <div className="admin-save-bar" style={{ marginTop: 20 }}>
              <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={() => void save()}>保存配置</Button>
              <Text type="secondary">启用并保存后立即检查；立即巡检会先保存本页配置。</Text>
            </div>
          </Form>
        </Card>

        <Card className="admin-panel" title="任务概览">
          <Descriptions column={1} size="small">
            <Descriptions.Item label="定时巡检">
              <Badge status={config.enabled ? 'success' : 'default'} text={config.enabled ? '已开启' : '已关闭'} />
            </Descriptions.Item>
            <Descriptions.Item label="任务状态">{activeOrLast ? statusTag(activeOrLast.status) : '暂无任务'}</Descriptions.Item>
            <Descriptions.Item label="任务时间">{formatTime(activeOrLast?.created_at)}</Descriptions.Item>
            <Descriptions.Item label="处理进度">{activeOrLast ? activeOrLast.progress || `${activeOrLast.registered}/${activeOrLast.total}` : '—'}</Descriptions.Item>
            <Descriptions.Item label="失败记录"><Text type={taskErrors ? 'danger' : 'secondary'}>{taskErrors} 条</Text></Descriptions.Item>
          </Descriptions>
          {activeOrLast ? <Button style={{ marginTop: 16 }} onClick={() => setLogTaskId(activeOrLast.id)}>查看{status.active ? '当前' : '最近'}日志</Button> : null}
          <div className="admin-note" style={{ marginTop: 20 }}>概览显示已保存策略；高级区收起不会丢失 RT 恢复、上传目标或凭证配置。</div>
        </Card>
      </div>

      <Card className="admin-panel" title="巡检任务历史" extra={<Text type="secondary">最近 {status.tasks?.length || 0} 次</Text>}>
        <Table<TaskSnapshot>
          rowKey="id"
          size="small"
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          columns={columns}
          dataSource={status.tasks || []}
          locale={{ emptyText: '暂无巡检任务' }}
          scroll={{ x: 640 }}
        />
      </Card>

      <Drawer
        title="巡检日志"
        open={Boolean(logTaskId)}
        onClose={() => setLogTaskId(null)}
        width="min(760px, 100vw)"
        destroyOnClose
      >
        {logTaskId ? <>
          <Paragraph type="secondary" style={{ overflowWrap: 'anywhere' }}>任务 ID：<Text code>{logTaskId}</Text></Paragraph>
          <TaskLogPanel taskId={logTaskId} kind="inspection" />
        </> : null}
      </Drawer>
    </div>
  )
}
