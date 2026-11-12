import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, Card, Form, Input, InputNumber, Modal, Segmented, Select, Space, Table, Tabs, Tag, Typography, message,
} from 'antd'
import {
  CreditCardOutlined, DeleteOutlined, FileTextOutlined, LinkOutlined, PlusOutlined, ReloadOutlined, SettingOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import {
  addPaymentCard, createPaymentTask, deletePaymentCard, getPaymentSettings, listPaymentCards, listPaymentChannels, resetPaymentCardUses, savePaymentProxy,
  type PaymentCard, type PaymentChannel, type PaymentOperation, type PaymentResult,
} from '@/api/payments'
import { PaymentOptionsForm } from '@/components/payments/PaymentOptionsForm'
import { PaymentResultsTable } from '@/components/payments/PaymentResultsTable'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { AdminPageHeader } from '@/components/admin/AdminPage'

const { Text } = Typography

interface AccountRow { id: number; email: string; status: string; token?: string; extra_json?: string }
interface PaymentTaskRow { id: string; source?: string; status: string; total: number; success: number; updated_at?: string; meta?: { operation?: PaymentOperation; channel?: string; account_ids?: number[]; options?: Record<string, unknown>; results?: PaymentResult[] } }

function defaults(channel: PaymentChannel | undefined, operation: PaymentOperation) {
  return Object.fromEntries((channel?.option_schema?.[operation] || []).filter((field) => field.default !== undefined).map((field) => [field.key, field.default]))
}

function proxyKey(operation: PaymentOperation) {
  return operation === 'link' ? 'link_proxy' : 'pay_proxy'
}

function statusTag(status: string) {
  const config: Record<string, { color: string; label: string }> = {
    pending: { color: 'default', label: '等待中' }, running: { color: 'processing', label: '运行中' }, done: { color: 'success', label: '完成' }, failed: { color: 'error', label: '失败' }, stopped: { color: 'warning', label: '已停止' },
  }
  const item = config[status] || { color: 'default', label: status }
  return <Tag color={item.color}>{item.label}</Tag>
}

export default function Payments() {
  const [tab, setTab] = useState('workbench')
  const [accounts, setAccounts] = useState<AccountRow[]>([])
  const [accountSearch, setAccountSearch] = useState('')
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])
  const [channels, setChannels] = useState<PaymentChannel[]>([])
  const [cards, setCards] = useState<PaymentCard[]>([])
  const [channelName, setChannelName] = useState('direct')
  const [operation, setOperation] = useState<PaymentOperation>('link')
  const [options, setOptions] = useState<Record<string, unknown>>({})
  const [paymentProxies, setPaymentProxies] = useState({ link: '', pay: '' })
  const proxySaveTimer = useRef<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [tasks, setTasks] = useState<PaymentTaskRow[]>([])
  const [cardModalOpen, setCardModalOpen] = useState(false)
  const [cardForm] = Form.useForm()

  const channel = useMemo(() => channels.find((item) => item.name === channelName), [channels, channelName])
  const filteredAccounts = useMemo(() => {
    const query = accountSearch.trim().toLowerCase()
    return query ? accounts.filter((account) => account.email.toLowerCase().includes(query)) : accounts
  }, [accounts, accountSearch])

  const loadAccounts = useCallback(async () => {
    const data = await apiFetch('/accounts?platform=chatgpt&page=1&page_size=1000') as { items?: AccountRow[] }
    setAccounts(data.items || [])
  }, [])

  const loadPaymentConfig = useCallback(async () => {
    const [nextChannels, nextCards, settings] = await Promise.all([listPaymentChannels(), listPaymentCards('direct'), getPaymentSettings()])
    setChannels(nextChannels)
    setCards(nextCards)
    const first = nextChannels[0]
    const nextName = first?.name || 'direct'
    const nextOperation: PaymentOperation = first?.operations?.includes('link') ? 'link' : 'pay'
    setChannelName(nextName)
    setOperation(nextOperation)
    setPaymentProxies({ link: settings.linkProxy, pay: settings.payProxy })
    setOptions({ ...defaults(first, nextOperation), [proxyKey(nextOperation)]: nextOperation === 'link' ? settings.linkProxy : settings.payProxy })
  }, [])

  useEffect(() => () => {
    if (proxySaveTimer.current !== null) window.clearTimeout(proxySaveTimer.current)
  }, [])

  const loadTasks = useCallback(async () => {
    const data = await apiFetch('/tasks') as PaymentTaskRow[]
    setTasks((data || []).filter((item) => item.source === 'payment').sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || ''))))
  }, [])

  useEffect(() => {
    // Initial data synchronization is intentionally started after mount.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    Promise.all([loadAccounts(), loadPaymentConfig(), loadTasks()]).catch((error) => message.error(error instanceof Error ? error.message : '加载支付中心失败')).finally(() => setLoading(false))
  }, [loadAccounts, loadPaymentConfig, loadTasks])

  useEffect(() => {
    if (!taskId) return
    const poll = window.setInterval(() => {
      loadTasks().catch(() => {})
    }, 2500)
    return () => window.clearInterval(poll)
  }, [taskId, loadTasks])

  const execute = async () => {
    const ids = selectedRowKeys.map(Number)
    if (ids.length === 0) { message.info('请先选择账号'); return }
    const required = (channel?.option_schema?.[operation] || []).filter((field) => field.required)
    const missing = required.find((field) => options[field.key] === undefined || options[field.key] === '')
    if (missing) { message.error(`请填写${missing.label}`); return }
    try {
      const result = await createPaymentTask({ account_ids: ids, operation, channel: channelName, options, concurrency: 1, delay_seconds: 0 })
      setTaskId(result.task_id)
      setTab('records')
      await loadTasks()
      message.success('支付任务已创建')
    } catch (error) { message.error(error instanceof Error ? error.message : '创建任务失败') }
  }

  const addCard = async () => {
    try {
      await addPaymentCard('direct', await cardForm.validateFields())
      setCardModalOpen(false)
      cardForm.resetFields()
      setCards(await listPaymentCards('direct'))
      message.success('卡片已添加')
    } catch (error) {
      if (error instanceof Error) message.error(error.message)
    }
  }

  const selectedTask = tasks.find((task) => task.id === taskId)

  const retryTask = async (task: PaymentTaskRow) => {
    const meta = task.meta || {}
    if (!meta.account_ids?.length || !meta.channel || !meta.operation) return
    try {
      const next = await createPaymentTask({ account_ids: meta.account_ids, operation: meta.operation, channel: meta.channel, options: meta.options || {} })
      setTaskId(next.task_id)
      await loadTasks()
      message.success('重试任务已创建')
    } catch (error) { message.error(error instanceof Error ? error.message : '重试失败') }
  }

  return (
    <div className="admin-page">
      <AdminPageHeader title="支付中心" kicker="PAYMENTS" description="选择账号、配置渠道，按任务查看提链与支付结果。" extra={
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => { setLoading(true); Promise.all([loadAccounts(), loadPaymentConfig(), loadTasks()]).finally(() => setLoading(false)) }}>刷新</Button>
      } />

      <Tabs activeKey={tab} onChange={setTab} items={[
        { key: 'workbench', label: <Space><PlayIcon /><span>操作台</span></Space> },
        { key: 'records', label: <Space><FileTextOutlined /><span>执行记录</span></Space> },
        { key: 'channels', label: <Space><SettingOutlined /><span>渠道管理</span></Space> },
      ]} />

      {tab === 'workbench' ? (
        <div className="admin-settings-grid">
          <Card className="admin-panel" title={<Space><span>ChatGPT 账号</span><Tag>{filteredAccounts.length}</Tag></Space>}>
            <Input.Search placeholder="搜索邮箱" allowClear onChange={(event) => setAccountSearch(event.target.value)} style={{ width: '100%', maxWidth: 360, marginBottom: 16 }} />
            <Table<AccountRow>
              size="small"
              rowKey="id"
              loading={loading}
              dataSource={filteredAccounts}
              rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys }}
              pagination={{ pageSize: 12, showSizeChanger: false }}
              scroll={{ x: 520 }}
              columns={[
                { title: '邮箱', dataIndex: 'email', ellipsis: true },
                { title: '状态', dataIndex: 'status', width: 100, render: (value: string) => <Tag>{value || '-'}</Tag> },
                { title: 'Token', key: 'token', width: 90, render: (_: unknown, row: AccountRow) => <Tag color={row.token ? 'default' : 'warning'}>{row.token ? '已保存' : '缺少'}</Tag> },
              ]}
            />
          </Card>
          <Card className="admin-panel" title="操作配置">
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <div><Text type="secondary">操作</Text><Segmented block value={operation} options={[{ value: 'link', label: <><LinkOutlined /> 提链</> }, { value: 'pay', label: <><CreditCardOutlined /> 支付</> }]} onChange={(value) => { const next = value as PaymentOperation; setOperation(next); setOptions({ ...defaults(channel, next), [proxyKey(next)]: next === 'link' ? paymentProxies.link : paymentProxies.pay }) }} style={{ marginTop: 8 }} /></div>
              <div><Text type="secondary">支付渠道</Text><Select value={channelName} onChange={(value) => { const next = channels.find((item) => item.name === value); setChannelName(value); setOptions({ ...defaults(next, operation), [proxyKey(operation)]: operation === 'link' ? paymentProxies.link : paymentProxies.pay }) }} options={channels.map((item) => ({ value: item.name, label: item.display_name }))} style={{ width: '100%', marginTop: 8 }} /></div>
              {operation === 'pay' && cards.length === 0 ? <Alert type="warning" showIcon message="暂无可用卡片" /> : null}
              <PaymentOptionsForm fields={channel?.option_schema?.[operation] || []} values={options} cards={cards} onChange={(key, value) => {
                setOptions((previous) => ({ ...previous, [key]: value }))
                if ((key === 'link_proxy' || key === 'pay_proxy') && channelName === 'direct') {
                  const proxy = String(value || '')
                  const kind = key === 'link_proxy' ? 'link' : 'pay'
                  setPaymentProxies((previous) => ({ ...previous, [kind]: proxy }))
                  if (proxySaveTimer.current !== null) window.clearTimeout(proxySaveTimer.current)
                  proxySaveTimer.current = window.setTimeout(() => {
                    savePaymentProxy(kind, proxy).catch(() => {})
                  }, 500)
                }
              }} />
              <div style={{ borderTop: '1px solid var(--border)', paddingTop: 14 }}>
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  <Text type="secondary">已选择 {selectedRowKeys.length} 个账号</Text>
                  <Button type="primary" block icon={operation === 'link' ? <LinkOutlined /> : <CreditCardOutlined />} onClick={execute} disabled={selectedRowKeys.length === 0}>{operation === 'link' ? '开始提链' : '开始支付'}</Button>
                </Space>
              </div>
            </Space>
          </Card>
        </div>
      ) : null}

      {tab === 'records' ? (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Table<PaymentTaskRow> rowKey="id" size="small" dataSource={tasks} pagination={{ pageSize: 10 }} columns={[
            { title: '任务', dataIndex: 'id', render: (value: string) => <Text code>{value}</Text> },
            { title: '操作', key: 'operation', render: (_: unknown, row) => `${row.meta?.channel || '-'} · ${row.meta?.operation === 'pay' ? '支付' : '提链'}` },
            { title: '进度', key: 'progress', render: (_: unknown, row) => `${row.success || 0}/${row.total || 0}` },
            { title: '状态', dataIndex: 'status', render: statusTag },
            { title: '结果', key: 'results', render: (_: unknown, row) => `${row.meta?.results?.filter((item) => item.ok).length || 0} 成功` },
            { title: '操作', key: 'action', render: (_: unknown, row) => <Space size={4}><Button size="small" icon={<FileTextOutlined />} onClick={() => setTaskId(row.id)}>查看</Button><Button size="small" icon={<ReloadOutlined />} onClick={() => retryTask(row)}>重试</Button></Space> },
          ]} />
          {selectedTask ? <Card title={<Space><FileTextOutlined /><span>{selectedTask.id}</span></Space>} extra={<Button onClick={() => setTaskId(null)}>关闭</Button>}><TaskLogPanel taskId={selectedTask.id} kind="payment" operation={selectedTask.meta?.operation} onDone={loadTasks} /><PaymentResultsTable results={selectedTask.meta?.results || []} /></Card> : null}
        </Space>
      ) : null}

      {tab === 'channels' ? (
        <Card title="直卡卡片库" extra={<Space><Button onClick={async () => { await resetPaymentCardUses(); setCards(await listPaymentCards('direct')); message.success('使用次数已重置') }}>重置使用次数</Button><Button type="primary" icon={<PlusOutlined />} onClick={() => setCardModalOpen(true)}>添加卡片</Button></Space>}>
          <Table<PaymentCard> size="small" rowKey="id" dataSource={cards} pagination={false} columns={[
            { title: '品牌', dataIndex: 'brand', width: 90 }, { title: '卡片', key: 'card', render: (_: unknown, row) => `•••• ${row.last4}` }, { title: '名称', dataIndex: 'name' }, { title: '使用次数', key: 'uses', render: (_: unknown, row) => `${row.uses}/${row.max_uses}` }, { title: '备注', dataIndex: 'note' }, { title: '操作', key: 'action', width: 80, render: (_: unknown, row) => <Button danger type="text" icon={<DeleteOutlined />} aria-label="删除卡片" onClick={async () => { await deletePaymentCard('direct', row.id); setCards(await listPaymentCards('direct')) }} /> },
          ]} />
        </Card>
      ) : null}

      <Modal title="添加卡片" open={cardModalOpen} onCancel={() => setCardModalOpen(false)} onOk={addCard} okText="添加">
        <Form form={cardForm} layout="vertical">
          <Form.Item name="number" label="卡号" rules={[{ required: true }]}><Input.Password /></Form.Item>
          <Space style={{ display: 'flex' }}>
            <Form.Item name="exp_month" label="月份" rules={[{ required: true }]}><Input placeholder="12" /></Form.Item>
            <Form.Item name="exp_year" label="年份" rules={[{ required: true }]}><Input placeholder="30" /></Form.Item>
            <Form.Item name="cvc" label="CVC" rules={[{ required: true }]}><Input.Password /></Form.Item>
          </Space>
          <Form.Item name="name" label="持卡人"><Input /></Form.Item>
          <Space style={{ display: 'flex' }}>
            <Form.Item name="brand" label="品牌"><Input placeholder="VISA" /></Form.Item>
            <Form.Item name="max_uses" label="最大使用次数" initialValue={10}><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          </Space>
          <Form.Item name="note" label="备注"><Input /></Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

function PlayIcon() { return <CreditCardOutlined /> }
