import { useCallback, useMemo, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
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
  Typography,
} from 'antd'
import {
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { usePollingRequest } from '@/hooks/usePollingRequest'

const { Text, Title, Paragraph } = Typography

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
      const values = await form.validateFields()
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
      if (signal.aborted || (error && typeof error === 'object' && 'errorFields' in error)) return false
      message.error(error instanceof Error ? error.message : '保存巡检配置失败')
      return false
    } finally {
      if (!signal.aborted) setSaving(false)
    }
  }, [form, getSignal])

  const runNow = async () => {
    const signal = getSignal()
    if (!signal || signal.aborted) return
    const saved = await save()
    if (!saved || signal.aborted) return
    setRunning(true)
    try {
      const values = form.getFieldsValue()
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
  const uploadCount = useMemo(
    () => [config?.upload_cpa, config?.upload_sub2api, config?.chat2api_enabled].filter(Boolean).length,
    [config],
  )

  const columns = [
    {
      title: '任务',
      dataIndex: 'id',
      key: 'id',
      render: (value: string) => <Text code style={{ fontSize: 11 }}>{value}</Text>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (value: TaskSnapshot['status']) => statusTag(value),
    },
    {
      title: '进度',
      key: 'progress',
      render: (_: unknown, row: TaskSnapshot) => `${row.progress || `${row.registered}/${row.total}`}`,
    },
    {
      title: '成功 / 跳过 / 失败',
      key: 'result',
      render: (_: unknown, row: TaskSnapshot) => `${row.success || 0} / ${row.skipped || 0} / ${row.error_count ?? row.errors?.length ?? 0}`,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (value: TaskSnapshot['created_at']) => formatTime(value),
    },
    {
      title: '操作',
      key: 'action',
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
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <Title level={3} style={{ margin: 0 }}>Codex／RT 巡检中心</Title>
          <Paragraph type="secondary" style={{ margin: '4px 0 0' }}>
            原 Codex／RT 巡检：定时测活、按需补 RT，并同步到 CPA、Sub2API 或自定义 Chat2API；排除 Web 池用途账号。
            Web 图片账号池请使用 <Typography.Link href="/chatgpt-web-pool">独立 Web 池巡检入口</Typography.Link>，不在此处补 RT 或上传。
          </Paragraph>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
          <Button type="primary" icon={<PlayCircleOutlined />} loading={running} onClick={runNow}>立即巡检</Button>
        </Space>
      </div>

      {loadError && <Alert type="error" showIcon message="状态刷新失败，以下可能是旧数据" description={loadError} />}
      <Alert
        type="info"
        showIcon
        message="原 Codex／RT 巡检不处理 Web 池用途账号"
        description={<>Web 池账号不会进入本页的测活、补 RT 或上传流程；请前往 <Typography.Link href="/chatgpt-web-pool">Web 池巡检</Typography.Link> 管理远端图片额度和邮箱补号。原 Chat2API 仍按配置的 URL 发送标准 JSON 账号快照，请只填写可信的 HTTPS／内网地址并安全保存凭证。</>}
      />

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={16}>
          <Card title={<Space><SettingOutlined />巡检策略</Space>}>
            <Form form={form} layout="vertical">
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
                  <Form.Item name="concurrency" label="并发账号数">
                    <InputNumber min={1} max={20} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={12}>
                  <Form.Item name="delay_seconds" label="账号启动间隔（秒）">
                    <InputNumber min={0} max={3600} step={0.5} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={12}>
                  <Form.Item name="account_statuses" label="巡检账号状态">
                    <Select mode="multiple" options={STATUS_OPTIONS} placeholder="选择要巡检的账号状态" />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={12}>
                  <Form.Item name="max_accounts" label="每轮最多账号数" extra="0 表示不限制">
                    <InputNumber min={0} max={5000} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Form.Item name="email_filter" hidden>
                  <Input />
                </Form.Item>
              </Row>

              <Divider orientation="left">执行动作</Divider>
              <Row gutter={[16, 8]}>
                <Col xs={24} sm={12}>
                  <Form.Item name="probe_enabled" label="自动测活" valuePropName="checked">
                    <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={12}>
                  <Form.Item name="backfill_rt_enabled" label="自动补 RT／恢复失效认证" valuePropName="checked" extra="认证失效先重测，再按此开关恢复；恢复后必须再次测活通过才上传">
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
                <Col xs={24} sm={12}>
                  <Form.Item name="only_sms_verified" label="只巡检已接过码账号" valuePropName="checked" extra="仅选择有短信验证记录的账号；无短信但已持有有效 RT 的账号会被排除，按需开启">
                    <Switch checkedChildren="是" unCheckedChildren="否" />
                  </Form.Item>
                </Col>
              </Row>

              <Divider orientation="left">自动上传</Divider>
              <Row gutter={[16, 8]}>
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
                <Col xs={24} sm={12}>
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
                  <Form.Item name="chat2api_token" label="Chat2API Token" extra={config?.chat2api_token_configured ? `已配置 ${config.chat2api_token_masked || ''}；留空则保持不变` : '可选，保存后不会回显'}>
                    <Input.Password placeholder="留空表示不修改现有 Token" />
                  </Form.Item>
                </Col>
              </Row>

              <Space>
                <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={() => void save()}>保存配置</Button>
                <Text type="secondary">启用后会立即检查，之后按设定间隔运行。</Text>
              </Space>
            </Form>
          </Card>
        </Col>

        <Col xs={24} xl={8}>
          <Card title="运行概览">
            <Descriptions column={1} size="small">
              <Descriptions.Item label="定时巡检">
                <Badge status={config?.enabled ? 'success' : 'default'} text={config?.enabled ? '已开启' : '已关闭'} />
              </Descriptions.Item>
              <Descriptions.Item label="巡检间隔">{config?.interval_minutes || 60} 分钟</Descriptions.Item>
              <Descriptions.Item label="上传目标">{uploadCount > 0 ? `${uploadCount} 个已启用` : '未启用'}</Descriptions.Item>
              <Descriptions.Item label="最近任务">{activeOrLast ? statusTag(activeOrLast.status) : '暂无'}</Descriptions.Item>
            </Descriptions>
            <Divider style={{ margin: '16px 0' }} />
            <Text type="secondary" style={{ fontSize: 12 }}>
              CPA、Sub2API 使用全局配置中的 URL/API Key；巡检页面只负责选择是否在本轮执行上传。
            </Text>
          </Card>
        </Col>
      </Row>

      <Card title="巡检任务历史" extra={<Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>}>
        <Table<TaskSnapshot>
          rowKey="id"
          size="small"
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          columns={columns}
          dataSource={status.tasks || []}
          locale={{ emptyText: '暂无巡检任务' }}
          scroll={{ x: 760 }}
        />
      </Card>

      <Drawer
        title={logTaskId ? `巡检日志 · ${logTaskId}` : '巡检日志'}
        open={Boolean(logTaskId)}
        onClose={() => setLogTaskId(null)}
        width={760}
        destroyOnClose
      >
        {logTaskId ? <TaskLogPanel taskId={logTaskId} kind="inspection" /> : null}
      </Drawer>
    </div>
  )
}
