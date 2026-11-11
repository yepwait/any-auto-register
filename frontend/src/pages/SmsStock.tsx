import { useCallback, useState } from 'react'
import {
  Alert, App, Badge, Button, Card, Col, Descriptions, Drawer, Form,
  InputNumber, Row, Select, Space, Spin, Statistic, Switch, Table, Tag, Typography,
} from 'antd'
import { PlayCircleOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { usePollingRequest } from '@/hooks/usePollingRequest'

const { Title, Paragraph, Text } = Typography

type StockConfig = {
  enabled: boolean
  min_accounts: number
  interval_minutes: number
  max_attempts_per_run: number
  daily_attempt_limit: number
  max_consecutive_failures: number
  delay_seconds: number
  candidate_cooldown_minutes: number
  allow_login: boolean
  register_flow: 'email' | 'phone' | 'phone_with_email'
}

type StockTask = {
  id: string
  status: string
  progress: string
  success: number
  skipped: number
  errors?: string[]
  error_count?: number
  meta?: { reason?: string; phase?: string }
}

type StockStatus = {
  inventory: {
    verified_accounts: number
    pending_accounts: number
    ready_candidates: number
    missing_accounts: number
    daily_attempts_used: number
    daily_attempts_remaining: number
    budget_date_utc: string
  }
  active: StockTask | null
  tasks: StockTask[]
}

const DEFAULTS: StockConfig = {
  enabled: false, min_accounts: 10, interval_minutes: 30,
  max_attempts_per_run: 5, daily_attempt_limit: 20, max_consecutive_failures: 3,
  delay_seconds: 5, candidate_cooldown_minutes: 60, allow_login: true,
  register_flow: 'phone_with_email',
}

function taskStatus(value: string) {
  const labels: Record<string, [string, string]> = {
    pending: ['default', '等待中'], running: ['processing', '运行中'],
    done: ['success', '已结束'], failed: ['error', '未完成'], stopped: ['warning', '已停止'],
  }
  const [color, label] = labels[value] || ['default', value]
  return <Tag color={color}>{label}</Tag>
}

export default function SmsStock() {
  const { message, modal } = App.useApp()
  const [form] = Form.useForm<StockConfig>()
  const [config, setConfig] = useState<StockConfig>(DEFAULTS)
  const [initialized, setInitialized] = useState(false)
  const [status, setStatus] = useState<StockStatus | null>(null)
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)
  const [logTaskId, setLogTaskId] = useState<string | null>(null)
  const [loadError, setLoadError] = useState('')

  const initialize = useCallback(async (signal: AbortSignal) => {
    const [nextConfig, nextStatus] = await Promise.all([
      apiFetch('/sms-stock/config', { signal }) as Promise<StockConfig>,
      apiFetch('/sms-stock/status', { signal }) as Promise<StockStatus>,
    ])
    return { config: nextConfig, status: nextStatus }
  }, [])
  const requestStatus = useCallback(async (signal: AbortSignal): Promise<{ config?: StockConfig; status: StockStatus }> => ({
    status: await apiFetch('/sms-stock/status', { signal }) as StockStatus,
  }), [])
  const { loading, refresh: load, getSignal } = usePollingRequest({
    initialRequest: initialize,
    request: requestStatus,
    intervalMs: 5000,
    onSuccess: result => {
      if (result.config) {
        setConfig(result.config)
        form.setFieldsValue(result.config)
        setInitialized(true)
      }
      setStatus(result.status)
      setLoadError('')
    },
    onError: error => setLoadError(error instanceof Error ? error.message : '加载接码库存失败'),
  })

  const persist = async (values: StockConfig) => {
    const signal = getSignal()
    if (!signal || signal.aborted) return
    setSaving(true)
    try {
      const result = await apiFetch('/sms-stock/config', {
        method: 'PUT', signal, body: JSON.stringify({ config: values, confirm_cost: values.enabled }),
      }) as { config: StockConfig }
      if (signal.aborted) return
      setConfig(result.config)
      form.setFieldsValue(result.config)
      message.success(values.enabled ? '已保存并开启自动补库，将立即检查库存' : '配置已保存，自动补库已关闭')
      await load()
    } catch (error) {
      if (!signal.aborted) message.error(error instanceof Error ? error.message : '保存失败')
    } finally {
      if (!signal.aborted) setSaving(false)
    }
  }

  const save = async () => {
    let values: StockConfig
    try { values = await form.validateFields() } catch { return }
    if (getSignal()?.aborted !== false) return
    if (!values.enabled) { await persist(values); return }
    modal.confirm({
      title: '确认开启自动接码补库？',
      content: `库存不足时会使用全局接码/邮箱服务并可能产生费用。每轮最多 ${values.max_attempts_per_run} 次、每日最多 ${values.daily_attempt_limit} 次账号尝试；短信条数和费用仍受全局接码配置影响。`,
      okText: '确认费用并开启', cancelText: '取消',
      onOk: () => persist(values),
    })
  }

  const runNow = () => modal.confirm({
    title: '确认按已保存配置执行一次补库？',
    content: '先补已有账号的短信验证，没有可尝试账号时再注册并接码。此操作可能产生接码和邮箱费用，并计入每日尝试上限。',
    okText: '确认并执行', cancelText: '取消',
    onOk: async () => {
      const signal = getSignal()
      if (!signal || signal.aborted) return
      setRunning(true)
      try {
        const result = await apiFetch('/sms-stock/run', {
          method: 'POST', signal, body: JSON.stringify({ confirm_cost: true }),
        }) as { triggered: boolean; task_id?: string; reason?: string }
        if (signal.aborted) return
        if (result.task_id) {
          setLogTaskId(result.task_id)
          message.success('接码补库任务已创建')
        } else {
          message.info(result.reason || '当前无需补库')
        }
        await load()
      } catch (error) {
        if (!signal.aborted) message.error(error instanceof Error ? error.message : '创建任务失败')
      } finally { if (!signal.aborted) setRunning(false) }
    },
  })

  if (loading && !initialized) return <div style={{ padding: 80, textAlign: 'center' }}><Spin size="large" /></div>
  if (!initialized) return <Alert type="error" showIcon message="无法读取接码库存配置" description={loadError}
    action={<Button onClick={() => void load()}>重试</Button>} />
  const stock = status?.inventory

  return <Space direction="vertical" size={16} style={{ width: '100%' }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
      <div>
        <Title level={3} style={{ margin: 0 }}>接码账号自动补库</Title>
        <Paragraph type="secondary" style={{ margin: '4px 0 0' }}>库存低于目标时，先给已注册未接码账号补接码，没有可尝试账号时再注册＋接码。</Paragraph>
      </div>
      <Space>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新库存</Button>
        <Button type="primary" icon={<PlayCircleOutlined />} loading={running} disabled={Boolean(status?.active)} onClick={runNow}>立即补库</Button>
      </Space>
    </div>
    {loadError && <Alert type="error" showIcon message="库存刷新失败，以下可能是旧数据" description={loadError} />}
    <Alert type="warning" showIcon message="默认关闭；开启和立即执行都需要确认费用"
      description="复用全局接码平台、单价限制、国家和邮箱配置。每次账号尝试可能包含多次租号，请先设置全局接码单价及换号上限。读取库存/刷新页面不会租号。每日尝试限额按 UTC 计算，失败、跳过、重启也不退还已预留次数。" />
    <Row gutter={[16, 16]}>
      <Col xs={12} md={6}><Card><Statistic title="当前正常接码账号" value={stock?.verified_accounts ?? 0} suffix={`/ ${config.min_accounts}`} /></Card></Col>
      <Col xs={12} md={6}><Card><Statistic title="还需补充" value={stock?.missing_accounts ?? 0} /></Card></Col>
      <Col xs={12} md={6}><Card><Statistic title="已注册未接码" value={stock?.pending_accounts ?? 0} /><Text type="secondary">本轮可尝试 {stock?.ready_candidates ?? 0} 个</Text></Card></Col>
      <Col xs={12} md={6}><Card><Statistic title="今日账号尝试 / 上限" value={stock?.daily_attempts_used ?? 0} suffix={`/ ${config.daily_attempt_limit}`} /><Text type="secondary">UTC {stock?.budget_date_utc || '-'}</Text></Card></Col>
    </Row>
    <Row gutter={[16, 16]}>
      <Col xs={24} xl={16}>
        <Card title="补库策略（始终串行，达到目标立即停止）">
          <Form form={form} layout="vertical" initialValues={DEFAULTS}>
            <Row gutter={16}>
              <Col xs={24} sm={12}><Form.Item name="enabled" label="启用自动补库" valuePropName="checked"><Switch checkedChildren="开启" unCheckedChildren="关闭" /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="min_accounts" label="最低／目标可用 RT 账号数" rules={[{ required: true }]}><InputNumber min={1} max={5000} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="interval_minutes" label="库存检查间隔（分钟）" rules={[{ required: true }]}><InputNumber min={1} max={10080} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="max_attempts_per_run" label="每轮最多账号尝试数" rules={[{ required: true }]}><InputNumber min={1} max={100} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="daily_attempt_limit" label="每日最多账号尝试数（UTC）" rules={[{ required: true }]}><InputNumber min={1} max={1000} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="max_consecutive_failures" label="连续失败多少次停止本轮" rules={[{ required: true }]}><InputNumber min={1} max={10} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="delay_seconds" label="两次尝试间隔（秒）" rules={[{ required: true }]}><InputNumber min={0} max={3600} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="candidate_cooldown_minutes" label="同一老账号重试冷却（分钟）" rules={[{ required: true }]}><InputNumber min={1} max={10080} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="allow_login" label="补已有账号时允许重新登录" valuePropName="checked"><Switch checkedChildren="允许" unCheckedChildren="禁止" /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="register_flow" label="没有候选账号时的新注册方式" rules={[{ required: true }]} extra="只要拿到有效 RT 且账态正常就计入库存，短信验证不是必要条件">
                <Select options={[{ value: 'phone_with_email', label: '手机注册＋绑定邮箱' }, { value: 'phone', label: '手机注册' }, { value: 'email', label: '邮箱注册＋授权链接码' }]} />
              </Form.Item></Col>
            </Row>
            <Space wrap><Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={() => void save()}>保存配置</Button><Text type="secondary">立即补库使用已保存配置；关闭并保存会停止当前补库。</Text></Space>
          </Form>
        </Card>
      </Col>
      <Col xs={24} xl={8}>
        <Card title="统计与执行规则">
          <Descriptions column={1} size="small">
            <Descriptions.Item label="自动补库"><Badge status={config.enabled ? 'success' : 'default'} text={config.enabled ? '已开启' : '已关闭'} /></Descriptions.Item>
            <Descriptions.Item label="当前任务">{status?.active ? taskStatus(status.active.status) : '空闲'}</Descriptions.Item>
            <Descriptions.Item label="执行阶段">{status?.active?.meta?.phase || '-'}</Descriptions.Item>
            <Descriptions.Item label="库存口径">registered / trial / subscribed，且持有有效 RT（短信验证为独立记录，不强制）</Descriptions.Item>
            <Descriptions.Item label="不计入库存">失效、过期、删除、封禁账号；只有短信记录、缺少 RT 的账号</Descriptions.Item>
            <Descriptions.Item label="优先顺序">缺少 RT 的可恢复老账号 → 无可恢复候选时新注册</Descriptions.Item>
            <Descriptions.Item label="含 Web 池账号">Web 池（免接码注册）账号有 AT 但缺 RT，会作为可补 RT／接码候选；补到 RT 后计入库存</Descriptions.Item>
            <Descriptions.Item label="并发保护">已有 GPT 后台任务时本轮不启动；补库运行时不接受冲突任务</Descriptions.Item>
            <Descriptions.Item label="失败处理">同号冷却、连续失败熔断、每轮／每日限额</Descriptions.Item>
          </Descriptions>
        </Card>
      </Col>
    </Row>
    <Card title="补库任务历史">
      <Table<StockTask> rowKey="id" size="small" dataSource={status?.tasks || []} pagination={{ pageSize: 10 }} scroll={{ x: 850 }} columns={[
        { title: '任务', dataIndex: 'id', ellipsis: true },
        { title: '状态', dataIndex: 'status', render: taskStatus },
        { title: '尝试 / 每轮上限', dataIndex: 'progress' },
        { title: '成功 / 跳过 / 失败', render: (_, task) => `${task.success} / ${task.skipped} / ${task.error_count ?? task.errors?.length ?? 0}` },
        { title: '结束原因', render: (_, task) => task.meta?.reason || task.meta?.phase || '-' },
        { title: '操作', render: (_, task) => <Button size="small" onClick={() => setLogTaskId(task.id)}>查看日志</Button> },
      ]} />
    </Card>
    <Drawer title="接码补库日志" open={Boolean(logTaskId)} onClose={() => setLogTaskId(null)} width={800} destroyOnClose>
      {logTaskId && <TaskLogPanel taskId={logTaskId} kind="sms_stock" onDone={() => void load()} />}
    </Drawer>
  </Space>
}
