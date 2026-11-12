import { useCallback, useState } from 'react'
import {
  Alert, App, Badge, Button, Card, Col, Collapse, Descriptions, Drawer, Form,
  InputNumber, Row, Select, Space, Spin, Switch, Table, Tag, Tooltip, Typography,
} from 'antd'
import { PlayCircleOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { AdminPageHeader, MetricCard } from '@/components/admin/AdminPage'
import { usePollingRequest } from '@/hooks/usePollingRequest'

const { Paragraph, Text } = Typography

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
  created_at?: number | string | null
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

const ADVANCED_FIELDS: Partial<Record<keyof StockConfig, string>> = {
  max_attempts_per_run: 'limits',
  daily_attempt_limit: 'limits',
  max_consecutive_failures: 'limits',
  delay_seconds: 'rate',
  candidate_cooldown_minutes: 'rate',
  allow_login: 'recovery',
  register_flow: 'recovery',
}

type ValidationFailure = { errorFields: { name: (string | number)[] }[] }

function isValidationFailure(error: unknown): error is ValidationFailure {
  return Boolean(error && typeof error === 'object' && 'errorFields' in error && Array.isArray(error.errorFields))
}

function taskStatus(value: string) {
  const labels: Record<string, [string, string]> = {
    pending: ['default', '等待中'], running: ['processing', '运行中'],
    done: ['success', '已结束'], failed: ['error', '未完成'], stopped: ['warning', '已停止'],
  }
  const [color, label] = labels[value] || ['default', value]
  return <Tag color={color}>{label}</Tag>
}

function formatTime(value: StockTask['created_at']) {
  if (value === null || value === undefined || value === '') return '未提供'
  const raw = typeof value === 'number' ? value : Number(value)
  const date = Number.isFinite(raw)
    ? new Date(raw > 1_000_000_000_000 ? raw : raw * 1000)
    : new Date(String(value))
  return Number.isNaN(date.getTime()) ? '未提供' : date.toLocaleString('zh-CN')
}

function FeeConfirmation({ config, manual = false }: { config: StockConfig; manual?: boolean }) {
  return <>
    <Paragraph>{manual ? '本次使用已保存配置。' : '保存开启后会立即检查库存。'}库存不足时先恢复缺少 RT 的已有账号，无可恢复候选时再新注册；有效 RT 且账态正常才计入库存，短信验证不是必要条件。</Paragraph>
    <Paragraph>将复用全局接码平台、国家、单价限制和邮箱服务，可能产生邮箱／接码费用；每次账号尝试可能包含多次租号，请先核对全局接码单价及换号上限。</Paragraph>
    <ul style={{ paddingInlineStart: 20, margin: '0 0 12px' }}>
      <li>每轮最多 {config.max_attempts_per_run} 次账号尝试，达到目标 {config.min_accounts} 个可用 RT 账号即停止。</li>
      <li>每日最多 {config.daily_attempt_limit} 次，按 UTC 日期计算；失败、跳过、重启均不退还已预留次数。</li>
      <li>连续失败 {config.max_consecutive_failures} 次停止本轮，短信条数和实际费用仍受全局接码配置影响。</li>
    </ul>
    <Text type="secondary">取消仅放弃本次操作，不会更改已保存配置或已有任务；仅查看库存或刷新页面不会租号。</Text>
  </>
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
  const [advancedKeys, setAdvancedKeys] = useState<string[]>([])

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
    onError: error => setLoadError(error instanceof Error ? error.message : '加载可用 RT 库存失败'),
  })

  const revealValidationErrors = (error: ValidationFailure) => {
    const sections = error.errorFields.flatMap(({ name }) => {
      const section = ADVANCED_FIELDS[name[0] as keyof StockConfig]
      return section ? [section] : []
    })
    if (sections.length) setAdvancedKeys(keys => [...new Set([...keys, ...sections])])
    const firstField = error.errorFields[0]?.name
    if (firstField) requestAnimationFrame(() => form.scrollToField(firstField, { block: 'center', focus: true }))
  }

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
    try {
      await form.validateFields()
      // Preserve server values from every advanced panel, even before its first expansion.
      values = form.getFieldsValue(true) as StockConfig
    } catch (error) {
      if (isValidationFailure(error)) revealValidationErrors(error)
      return
    }
    if (getSignal()?.aborted !== false) return
    if (!values.enabled) { await persist(values); return }
    modal.confirm({
      title: '确认开启自动补库？',
      content: <FeeConfirmation config={values} />,
      okText: '确认费用并开启', cancelText: '取消',
      onOk: () => persist(values),
    })
  }

  const runNow = () => modal.confirm({
    title: '确认按已保存配置执行一次补库？',
    content: <FeeConfirmation config={config} manual />,
    okText: '确认费用并执行', cancelText: '取消',
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
          message.success('补库任务已创建')
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
  if (!initialized) return <Alert type="error" showIcon message="无法读取可用 RT 库存配置" description={loadError}
    action={<Button onClick={() => void load()}>重试</Button>} />
  const stock = status?.inventory
  const activeTask = status?.active

  return <div className="admin-page">
    <AdminPageHeader
      title="可用 RT 库存"
      description="库存低于目标时优先恢复已有账号、按需新注册，账态正常且持有有效 RT 即计入，短信非必需。"
      extra={<Space wrap>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新库存</Button>
        <Button type="primary" icon={<PlayCircleOutlined />} loading={running} disabled={Boolean(status?.active)} onClick={runNow}>立即补库</Button>
      </Space>}
    />

    {loadError ? <Alert type="error" showIcon message="库存刷新失败，以下可能是旧数据" description={loadError} /> : null}

    <div className="admin-metrics">
      <MetricCard label="可用 RT 账号" value={stock?.verified_accounts ?? 0} hint={`目标 ${config.min_accounts} 个 · 短信非必需`} tone="accent" />
      <MetricCard label="库存缺口" value={stock?.missing_accounts ?? 0} hint="达到已保存目标即停止补库" tone={stock?.missing_accounts ? 'warning' : 'success'} />
      <MetricCard label="待恢复账号" value={stock?.pending_accounts ?? 0} hint={`当前可尝试 ${stock?.ready_candidates ?? 0} 个`} />
      <MetricCard label="今日账号尝试" value={<>{stock?.daily_attempts_used ?? 0}<Text type="secondary" style={{ fontSize: 16 }}> / {config.daily_attempt_limit}</Text></>} hint={`UTC ${stock?.budget_date_utc || '—'} · 剩余 ${stock?.daily_attempts_remaining ?? 0} 次`} />
    </div>

    <div className="admin-settings-grid">
      <Card className="admin-panel" title="补库策略">
        <Form form={form} layout="vertical" preserve>
          <Row gutter={16}>
            <Col xs={24} sm={12}>
              <Form.Item name="enabled" label="启用自动补库" valuePropName="checked">
                <Switch checkedChildren="开启" unCheckedChildren="关闭" />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="min_accounts" label="目标可用 RT 账号数" rules={[{ required: true, message: '请输入目标账号数' }]}>
                <InputNumber min={1} max={5000} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="interval_minutes" label="库存检查间隔（分钟）" rules={[{ required: true, message: '请输入检查间隔' }]}>
                <InputNumber min={1} max={10080} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <div className="admin-note" style={{ marginBottom: 16 }}>可能产生邮箱/接码费用</div>
          <Collapse
            activeKey={advancedKeys}
            onChange={keys => setAdvancedKeys(Array.isArray(keys) ? keys : [keys])}
            items={[
              {
                key: 'limits', label: '高级 · 每轮 / 每日限额与失败保护', forceRender: true,
                children: <>
                  <Paragraph type="secondary">每日尝试限额按 UTC 日期计算；失败、跳过、重启均不退还已预留次数。连续失败达到阈值后停止本轮。</Paragraph>
                  <Row gutter={16}>
                    <Col xs={24} sm={12}>
                      <Form.Item name="max_attempts_per_run" label="每轮最多账号尝试数" rules={[{ required: true, message: '请输入每轮尝试上限' }]}>
                        <InputNumber min={1} max={100} precision={0} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} sm={12}>
                      <Form.Item name="daily_attempt_limit" label="每日最多账号尝试数（UTC）" rules={[{ required: true, message: '请输入每日尝试上限' }]}>
                        <InputNumber min={1} max={1000} precision={0} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} sm={12}>
                      <Form.Item name="max_consecutive_failures" label="连续失败多少次停止本轮" rules={[{ required: true, message: '请输入连续失败阈值' }]}>
                        <InputNumber min={1} max={10} precision={0} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  </Row>
                </>,
              },
              {
                key: 'rate', label: '高级 · 尝试间隔与账号冷却', forceRender: true,
                children: <Row gutter={16}>
                  <Col xs={24} sm={12}>
                    <Form.Item name="delay_seconds" label="两次尝试间隔（秒）" rules={[{ required: true, message: '请输入尝试间隔' }]}>
                      <InputNumber min={0} max={3600} style={{ width: '100%' }} />
                    </Form.Item>
                  </Col>
                  <Col xs={24} sm={12}>
                    <Form.Item name="candidate_cooldown_minutes" label="同一老账号重试冷却（分钟）" rules={[{ required: true, message: '请输入账号冷却时间' }]}>
                      <InputNumber min={1} max={10080} precision={0} style={{ width: '100%' }} />
                    </Form.Item>
                  </Col>
                </Row>,
              },
              {
                key: 'recovery', label: '高级 · RT 恢复与新注册方式', forceRender: true,
                children: <Row gutter={16}>
                  <Col xs={24} sm={12}>
                    <Form.Item name="allow_login" label="恢复已有账号时允许重新登录" valuePropName="checked">
                      <Switch checkedChildren="允许" unCheckedChildren="禁止" />
                    </Form.Item>
                  </Col>
                  <Col xs={24}>
                    <Form.Item name="register_flow" label="没有候选账号时的新注册方式" rules={[{ required: true, message: '请选择新注册方式' }]} extra="账态正常且持有有效 RT 即可计入库存，短信验证不是必要条件">
                      <Select options={[{ value: 'phone_with_email', label: '手机注册＋绑定邮箱' }, { value: 'phone', label: '手机注册' }, { value: 'email', label: '邮箱注册＋授权链接码' }]} />
                    </Form.Item>
                  </Col>
                </Row>,
              },
            ]}
          />
          <div className="admin-save-bar" style={{ marginTop: 20 }}>
            <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={() => void save()}>保存配置</Button>
            <Text type="secondary">立即补库使用已保存配置；关闭并保存会停止当前补库。</Text>
          </div>
        </Form>
      </Card>

      <Card className="admin-panel" title="运行概览">
        <Descriptions column={1} size="small">
          <Descriptions.Item label="自动补库"><Badge status={config.enabled ? 'success' : 'default'} text={config.enabled ? '已开启' : '已关闭'} /></Descriptions.Item>
          <Descriptions.Item label="当前任务">{status?.active ? taskStatus(status.active.status) : '空闲'}</Descriptions.Item>
          <Descriptions.Item label="执行阶段">{status?.active?.meta?.phase || '—'}</Descriptions.Item>
          <Descriptions.Item label="执行方式">始终串行，达到目标立即停止</Descriptions.Item>
          <Descriptions.Item label="可用口径">registered / trial / subscribed，且持有有效 RT；短信验证为独立记录</Descriptions.Item>
        </Descriptions>
        {activeTask ? <Button style={{ marginTop: 16 }} onClick={() => setLogTaskId(activeTask.id)}>查看当前日志</Button> : null}
        <Collapse
          ghost
          style={{ marginTop: 16 }}
          items={[{
            key: 'rules', label: '查看库存口径与执行保护',
            children: <Descriptions column={1} size="small">
              <Descriptions.Item label="不计入库存">失效、过期、删除、封禁账号；只有短信记录但缺少 RT 的账号</Descriptions.Item>
              <Descriptions.Item label="优先顺序">缺少 RT 的可恢复老账号 → 无可恢复候选时新注册</Descriptions.Item>
              <Descriptions.Item label="Web 池账号">免接码注册账号有 AT 但缺 RT 时，可能成为补 RT／接码候选；补到有效 RT 且账态正常才计入库存</Descriptions.Item>
              <Descriptions.Item label="并发保护">已有 GPT 后台任务时本轮不启动；补库运行时不接受冲突任务</Descriptions.Item>
              <Descriptions.Item label="停止与保护">同号冷却、连续失败熔断、每轮／每日限额；关闭自动补库并保存可停止当前任务</Descriptions.Item>
              <Descriptions.Item label="只读操作">查看库存和刷新页面不会租号</Descriptions.Item>
            </Descriptions>,
          }]}
        />
      </Card>
    </div>

    <Card className="admin-panel" title="补库任务历史" extra={<Text type="secondary">最近 {status?.tasks?.length || 0} 次</Text>}>
      <Table<StockTask>
        rowKey="id"
        size="small"
        dataSource={status?.tasks || []}
        pagination={{ pageSize: 10, hideOnSinglePage: true }}
        locale={{ emptyText: '暂无补库任务' }}
        scroll={{ x: 680 }}
        columns={[
          { title: '时间', dataIndex: 'created_at', width: 190, render: (value: StockTask['created_at'], task: StockTask) => <Tooltip title={`任务 ID：${task.id}`}>{formatTime(value)}</Tooltip> },
          { title: '状态', dataIndex: 'status', width: 100, render: taskStatus },
          { title: '处理结果', render: (_: unknown, task: StockTask) => <div>
            <Text>成功 {task.success} · 跳过 {task.skipped} · 失败 {task.error_count ?? task.errors?.length ?? 0}</Text>
            <div><Text type="secondary" style={{ fontSize: 12 }}>尝试 {task.progress || '—'} · {task.meta?.reason || task.meta?.phase || '暂无结束原因'}</Text></div>
          </div> },
          { title: '日志', width: 110, render: (_: unknown, task: StockTask) => <Button size="small" onClick={() => setLogTaskId(task.id)}>查看日志</Button> },
        ]}
      />
    </Card>
    <Drawer title="补库日志" open={Boolean(logTaskId)} onClose={() => setLogTaskId(null)} width="min(800px, 100vw)" destroyOnClose>
      {logTaskId ? <>
        <Paragraph type="secondary" style={{ overflowWrap: 'anywhere' }}>任务 ID：<Text code>{logTaskId}</Text></Paragraph>
        <TaskLogPanel taskId={logTaskId} kind="sms_stock" onDone={() => void load()} />
      </> : null}
    </Drawer>
  </div>
}
