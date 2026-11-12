import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Alert, Badge, Button, Card, Col, Collapse, Empty, Progress, Row, Space, Tag, Tooltip, Typography } from 'antd'
import { ArrowRightOutlined, ReloadOutlined } from '@ant-design/icons'
import { AdminPageHeader, MetricCard } from '@/components/admin/AdminPage'
import { usePollingRequest } from '@/hooks/usePollingRequest'
import { getPlatformColor, getPlatformLabel } from '@/lib/platforms'
import { apiFetch } from '@/lib/utils'

const { Text } = Typography

type Counter = number | null | undefined

interface AccountStats {
  total: number | null
  by_platform: Record<string, number | null> | null
  by_status: Record<string, number | null> | null
}

interface TaskSummary {
  id: string
  platform: string
  source: string
  kind?: string
  status: string
  total?: number | null
  registered?: number | null
  success?: number | null
  error_count?: number | null
  meta?: { kind?: string; attempt_limit_unlimited?: boolean; settings?: { max_attempts_per_run?: number } }
}

interface ActiveTaskPage {
  items: TaskSummary[]
  total: number | null
}

interface MaintenanceStatus {
  enabled?: boolean | null
  active: TaskSummary | null
  tasks?: TaskSummary[]
}

interface WebPoolStatus extends MaintenanceStatus {
  snapshot: {
    available_accounts?: number | null
    checked_at: number | string | null
  } | null
  pending_uploads?: number | null
  realtime?: { status?: string; secret_configured?: boolean }
}

interface SmsStockStatus extends MaintenanceStatus {
  inventory: {
    verified_accounts?: number | null
    missing_accounts?: number | null
  } | null
}

interface InspectionStatus extends MaintenanceStatus {
  last_task: TaskSummary | null
}

interface Overview {
  accounts: AccountStats | null
  tasks: ActiveTaskPage | null
  webPool: WebPoolStatus | null
  smsStock: SmsStockStatus | null
  inspection: InspectionStatus | null
}

const EMPTY_OVERVIEW: Overview = {
  accounts: null, tasks: null, webPool: null, smsStock: null, inspection: null,
}

const STATUS_LABELS: Record<string, string> = {
  registered: '已注册', trial: '试用中', subscribed: '已订阅',
  expired: '已过期', invalid: '无效', deleted: '已删除', banned: '已封禁',
}

const STATUS_COLORS: Record<string, string> = {
  registered: 'default', trial: 'processing', subscribed: 'success',
  expired: 'warning', invalid: 'error', deleted: 'default', banned: 'error',
}

const TASK_SOURCES: Record<string, string> = {
  manual: '手动注册', api: 'API 注册', schedule: '定时注册',
  backfill_rt: '补 RT', bind_2fa: '绑定 2FA', payment: '支付',
  inspection: 'Codex／RT 巡检', sms_stock: '接码自动补库',
  sms_stock_register: '补库注册子任务', chatgpt_web_pool: 'Web 池巡检',
  chatgpt_web_pool_register: 'Web 池邮箱注册子任务',
}

function validCount(value: Counter): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function countText(value: Counter): string {
  const count = validCount(value)
  return count === null ? '未知' : count.toLocaleString('zh-CN')
}

function statusCount(stats: AccountStats | null, status: string): number | null {
  if (!stats?.by_status) return null
  // The stats endpoint only emits statuses present in the database; an absent key is a real zero.
  return Object.hasOwn(stats.by_status, status) ? validCount(stats.by_status[status]) : 0
}

function snapshotTime(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '时间未知'
  const numeric = typeof value === 'string' ? Number(value) : value
  const date = Number.isFinite(numeric)
    ? new Date(numeric > 1_000_000_000_000 ? numeric : numeric * 1000)
    : new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false })
}

function taskSource(task: TaskSummary): string {
  const kind = task.kind || task.meta?.kind || ''
  return TASK_SOURCES[kind] || TASK_SOURCES[task.source] || kind || task.source || '后台任务'
}

function maintenanceBadge(status: MaintenanceStatus | null) {
  if (!status) return <Badge status="default" text="状态未知" />
  if (status.active) return <Badge status="processing" text={status.active.status === 'pending' ? '等待中' : '运行中'} />
  if (typeof status.enabled !== 'boolean') return <Badge status="default" text="开关未知" />
  return <Badge status="default" text={status.enabled ? '自动维护已开启' : '自动维护已关闭'} />
}

function taskHasErrors(task: TaskSummary | null | undefined): boolean {
  return task?.status === 'failed' || (validCount(task?.error_count) ?? 0) > 0
}

async function readStatus<T>(path: string, signal: AbortSignal): Promise<T | null> {
  try {
    return await apiFetch(path, { signal }) as T
  } catch {
    // Each local read can fail independently; never let a maintenance endpoint hide account stats.
    return null
  }
}

export default function Dashboard() {
  const navigate = useNavigate()
  const [overview, setOverview] = useState<Overview>(EMPTY_OVERVIEW)
  const [settled, setSettled] = useState(false)
  const requestOverview = useCallback(async (signal: AbortSignal): Promise<Overview> => {
    const [accounts, tasks, webPool, smsStock, inspection] = await Promise.all([
      readStatus<AccountStats>('/accounts/stats', signal),
      readStatus<ActiveTaskPage>('/tasks/page?status=active&limit=5', signal),
      readStatus<WebPoolStatus>('/chatgpt-web-pool/status', signal),
      readStatus<SmsStockStatus>('/sms-stock/status', signal),
      readStatus<InspectionStatus>('/inspection/status', signal),
    ])
    return { accounts, tasks, webPool, smsStock, inspection }
  }, [])
  const { loading, refresh } = usePollingRequest({
    request: requestOverview,
    intervalMs: 30_000,
    onSuccess: result => {
      setOverview(result)
      setSettled(true)
    },
    onError: () => {
      setOverview(EMPTY_OVERVIEW)
      setSettled(true)
    },
  })

  const { accounts, tasks, webPool, smsStock, inspection } = overview
  const expiredCounts = ['expired', 'invalid', 'deleted', 'banned'].map(status => statusCount(accounts, status))
  const expired = expiredCounts.every((count): count is number => count !== null)
    ? expiredCounts.reduce((sum, count) => sum + count, 0)
    : null
  const metricValue = (value: Counter) => !settled && loading ? '—' : countText(value)
  const webLastTask = webPool?.tasks?.[0]
  const smsLastTask = smsStock?.tasks?.[0]
  const webNotice = !webPool && settled
    ? '状态读取失败，可进入维护页重试。'
    : webPool?.realtime?.status === 'error'
      ? '实时通知异常，请检查维护页。'
      : webPool?.enabled && webPool.realtime?.secret_configured === false
        ? '实时触发尚未配置。'
        : taskHasErrors(webLastTask) ? '最近任务有失败项，可在任务页查看日志。' : null
  const stockMissing = validCount(smsStock?.inventory?.missing_accounts)
  const smsNotice = !smsStock && settled
    ? '状态读取失败，可进入维护页重试。'
    : stockMissing !== null && stockMissing > 0
      ? `本地库存距目标还差 ${countText(stockMissing)} 个。`
      : taskHasErrors(smsLastTask) ? '最近任务有失败项，可在任务页查看日志。' : null
  const inspectionNotice = !inspection && settled
    ? '状态读取失败，可进入维护页重试。'
    : taskHasErrors(inspection?.last_task) ? '最近巡检有失败项，可在任务页查看日志。' : null

  return (
    <div className="admin-page">
      <AdminPageHeader
        title="运营总览"
        kicker="OVERVIEW"
        description="账号、后台任务与维护状态"
        extra={<Button icon={<ReloadOutlined />} loading={loading} onClick={() => void refresh()}>刷新</Button>}
      />

      {settled && !accounts ? <Alert type="warning" showIcon message="账号统计暂不可用" description="未将未知数据计为 0，请稍后刷新。" /> : null}

      <div className="admin-metrics">
        <MetricCard label="总账号数" value={metricValue(accounts?.total)} hint="本地账号记录" tone="accent" />
        <MetricCard label="试用中" value={metricValue(statusCount(accounts, 'trial'))} hint="状态为 trial" />
        <MetricCard label="已订阅" value={metricValue(statusCount(accounts, 'subscribed'))} hint="状态为 subscribed" tone="success" />
        <MetricCard label="已失效" value={metricValue(expired)} hint="过期 · 无效 · 删除 · 封禁" tone={expired !== null && expired > 0 ? 'danger' : 'default'} />
      </div>

      <section>
        <div className="admin-section-header" style={{ marginBottom: 12 }}><h2 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>维护中心</h2></div>
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={8}>
            <Card className="admin-panel" title="Web 池巡检" extra={<Button type="text" aria-label="进入 Web 池巡检" icon={<ArrowRightOutlined />} onClick={() => navigate('/chatgpt-web-pool')} />} style={{ height: '100%' }}>
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                {maintenanceBadge(webPool)}
                <div className="admin-inline-meta">
                  <span>待上传 <Text strong>{countText(webPool?.pending_uploads)}</Text></span>
                  <span>快照可用账号 <Text strong>{countText(webPool?.snapshot?.available_accounts)}</Text></span>
                </div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {webPool?.snapshot ? `上次快照 ${snapshotTime(webPool.snapshot.checked_at)}，非实时额度` : '暂无远端快照，当前额度未知'}
                </Text>
                {webNotice ? <div className="admin-note">{webNotice}</div> : null}
              </Space>
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card className="admin-panel" title="接码自动补库" extra={<Button type="text" aria-label="进入接码自动补库" icon={<ArrowRightOutlined />} onClick={() => navigate('/sms-stock')} />} style={{ height: '100%' }}>
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                {maintenanceBadge(smsStock)}
                <div className="admin-inline-meta">
                  <span>本地 RT 就绪库存 <Text strong>{countText(smsStock?.inventory?.verified_accounts)}</Text></span>
                </div>
                {smsNotice ? <div className="admin-note">{smsNotice}</div> : null}
              </Space>
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card className="admin-panel" title="Codex／RT 巡检" extra={<Button type="text" aria-label="进入 Codex／RT 巡检" icon={<ArrowRightOutlined />} onClick={() => navigate('/inspection')} />} style={{ height: '100%' }}>
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                {maintenanceBadge(inspection)}
                <div className="admin-inline-meta">
                  <span>最近巡检 {inspection?.last_task ? <Text strong>{taskHasErrors(inspection.last_task) ? '有失败项' : inspection.last_task.status === 'done' ? '已结束' : inspection.last_task.status === 'stopped' ? '已停止' : '进行中'}</Text> : inspection ? '暂无记录' : '未知'}</span>
                </div>
                {inspectionNotice ? <div className="admin-note">{inspectionNotice}</div> : null}
              </Space>
            </Card>
          </Col>
        </Row>
      </section>

      <Card
        className="admin-panel"
        title={<Space>当前任务 <Tag>{countText(tasks?.total)}</Tag></Space>}
        extra={<Button type="link" onClick={() => navigate('/running-tasks')} icon={<ArrowRightOutlined />} iconPosition="end">全部任务</Button>}
        loading={!settled && loading}
      >
        {!tasks ? <Text type="secondary">任务状态未知，请稍后刷新。</Text> : tasks.items.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有活动任务" />
        ) : (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            {tasks.items.slice(0, 5).map(task => {
              const done = validCount(task.registered)
              const total = validCount(task.total)
              const percent = done !== null && total !== null && total > 0 ? Math.min(100, Math.round(done / total * 100)) : null
              return <div key={task.id}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
                  <Space wrap>
                    <Text strong>{taskSource(task)}</Text>
                    <Tag color={getPlatformColor(task.platform)}>{getPlatformLabel(task.platform)}</Tag>
                    <Badge status={task.status === 'running' ? 'processing' : 'default'} text={task.status === 'running' ? '运行中' : '等待中'} />
                    <Tooltip title={task.id}><Text type="secondary" style={{ fontSize: 12 }}>#{task.id.slice(-8)}</Text></Tooltip>
                  </Space>
                  <Text type="secondary">已处理 {countText(done)} / {task.meta?.attempt_limit_unlimited || (task.source === 'chatgpt_web_pool' && task.meta?.settings?.max_attempts_per_run === 0) ? '不限' : countText(total)}</Text>
                </div>
                {percent !== null ? <Progress percent={percent} showInfo={false} size="small" status="active" style={{ margin: '6px 0 0' }} /> : null}
              </div>
            })}
          </Space>
        )}
      </Card>

      <Collapse items={[{
        key: 'distribution',
        label: '账号分布',
        children: !accounts ? <Text type="secondary">账号统计未知</Text> : (
          <Row gutter={[32, 20]}>
            <Col xs={24} md={12}>
              <Text strong>平台</Text>
              {accounts.by_platform === null ? <div className="admin-note">平台分布未知</div> : Object.entries(accounts.by_platform).length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无账号" /> : Object.entries(accounts.by_platform).map(([platform, count]) => (
                <div key={platform} style={{ marginTop: 14 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                    <Tag color={getPlatformColor(platform)}>{getPlatformLabel(platform)}</Tag><Text>{countText(count)}</Text>
                  </div>
                  {validCount(count) !== null && count !== null && validCount(accounts.total) !== null && accounts.total !== null && accounts.total > 0 ? <Progress percent={Math.round(count / accounts.total * 100)} showInfo={false} size="small" /> : null}
                </div>
              ))}
            </Col>
            <Col xs={24} md={12}>
              <Text strong>状态</Text>
              {accounts.by_status === null ? <div className="admin-note">状态分布未知</div> : Object.entries(accounts.by_status).length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无账号" /> : Object.entries(accounts.by_status).map(([status, count]) => (
                <div key={status} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, paddingTop: 14 }}>
                  <Tag color={STATUS_COLORS[status] || 'default'}>{STATUS_LABELS[status] || status}</Tag><Text>{countText(count)}</Text>
                </div>
              ))}
            </Col>
          </Row>
        ),
      }]} />
    </div>
  )
}
