import { useCallback, useEffect, useState } from 'react'
import {
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
  Tag,
  Typography,
} from 'antd'
import {
  DeleteOutlined,
  FileTextOutlined,
  LoadingOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { getPlatformColor, getPlatformLabel } from '@/lib/platforms'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import type { TaskKind } from '@/components/TaskLogPanel'
import { usePollingRequest } from '@/hooks/usePollingRequest'

const { Text, Title } = Typography

interface TaskSnapshot {
  id: string
  platform: string
  source: string
  kind?: string
  meta?: { kind?: string }
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
  control: { stop_requested: boolean }
}

interface TaskPage {
  items: TaskSnapshot[]
  total: number
}

const TASK_PAGE_SIZE = 50

function isActive(task: TaskSnapshot) {
  return task.status === 'running' || task.status === 'pending'
}

const SOURCE_LABELS: Record<string, string> = {
  manual: '手动',
  api: 'API',
  schedule: '调度',
  backfill_rt: '补 RT',
  bind_2fa: '绑 2FA',
  payment: '支付',
  inspection: '巡检',
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

const STATUS_CONFIG: Record<string, { color: 'default' | 'processing' | 'success' | 'error' | 'warning'; label: string; icon?: React.ReactNode }> = {
  pending: { color: 'default', label: '等待中', icon: <LoadingOutlined /> },
  running: { color: 'processing', label: '运行中', icon: <LoadingOutlined /> },
  done: { color: 'success', label: '已完成' },
  failed: { color: 'error', label: '失败' },
  stopped: { color: 'warning', label: '已停止' },
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
    if (Number.isFinite(parsed)) return parsed / 1000
    return null
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  // 兼容毫秒时间戳
  return value > 1_000_000_000_000 ? value / 1000 : value
}

function formatDuration(startTs: unknown, endTs?: unknown): string {
  const start = toUnixSeconds(startTs)
  const end = toUnixSeconds(endTs) ?? (Date.now() / 1000)
  if (start === null || !Number.isFinite(end)) return '-'
  const seconds = Math.max(0, Math.floor(end - start))
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

export default function RunningTasks() {
  const [activeTasks, setActiveTasks] = useState<TaskSnapshot[]>([])
  const [finishedTasks, setFinishedTasks] = useState<TaskSnapshot[]>([])
  const [activePage, setActivePage] = useState(1)
  const [activeTotal, setActiveTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [finishedTotal, setFinishedTotal] = useState(0)
  const [loadError, setLoadError] = useState('')
  const [logTaskId, setLogTaskId] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now() / 1000)

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
      setActiveTasks(active.items || [])
      setActiveTotal(active.total || 0)
      setFinishedTasks(finished.items || [])
      setFinishedTotal(finished.total || 0)
      setLoadError('')
      setActivePage(current => Math.min(current, Math.max(1, Math.ceil(active.total / TASK_PAGE_SIZE))))
      setPage(current => Math.min(current, Math.max(1, Math.ceil(finished.total / TASK_PAGE_SIZE))))
    },
    onError: error => setLoadError(error instanceof Error ? error.message : '加载任务失败'),
  })

  useEffect(() => {
    if (!activeTotal) return
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
  }, [activeTotal])

  const logTask = logTaskId ? [...activeTasks, ...finishedTasks].find(task => task.id === logTaskId) : null
  const logKind = logTask?.kind || logTask?.meta?.kind || ''

  const handleDelete = async (taskId: string) => {
    const signal = getSignal()
    if (!signal || signal.aborted) return
    try {
      await apiFetch(`/tasks/${taskId}`, { method: 'DELETE', signal })
      if (signal.aborted) return
      if (logTaskId === taskId) setLogTaskId(null)
      setFinishedTasks(previous => previous.filter(task => task.id !== taskId))
      message.success('任务已删除')
      // The page refresh deduplicates against a poll already in flight.
      await load()
    } catch (error) {
      if (!signal.aborted) message.error(error instanceof Error ? error.message : '删除失败')
    }
  }

  const renderTask = (task: TaskSnapshot) => {
    const kind = task.kind || task.meta?.kind || ''
    const cfg = STATUS_CONFIG[task.status] || { color: 'default', label: task.status }
    const failed = task.error_count ?? task.errors?.length ?? 0
    const totalRaw = Number(task.total)
    const doneRaw = Number(task.registered)
    const success = Number.isFinite(Number(task.success)) ? Number(task.success) : 0
    const skipped = Number.isFinite(Number(task.skipped)) ? Number(task.skipped) : 0
    const total = Number.isFinite(totalRaw) && totalRaw > 0 ? Math.floor(totalRaw) : 0
    const done = Number.isFinite(doneRaw) && doneRaw > 0 ? Math.floor(doneRaw) : 0
    const pct = total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0

    const duration = isActive(task)
      ? formatDuration(task.created_at, now)
      : formatDuration(task.created_at, task.updated_at)

    return (
      <Card
        key={task.id}
        size="small"
        style={{ marginBottom: 12 }}
        bodyStyle={{ padding: '12px 16px' }}
      >
        <Row gutter={[12, 8]} align="middle" wrap>
          {/* Task ID + platform */}
          <Col flex="220px">
            <Space direction="vertical" size={2}>
              <Text code style={{ fontSize: 11 }}>
                {task.id}
              </Text>
              <Space size={4}>
                <Tag color={getPlatformColor(task.platform)} style={{ margin: 0 }}>
                  {getPlatformLabel(task.platform)}
                </Tag>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {SOURCE_LABELS[kind] || SOURCE_LABELS[task.source] || kind || task.source || '-'}
                </Text>
              </Space>
            </Space>
          </Col>

          {/* Status */}
          <Col flex="90px">
            <Badge status={cfg.color} text={cfg.label} />
          </Col>

          {/* Duration */}
          <Col flex="70px">
            <Text type="secondary" style={{ fontSize: 12 }}>
              ⏱ {duration}
            </Text>
          </Col>

          {/* Progress bar */}
          <Col flex="1" style={{ minWidth: 160 }}>
            <Space direction="vertical" size={2} style={{ width: '100%' }}>
                <Progress
                  percent={pct}
                  size="small"
                status={
                  task.status === 'failed'
                    ? 'exception'
                    : task.status === 'done'
                      ? 'success'
                      : task.status === 'stopped'
                        ? 'exception'
                        : 'active'
                }
                format={() => `${done}/${total}`}
              />
              <Space size={8}>
                <Text style={{ fontSize: 11, color: 'var(--success)' }}>
                  ✓ 成功 {success}
                </Text>
                {failed > 0 && (
                  <Text style={{ fontSize: 11, color: 'var(--danger)' }}>
                    ✗ 失败 {failed}
                  </Text>
                )}
                {skipped > 0 && (
                  <Text style={{ fontSize: 11, color: 'var(--warning)' }}>
                    → 跳过 {skipped}
                  </Text>
                )}
              </Space>
            </Space>
          </Col>

          {/* Log button */}
          <Col>
            <Space>
              <Button
                size="small"
                icon={<FileTextOutlined />}
                onClick={() => setLogTaskId(task.id)}
              >
                查看日志
              </Button>
              {!isActive(task) && (
                <Popconfirm
                  title="确认删除该任务记录？"
                  okText="删除"
                  cancelText="取消"
                  okButtonProps={{ danger: true }}
                  onConfirm={() => handleDelete(task.id)}
                >
                  <Button size="small" danger icon={<DeleteOutlined />}>
                    删除
                  </Button>
                </Popconfirm>
              )}
            </Space>
          </Col>
        </Row>
      </Card>
    )
  }

  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 16,
        }}
      >
        <Title level={4} style={{ margin: 0 }}>
          任务运行
        </Title>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
          刷新
        </Button>
      </div>

      {loadError && <Text type="danger" style={{ display: 'block', marginBottom: 12 }}>{loadError}</Text>}

      {/* Active tasks */}
      {activeTasks.length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <Text
            strong
            style={{ display: 'block', marginBottom: 8, fontSize: 13, color: 'var(--accent)' }}
          >
            进行中 ({activeTotal})
          </Text>
          {activeTasks.map(renderTask)}
          {activeTotal > TASK_PAGE_SIZE && <Pagination
            current={activePage} total={activeTotal} pageSize={TASK_PAGE_SIZE} showSizeChanger={false}
            onChange={setActivePage} disabled={loading} size="small"
          />}
        </div>
      )}

      {/* Finished tasks */}
      {finishedTasks.length > 0 && (
        <div>
          <Text
            strong
            style={{ display: 'block', marginBottom: 8, fontSize: 13, color: 'var(--text-muted)' }}
          >
            已结束 ({finishedTotal}) · 每页 {TASK_PAGE_SIZE} 条摘要，翻页查看历史
          </Text>
          {finishedTasks.map(renderTask)}
        </div>
      )}
      {finishedTotal > TASK_PAGE_SIZE && <Pagination
        current={page} total={finishedTotal} pageSize={TASK_PAGE_SIZE} showSizeChanger={false}
        onChange={setPage} disabled={loading} showTotal={total => `共 ${total} 条已结束任务`}
        style={{ marginTop: 16, textAlign: 'right' }}
      />}

      {activeTasks.length === 0 && finishedTasks.length === 0 && !loading && !loadError && (
        <Empty description="暂无任务记录" style={{ marginTop: 60 }} />
      )}

      {/* Log drawer */}
      <Drawer
        title={
          <Space>
            <FileTextOutlined />
            <span>任务日志</span>
            {logTaskId && (
              <Text code style={{ fontSize: 11 }}>
                {logTaskId}
              </Text>
            )}
          </Space>
        }
        open={!!logTaskId}
        onClose={() => setLogTaskId(null)}
        width={720}
        destroyOnClose
        bodyStyle={{ padding: 16 }}
      >
        {logTaskId && <TaskLogPanel taskId={logTaskId} kind={
          LOG_KINDS[logKind] || LOG_KINDS[logTask?.source || ''] || 'register'
        } />}
      </Drawer>
    </div>
  )
}
