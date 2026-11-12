import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Drawer,
  Empty,
  message,
  Popconfirm,
  Pagination,
  Progress,
  Row,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { TableColumnsType } from 'antd'
import { DeleteOutlined, FileTextOutlined, ReloadOutlined } from '@ant-design/icons'
import { AdminPageHeader } from '@/components/admin/AdminPage'
import { getPlatformColor, getPlatformLabel } from '@/lib/platforms'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import type { TaskKind } from '@/components/TaskLogPanel'
import { usePollingRequest } from '@/hooks/usePollingRequest'

const { Text } = Typography

type Counter = number | null | undefined

interface TaskSnapshot {
  id: string
  platform: string
  source: string
  kind?: string
  meta?: { kind?: string; attempt_limit_unlimited?: boolean; outcome?: string; settings?: { max_attempts_per_run?: number } }
  status: 'pending' | 'running' | 'done' | 'failed' | 'stopped'
  total?: number | null
  registered?: number | null
  success?: number | null
  skipped?: number | null
  error_count?: number | null
  created_at: number | string | null
  updated_at: number | string | null
  control?: { stop_requested?: boolean }
}

interface TaskPage {
  items: TaskSnapshot[]
  total: number | null
}

interface LogSelection {
  id: string
  kind: TaskKind
  label: string
}

const TASK_PAGE_SIZE = 50

const SOURCE_LABELS: Record<string, string> = {
  manual: '手动注册',
  api: 'API 注册',
  schedule: '定时注册',
  backfill_rt: '补 RT',
  bind_2fa: '绑定 2FA',
  payment: '支付',
  inspection: 'Codex／RT 巡检',
  sms_stock: '接码自动补库',
  sms_stock_register: '补库注册子任务',
  chatgpt_web_pool: 'Web 池巡检',
  chatgpt_web_pool_register: 'Web 池邮箱注册子任务',
}

const LOG_KINDS: Record<string, TaskKind> = {
  backfill_rt: 'backfill_rt',
  bind_2fa: 'bind_2fa',
  payment: 'payment',
  inspection: 'inspection',
  sms_stock: 'sms_stock',
  sms_stock_register: 'register',
  chatgpt_web_pool: 'chatgpt_web_pool',
  chatgpt_web_pool_register: 'chatgpt_web_pool_register',
}

const STATUS_CONFIG: Record<string, { color: 'default' | 'processing' | 'error' | 'warning'; label: string }> = {
  pending: { color: 'default', label: '等待中' },
  running: { color: 'processing', label: '运行中' },
  // done is a terminal state, not a statement that every item succeeded.
  done: { color: 'default', label: '已结束' },
  failed: { color: 'error', label: '失败' },
  stopped: { color: 'warning', label: '已停止' },
}

function validCount(value: Counter): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function countText(value: Counter): string {
  const count = validCount(value)
  return count === null ? '未知' : count.toLocaleString('zh-CN')
}

function taskSource(task: TaskSnapshot): string {
  const kind = task.kind || task.meta?.kind || ''
  return SOURCE_LABELS[kind] || SOURCE_LABELS[task.source] || kind || task.source || '后台任务'
}

function taskTotalText(task: TaskSnapshot) {
  return task.meta?.attempt_limit_unlimited || (task.source === 'chatgpt_web_pool' && task.meta?.settings?.max_attempts_per_run === 0)
    ? '不限' : countText(task.total)
}

function taskStatus(task: TaskSnapshot) {
  if (task.meta?.outcome === 'quota_unknown') return <Badge status="warning" text="额度未知，未继续" />
  const cfg = STATUS_CONFIG[task.status] || { color: 'default' as const, label: task.status }
  return <Badge status={cfg.color} text={cfg.label} />
}

function toUnixSeconds(value: unknown): number | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed) return null
    const maybeNum = Number(trimmed)
    if (Number.isFinite(maybeNum)) {
      return maybeNum > 1_000_000_000_000 ? maybeNum / 1000 : maybeNum
    }
    const parsed = Date.parse(trimmed)
    return Number.isFinite(parsed) ? parsed / 1000 : null
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  return value > 1_000_000_000_000 ? value / 1000 : value
}

function formatDuration(startTs: unknown, endTs: unknown): string {
  const start = toUnixSeconds(startTs)
  const end = toUnixSeconds(endTs)
  if (start === null || end === null) return '未知'
  const seconds = Math.max(0, Math.floor(end - start))
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}

function formatTime(value: unknown): string {
  const seconds = toUnixSeconds(value)
  return seconds === null ? '未知' : new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false })
}

function taskPercent(task: TaskSnapshot): number | null {
  const total = validCount(task.total)
  const processed = validCount(task.registered)
  return total !== null && total > 0 && processed !== null
    ? Math.max(0, Math.min(100, Math.round(processed / total * 100)))
    : null
}

function TaskResult({ task }: { task: TaskSnapshot }) {
  const failures = validCount(task.error_count)
  return <div className="admin-inline-meta" style={{ fontSize: 12 }}>
    <span style={{ color: validCount(task.success) !== null && (task.success ?? 0) > 0 ? 'var(--success)' : 'var(--text-muted)' }}>成功 {countText(task.success)}</span>
    <span style={{ color: failures !== null && failures > 0 ? 'var(--danger)' : 'var(--text-muted)' }}>失败 {countText(failures)}</span>
    <span style={{ color: 'var(--text-muted)' }}>跳过 {countText(task.skipped)}</span>
  </div>
}

function ActiveTaskList({ tasks, onOpenLog }: { tasks: TaskSnapshot[]; onOpenLog: (task: TaskSnapshot) => void }) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  const hasTasks = tasks.length > 0

  useEffect(() => {
    if (!hasTasks) return
    let tick: number | undefined
    const updateTick = () => {
      window.clearInterval(tick)
      if (document.hidden) return
      setNow(Date.now() / 1000)
      tick = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    }
    updateTick()
    document.addEventListener('visibilitychange', updateTick)
    return () => {
      window.clearInterval(tick)
      document.removeEventListener('visibilitychange', updateTick)
    }
  }, [hasTasks])

  return <Row gutter={[16, 16]}>
    {tasks.map(task => {
      const percent = taskPercent(task)
      return <Col xs={24} xl={12} key={task.id}>
        <Card className="admin-panel" style={{ height: '100%' }}>
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, flexWrap: 'wrap' }}>
              <Space direction="vertical" size={8}>
                <Text strong style={{ fontSize: 16 }}>{taskSource(task)}</Text>
                <Space wrap size={8}>
                  <Tag color={getPlatformColor(task.platform)} style={{ margin: 0 }}>{getPlatformLabel(task.platform)}</Tag>
                  {taskStatus(task)}
                  {task.control?.stop_requested ? <Tag color="warning" style={{ margin: 0 }}>停止中</Tag> : null}
                </Space>
              </Space>
              <Button icon={<FileTextOutlined />} onClick={() => onOpenLog(task)}>查看日志</Button>
            </div>
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                <Text type="secondary">已处理</Text>
                <Text strong>{countText(task.registered)} / {taskTotalText(task)}</Text>
              </div>
              {percent !== null ? <Progress percent={percent} showInfo={false} size="small" status={task.control?.stop_requested ? 'normal' : 'active'} style={{ margin: '6px 0 0' }} /> : <Text type="secondary" style={{ fontSize: 12 }}>暂无可计算进度</Text>}
            </div>
            <TaskResult task={task} />
            <div className="admin-inline-meta" style={{ justifyContent: 'space-between', fontSize: 12 }}>
              <span>耗时 {formatDuration(task.created_at, now)}</span>
              <Tooltip title={task.id}><Text type="secondary" style={{ fontSize: 12 }}>#{task.id.slice(-8)}</Text></Tooltip>
            </div>
          </Space>
        </Card>
      </Col>
    })}
  </Row>
}

export default function RunningTasks() {
  const [activeTasks, setActiveTasks] = useState<TaskSnapshot[]>([])
  const [finishedTasks, setFinishedTasks] = useState<TaskSnapshot[]>([])
  const [activePage, setActivePage] = useState(1)
  const [activeTotal, setActiveTotal] = useState<number | null>(null)
  const [page, setPage] = useState(1)
  const [finishedTotal, setFinishedTotal] = useState<number | null>(null)
  const [loadError, setLoadError] = useState(false)
  const [hasLoaded, setHasLoaded] = useState(false)
  const [logTask, setLogTask] = useState<LogSelection | null>(null)

  const requestTasks = useCallback(async (signal: AbortSignal) => {
    // Active work has its own query, so old completed records never push it off the page.
    const [active, finished] = await Promise.all([
      apiFetch(`/tasks/page?status=active&limit=${TASK_PAGE_SIZE}&offset=${(activePage - 1) * TASK_PAGE_SIZE}`, { signal }) as Promise<TaskPage>,
      apiFetch(`/tasks/page?status=finished&limit=${TASK_PAGE_SIZE}&offset=${(page - 1) * TASK_PAGE_SIZE}`, { signal }) as Promise<TaskPage>,
    ])
    return { active, finished }
  }, [activePage, page])
  const { loading, refresh: load, getSignal } = usePollingRequest({
    request: requestTasks,
    intervalMs: 2500,
    onSuccess: ({ active, finished }) => {
      const nextActiveTotal = validCount(active.total)
      const nextFinishedTotal = validCount(finished.total)
      setActiveTasks(active.items || [])
      setActiveTotal(nextActiveTotal)
      setFinishedTasks(finished.items || [])
      setFinishedTotal(nextFinishedTotal)
      setLoadError(false)
      setHasLoaded(true)
      if (nextActiveTotal !== null) setActivePage(current => Math.min(current, Math.max(1, Math.ceil(nextActiveTotal / TASK_PAGE_SIZE))))
      if (nextFinishedTotal !== null) setPage(current => Math.min(current, Math.max(1, Math.ceil(nextFinishedTotal / TASK_PAGE_SIZE))))
    },
    onError: () => setLoadError(true),
  })

  const openLog = useCallback((task: TaskSnapshot) => {
    const kind = task.kind || task.meta?.kind || ''
    // Keep the source kind even if polling or pagination moves this row out of view.
    setLogTask({ id: task.id, label: taskSource(task), kind: LOG_KINDS[kind] || LOG_KINDS[task.source] || 'register' })
  }, [])

  const handleDelete = useCallback(async (taskId: string) => {
    const signal = getSignal()
    if (!signal || signal.aborted) return
    try {
      await apiFetch(`/tasks/${taskId}`, { method: 'DELETE', signal })
      if (signal.aborted) return
      setLogTask(current => current?.id === taskId ? null : current)
      setFinishedTasks(previous => previous.filter(task => task.id !== taskId))
      message.success('任务已删除')
      // The page refresh deduplicates against a poll already in flight.
      await load()
    } catch {
      if (!signal.aborted) message.error('删除失败，请稍后重试')
    }
  }, [getSignal, load])

  const historyColumns = useMemo<TableColumnsType<TaskSnapshot>>(() => [
    {
      title: '任务', key: 'task', width: 250,
      render: (_, task) => <Space direction="vertical" size={4}>
        <Text strong>{taskSource(task)}</Text>
        <Space size={6} wrap>
          <Tag color={getPlatformColor(task.platform)} style={{ margin: 0 }}>{getPlatformLabel(task.platform)}</Tag>
          <Tooltip title={task.id}><Text type="secondary" style={{ fontSize: 11 }}>#{task.id.slice(-8)}</Text></Tooltip>
        </Space>
      </Space>,
    },
    { title: '状态', key: 'status', width: 100, render: (_, task) => taskStatus(task) },
    { title: '已处理 / 总量', key: 'progress', width: 140, render: (_, task) => <Text>{countText(task.registered)} / {taskTotalText(task)}</Text> },
    { title: '结果', key: 'result', width: 240, render: (_, task) => <TaskResult task={task} /> },
    { title: '耗时', key: 'duration', width: 110, render: (_, task) => <Text type="secondary">{formatDuration(task.created_at, task.updated_at)}</Text> },
    { title: '开始时间', key: 'created_at', width: 175, render: (_, task) => <Text type="secondary" style={{ fontSize: 12 }}>{formatTime(task.created_at)}</Text> },
    {
      title: '操作', key: 'actions', width: 180,
      render: (_, task) => <Space size={4}>
        <Button size="small" type="text" icon={<FileTextOutlined />} onClick={() => openLog(task)}>日志</Button>
        <Popconfirm
          title="确认删除该任务记录？"
          okText="删除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={() => handleDelete(task.id)}
        >
          <Button size="small" type="text" danger icon={<DeleteOutlined />}>删除</Button>
        </Popconfirm>
      </Space>,
    },
  ], [handleDelete, openLog])

  return (
    <div className="admin-page">
      <AdminPageHeader
        title="任务运行"
        kicker="TASKS"
        description="跟踪活动任务，按页查阅执行记录"
        extra={<Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>刷新</Button>}
      />

      {loadError ? <Alert type="warning" showIcon message="任务状态暂未更新" description={hasLoaded ? '当前显示上次读取结果，请稍后刷新。' : '状态未知，请稍后刷新。'} /> : null}

      <section>
        <div className="admin-section-header" style={{ marginBottom: 12 }}>
          <Space><h2 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>活动任务</h2><Tag color={activeTotal !== null && activeTotal > 0 ? 'processing' : 'default'}>{countText(activeTotal)}</Tag></Space>
        </div>
        {activeTasks.length > 0 ? <ActiveTaskList tasks={activeTasks} onOpenLog={openLog} /> : <Card className="admin-panel" loading={!hasLoaded && loading}>
          {hasLoaded ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有活动任务" /> : <Text type="secondary">活动任务状态未知</Text>}
        </Card>}
        {activeTotal !== null && activeTotal > TASK_PAGE_SIZE ? <Pagination
          current={activePage} total={activeTotal} pageSize={TASK_PAGE_SIZE} showSizeChanger={false}
          onChange={setActivePage} disabled={loading} size="small" style={{ marginTop: 16 }}
          showTotal={total => `共 ${total} 条活动任务`}
        /> : null}
      </section>

      <Card className="admin-panel" title={<Space>已结束记录 <Tag>{countText(finishedTotal)}</Tag></Space>}>
        <Table<TaskSnapshot>
          rowKey="id"
          size="small"
          dataSource={finishedTasks}
          columns={historyColumns}
          loading={!hasLoaded && loading}
          pagination={false}
          scroll={{ x: 1195 }}
          locale={{ emptyText: hasLoaded ? '暂无已结束任务' : '历史记录未知' }}
        />
        {finishedTotal !== null && finishedTotal > TASK_PAGE_SIZE ? <Pagination
          current={page} total={finishedTotal} pageSize={TASK_PAGE_SIZE} showSizeChanger={false}
          onChange={setPage} disabled={loading} showTotal={total => `共 ${total} 条已结束任务`}
          style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}
        /> : null}
      </Card>

      <Drawer
        title={logTask ? <Space wrap><FileTextOutlined /><span>{logTask.label}日志</span><Tooltip title={logTask.id}><Text type="secondary" style={{ fontSize: 12 }}>#{logTask.id.slice(-8)}</Text></Tooltip></Space> : '任务日志'}
        open={!!logTask}
        onClose={() => setLogTask(null)}
        width={720}
        destroyOnClose
        styles={{ body: { padding: 16 } }}
      >
        {logTask ? <TaskLogPanel taskId={logTask.id} kind={logTask.kind} onDone={() => void load()} /> : null}
      </Drawer>
    </div>
  )
}
