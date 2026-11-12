import { useEffect, useState, useCallback } from 'react'
import { useParams } from 'react-router-dom'
import {
  Table,
  Card,
  Button,
  Input,
  InputNumber,
  Select,
  Tag,
  Space,
  Modal,
  Form,
  message,
  Popconfirm,
  Dropdown,
  Typography,
  Alert,
  DatePicker,
  Switch,
  theme,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  ReloadOutlined,
  CopyOutlined,
  LinkOutlined,
  PlusOutlined,
  DownloadOutlined,
  UploadOutlined,
  MoreOutlined,
  DeleteOutlined,
  SyncOutlined,
  KeyOutlined,
  FilterOutlined,
} from '@ant-design/icons'
import { AccountExportModal } from '@/components/AccountExportModal'
import { ChatGPTBind2faSwitch } from '@/components/ChatGPTBind2faSwitch'
import { ChatGPTRegisterFlowSelect } from '@/components/ChatGPTRegisterFlowSelect'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { AdminPageHeader } from '@/components/admin/AdminPage'
import { useTableViewport } from '@/hooks/useTableViewport'
import type { TaskKind } from '@/components/TaskLogPanel'
import { PaymentOperationDrawer } from '@/components/payments/PaymentOperationDrawer'
import { usePersistentChatGPTBind2fa } from '@/hooks/usePersistentChatGPTBind2fa'
import { usePersistentChatGPTRegisterFlow } from '@/hooks/usePersistentChatGPTRegisterFlow'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { apiFetch } from '@/lib/utils'
import { normalizeExecutorForPlatform } from '@/lib/platforms'
import {
  DEFAULT_REGISTER_RETRY_TIMES,
  normalizeRegisterRetryTimes,
} from '@/lib/registerRetry'

const { Text } = Typography

const STATUS_COLORS: Record<string, string> = {
  registered: 'default',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'error',
  // 被 OpenAI 删除/停用的号单独一档，用更深的红紫和"已失效"区分开
  deleted: 'volcano',
}

// 纯前端动作，不发请求，所以不和后端的 action id 抢命名空间
const COPY_TOTP_ACTION_ID = '__copy_totp_secret'

const BATCH_UPLOAD_ACTIONS = [
  { id: 'upload_cpa', label: '上传 CPA' },
  { id: 'upload_sub2api', label: '上传 Sub2API' },
  { id: 'upload_tm', label: '上传 Team Manager' },
  { id: 'upload_codex_proxy', label: '上传 CodexProxy' },
] as const

type BatchUploadActionId = (typeof BATCH_UPLOAD_ACTIONS)[number]['id']

// 这些动作要跑几十秒的协议链（还可能停下来等一封验证码），同步等只能看见一个
// 转圈。改走后台任务，和注册一样用日志弹窗把每一步显示出来。
const TASK_BACKED_ACTIONS: Record<
  string,
  { endpoint: string; kind: TaskKind; body: (accountId: number) => Record<string, unknown> }
> = {
  backfill_refresh_token: {
    endpoint: '/tasks/backfill-rt',
    kind: 'backfill_rt',
    body: (accountId) => ({ account_ids: [accountId], only_missing_rt: false, delay_seconds: 0 }),
  },
  bind_2fa: {
    endpoint: '/tasks/bind-2fa',
    kind: 'bind_2fa',
    body: (accountId) => ({ account_ids: [accountId], only_missing_2fa: false, delay_seconds: 0 }),
  },
}

function parseExtraJson(raw: string | undefined) {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function normalizeAccount(account: any) {
  const extra = parseExtraJson(account.extra_json)
  const syncStatuses = extra.sync_statuses && typeof extra.sync_statuses === 'object' ? extra.sync_statuses : {}
  const cpaSync = syncStatuses.cpa && typeof syncStatuses.cpa === 'object' ? syncStatuses.cpa : {}
  const sub2apiSync = syncStatuses.sub2api && typeof syncStatuses.sub2api === 'object' ? syncStatuses.sub2api : {}
  const cliproxySync = syncStatuses.cliproxyapi && typeof syncStatuses.cliproxyapi === 'object' ? syncStatuses.cliproxyapi : {}
  const webPoolSync = syncStatuses.chatgpt2api && typeof syncStatuses.chatgpt2api === 'object' ? syncStatuses.chatgpt2api : {}
  const chatgptLocal = extra.chatgpt_local && typeof extra.chatgpt_local === 'object' ? extra.chatgpt_local : {}
  const plusCheck = extra.plus_check && typeof extra.plus_check === 'object' ? extra.plus_check : {}
  const totpSecret = String(extra.totp_secret || '')
  const smsVerification = extra.sms_verification && typeof extra.sms_verification === 'object' ? extra.sms_verification : {}
  return {
    ...account,
    extra,
    cpaSync,
    sub2apiSync,
    cliproxySync,
    chatgptLocal,
    plusCheck,
    totpSecret,
    smsVerification,
    webPoolOnly: extra.account_purpose === 'chatgpt_web_pool' || ['1', 'true'].includes(String(extra.chatgpt_web_only).toLowerCase()),
    webPoolSync,
  }

}

function formatSyncTime(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function formatCreatedAt(value?: string) {
  if (!value) return { date: '-', time: '' }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return { date: value, time: '' }
  }
  return {
    date: date.toLocaleDateString(),
    time: date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  }
}

function authStateMeta(state?: string) {
  switch (state) {
    case 'access_token_valid':
      return { color: 'success', label: 'AT有效' }
    case 'account_deactivated':
      return { color: 'error', label: '已失效' }
    case 'access_token_invalidated':
      return { color: 'error', label: 'AT失效' }
    case 'unauthorized':
      return { color: 'error', label: '未授权' }
    case 'missing_access_token':
      return { color: 'default', label: '缺少AT' }
    case 'banned_like':
      return { color: 'error', label: '疑似封禁' }
    case 'probe_failed':
      return { color: 'warning', label: '探测失败' }
    default:
      return { color: 'default', label: '未探测' }
  }
}

function codexStateMeta(state?: string) {
  switch (state) {
    case 'usable':
      return { color: 'success', label: '可用' }
    case 'account_deactivated':
      return { color: 'error', label: '已失效' }
    case 'access_token_invalidated':
      return { color: 'error', label: 'AT失效' }
    case 'unauthorized':
      return { color: 'error', label: '未授权' }
    case 'payment_required':
      return { color: 'warning', label: '需付费/权限' }
    case 'quota_exhausted':
      return { color: 'warning', label: '额度耗尽' }
    case 'skipped_auth_invalid':
      return { color: 'default', label: '未测' }
    case 'probe_failed':
      return { color: 'warning', label: '探测失败' }
    default:
      return { color: 'default', label: '未探测' }
  }
}

type SmsVerification = { verified?: unknown; country?: string }

type AccountProbe = {
  auth?: {
    state?: string
    message?: string
    refresh_attempts?: { strategy?: string; ok?: boolean }[]
  }
  codex?: { state?: string; message?: string }
}

type WebPoolSync = {
  needs_resync?: boolean
  ready?: boolean
  imported?: boolean
  quota?: number | null
  checked_at?: string
}

function smsVerificationMeta(verification?: SmsVerification) {
  if (verification?.verified === true) {
    return { color: 'success', label: `已接码${verification.country ? ` (${verification.country})` : ''}` }
  }
  return { color: 'default', label: '无接码记录' }
}

function rtProbeMeta(probe: AccountProbe | undefined, hasRefreshToken: boolean) {
  // 只有 OAuth 刷新尝试能直接说明 RT 结果，AT/Codex 探测保留各自的含义。
  const attempts = probe?.auth?.refresh_attempts
  const oauthAttempt = Array.isArray(attempts) ? attempts.findLast((attempt) => attempt?.strategy === 'oauth') : undefined
  if (oauthAttempt?.ok === true) return { color: 'success', label: 'RT 刷新成功' }
  if (oauthAttempt?.ok === false) return { color: 'warning', label: 'RT 刷新失败' }

  const codexState = probe?.codex?.state
  if (codexState && !['not_checked', 'skipped_auth_invalid', 'unknown'].includes(codexState)) {
    const meta = codexStateMeta(codexState)
    return { ...meta, label: `Codex ${meta.label}` }
  }
  const authState = probe?.auth?.state
  if (authState && authState !== 'unknown') return authStateMeta(authState)
  return { color: 'default', label: hasRefreshToken ? '有 RT · 未检测' : '无 RT · 未检测' }
}

function WebPoolSyncSummary({ sync }: { sync: WebPoolSync }) {
  const needsResync = sync.needs_resync === true
  const color = needsResync ? 'warning' : sync.ready ? 'success' : sync.imported ? 'warning' : 'default'
  const label = needsResync
    ? '凭证更新待同步'
    : sync.ready ? 'Web 远端已确认可用' : sync.imported ? '已入 Web 池，未确认可用' : 'Web 账号待上传'

  return (
    <Space direction="vertical" size={2}>
      <Tag color={color} title={sync.checked_at ? `上次同步：${formatSyncTime(sync.checked_at)}` : undefined}>
        {label}
      </Tag>
      {needsResync ? <Text type="secondary" style={{ fontSize: 12 }}>本地凭证已更新，等待同步后重新确认 Web 可用性。</Text> : null}
      {sync.imported ? (
        <Text type="secondary" style={{ fontSize: 12 }}>上次记录图片额度：{sync.quota ?? '-'}（非实时）</Text>
      ) : null}
    </Space>
  )
}

const PLUS_TRIAL_FILTERS = [
  { value: 'trial_eligible', label: '可领首月免费' },
  { value: 'plus_active', label: 'Plus 生效中' },
  { value: 'free', label: 'Free' },
  { value: 'banned', label: '封号' },
  { value: 'token_invalid', label: '凭证失效' },
  { value: 'unchecked', label: '未检测' },
]

type PlusCheck = { status?: string; message?: string; checked_at?: string }

function plusTrialMeta(status?: string) {
  switch ((status || '').toLowerCase()) {
    case 'trial_eligible':
      return { color: 'success', label: '可领首月免费' }
    case 'plus_active':
      return { color: 'processing', label: 'Plus 生效中' }
    case 'free':
      return { color: 'default', label: 'Free' }
    case 'banned':
      return { color: 'error', label: '封号' }
    case 'token_invalid':
      return { color: 'warning', label: '凭证失效' }
    default:
      return { color: 'default', label: '未检测' }
  }
}

function planMeta(plan?: string) {
  switch ((plan || '').toLowerCase()) {
    case 'plus':
      return { color: 'success', label: 'Plus' }
    case 'team':
      return { color: 'processing', label: 'Team' }
    case 'enterprise':
      return { color: 'processing', label: 'Enterprise' }
    case 'pro':
      return { color: 'processing', label: 'Pro' }
    case 'free':
      return { color: 'default', label: 'Free' }
    default:
      return { color: 'default', label: '未知' }
  }
}

function formatStructuredText(value?: string) {
  if (!value) return ''
  const trimmed = String(value).trim()
  if (!trimmed) return ''
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2)
    } catch {
      return trimmed
    }
  }
  return trimmed
}

function SummaryField({
  label,
  value,
  code = false,
}: {
  label: string
  value?: string
  code?: boolean
}) {
  const { token } = theme.useToken()
  if (!value) return null

  const content = code ? formatStructuredText(value) : value
  const isBlock = code || content.length > 96 || content.includes('\n')

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '104px minmax(0, 1fr)',
        gap: 12,
        alignItems: 'start',
      }}
    >
      <Text type="secondary" style={{ fontSize: 12, lineHeight: '20px' }}>
        {label}
      </Text>
      {isBlock ? (
        <pre
          style={{
            margin: 0,
            padding: code ? '8px 10px' : 0,
            borderRadius: code ? token.borderRadius : 0,
            border: code ? `1px solid ${token.colorBorder}` : 'none',
            background: code ? token.colorBgElevated : 'transparent',
            color: code ? token.colorText : token.colorTextSecondary,
            fontFamily: code ? 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace' : 'inherit',
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            overflowWrap: 'anywhere',
            maxHeight: code ? 160 : 'none',
            overflow: code ? 'auto' : 'visible',
          }}
        >
          {content}
        </pre>
      ) : (
        <Text style={{ display: 'block', color: token.colorTextSecondary, lineHeight: '20px' }}>
          {content}
        </Text>
      )}
    </div>
  )
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  const { token } = theme.useToken()

  return (
    <div
      style={{
        marginTop: 16,
        padding: 14,
        borderRadius: token.borderRadiusLG,
        border: `1px solid ${token.colorBorder}`,
        background: token.colorFillAlter,
      }}
    >
      <div style={{ marginBottom: 10, fontWeight: 600, color: token.colorText }}>{title}</div>
      {children}
    </div>
  )
}

function LocalProbeSummary({ probe }: { probe: any }) {
  const checkedAt = probe?.checked_at || probe?.auth?.checked_at || probe?.subscription?.checked_at || probe?.codex?.checked_at
  const auth = probe?.auth || {}
  const subscription = probe?.subscription || {}
  const codex = probe?.codex || {}

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={authStateMeta(auth.state).color}>认证: {authStateMeta(auth.state).label}</Tag>
        <Tag color={planMeta(subscription.plan).color}>订阅: {planMeta(subscription.plan).label}</Tag>
        <Tag color={codexStateMeta(codex.state).color}>Codex: {codexStateMeta(codex.state).label}</Tag>
      </div>
      <SummaryField label="探测时间" value={checkedAt ? formatSyncTime(checkedAt) : ''} />
      <SummaryField label="认证信息" value={auth.message} code />
      <SummaryField label="工作区套餐" value={subscription.workspace_plan_type} />
      <SummaryField label="Codex 信息" value={codex.message} code />
    </div>
  )
}

function cliproxyStateMeta(sync: any) {
  if (!sync || Object.keys(sync).length === 0) {
    return { color: 'default', label: '未同步' }
  }
  if (sync.remote_state === 'unreachable') {
    return { color: 'error', label: '不可连接' }
  }
  if (sync.remote_state === 'not_found') {
    return { color: 'default', label: '远端未发现' }
  }
  if (!sync.uploaded) {
    return { color: 'default', label: '未发现' }
  }
  if (sync.remote_state === 'relogin_required') {
    return { color: 'warning', label: '需重新登录' }
  }
  if (sync.remote_state === 'usable') {
    return { color: 'success', label: '远端可用' }
  }
  if (sync.remote_state === 'account_deactivated') {
    return { color: 'error', label: '远端已失效' }
  }
  if (sync.remote_state === 'access_token_invalidated') {
    return { color: 'error', label: '远端AT失效' }
  }
  if (sync.remote_state === 'unauthorized') {
    return { color: 'error', label: '远端未授权' }
  }
  if (sync.remote_state === 'payment_required') {
    return { color: 'warning', label: '远端需付费/权限' }
  }
  if (sync.remote_state === 'quota_exhausted') {
    return { color: 'warning', label: '远端额度耗尽' }
  }
  if (sync.status === 'active') {
    return { color: 'processing', label: '远端Active' }
  }
  if (sync.status === 'refreshing') {
    return { color: 'processing', label: '远端刷新中' }
  }
  if (sync.status === 'pending') {
    return { color: 'default', label: '远端待处理' }
  }
  if (sync.status === 'error') {
    return { color: 'error', label: '远端错误' }
  }
  if (sync.status === 'disabled') {
    return { color: 'default', label: '远端禁用' }
  }
  return { color: 'default', label: '未同步' }
}

function uploadSyncMeta(sync: any) {
  if (!sync || Object.keys(sync).length === 0) {
    return { color: 'default', label: '未上传' }
  }
  if (sync.uploaded || sync.uploaded_at) {
    return { color: 'success', label: '已上传' }
  }
  if (sync.last_attempt_ok === false) {
    return { color: 'error', label: '失败' }
  }
  if (sync.last_attempt_ok === true || sync.last_attempt_at) {
    return { color: 'processing', label: '已尝试' }
  }
  return { color: 'default', label: '未上传' }
}

function uploadSyncTitle(name: string, sync: any) {
  if (!sync || Object.keys(sync).length === 0) {
    return `${name} 未上传`
  }

  const parts: string[] = []
  if (sync.uploaded_at) {
    parts.push(`成功时间: ${formatSyncTime(sync.uploaded_at)}`)
  }
  if (sync.last_attempt_at) {
    parts.push(`最近尝试: ${formatSyncTime(sync.last_attempt_at)}`)
  }
  if (sync.last_message) {
    parts.push(`结果: ${sync.last_message}`)
  }
  return parts.join('\n') || `${name} 已记录状态`
}

function CliproxySyncSummary({ sync }: { sync: any }) {
  const meta = cliproxyStateMeta(sync)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={meta.color}>{meta.label}</Tag>
        {sync?.status ? <Tag>{`status: ${sync.status}`}</Tag> : null}
      </div>
      <SummaryField label="状态信息" value={sync?.status_message} code />
      <SummaryField label="auth-file" value={sync?.name} />
      <SummaryField label="API URL" value={sync?.base_url} />
      <SummaryField label="同步时间" value={sync?.last_synced_at ? formatSyncTime(sync.last_synced_at) : ''} />
      <SummaryField label="远端刷新时间" value={sync?.last_refresh ? formatSyncTime(sync.last_refresh) : ''} />
      <SummaryField label="下次重试时间" value={sync?.next_retry_after ? formatSyncTime(sync.next_retry_after) : ''} />
      <SummaryField label="探测信息" value={sync?.last_probe_message} code />
    </div>
  )
}

function TotpSecretAlert({ secret }: { secret: string }) {
  return (
    <Alert
      type="warning"
      showIcon
      message="TOTP 密钥只下发这一次，服务端取不回"
      description={
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Text
            style={{ fontFamily: 'monospace', fontSize: 13, wordBreak: 'break-all' }}
            copyable={{ text: secret, tooltips: ['复制密钥', '已复制'] }}
          >
            {secret}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            已随账号存库，之后可在账号详情或列表的「2FA 已绑」标签上再复制。
          </Text>
        </Space>
      }
    />
  )
}

function ActionMenu({ acc, onRefresh, actions }: { acc: any; onRefresh: () => void; actions: any[] }) {
  const [resultOpen, setResultOpen] = useState(false)
  const [resultTitle, setResultTitle] = useState('')
  const [resultStatus, setResultStatus] = useState<'success' | 'error'>('success')
  const [resultText, setResultText] = useState('')
  const [resultUrl, setResultUrl] = useState('')
  const [resultProbe, setResultProbe] = useState<any>(null)
  const [resultCliproxySync, setResultCliproxySync] = useState<any>(null)
  const [resultSecret, setResultSecret] = useState('')
  const [runningActionId, setRunningActionId] = useState<string | null>(null)
  const [taskModalTitle, setTaskModalTitle] = useState('')
  const [taskModalKind, setTaskModalKind] = useState<TaskKind>('backfill_rt')
  const [actionTaskId, setActionTaskId] = useState<string | null>(null)
  const [paymentOpen, setPaymentOpen] = useState(false)

  const showResult = (
    title: string,
    status: 'success' | 'error',
    text: string,
    url = '',
    probe: any = null,
    cliproxySync: any = null,
    secret = '',
  ) => {
    setResultTitle(title)
    setResultStatus(status)
    setResultText(text)
    setResultUrl(url)
    setResultProbe(probe)
    setResultCliproxySync(cliproxySync)
    setResultSecret(secret)
    setResultOpen(true)
  }

  const copyResultUrl = async () => {
    if (!resultUrl) return
    try {
      await navigator.clipboard.writeText(resultUrl)
      message.success('链接已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const runAsTask = async (actionId: string, actionLabel: string) => {
    const task = TASK_BACKED_ACTIONS[actionId]
    setRunningActionId(actionId)
    try {
      const result = await apiFetch(task.endpoint, {
        method: 'POST',
        body: JSON.stringify(task.body(acc.id)),
      })
      setTaskModalTitle(`${actionLabel} - ${acc.email}`)
      setTaskModalKind(task.kind)
      setActionTaskId(result.task_id)
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e)
      message.error(`${actionLabel}失败: ${detail}`)
    } finally {
      setRunningActionId(null)
    }
  }

  const handleAction = async (actionId: string) => {
    if (runningActionId) return
    const actionLabel = actions.find((item) => item.id === actionId)?.label || actionId
    const toastKey = `account-action:${acc?.id}:${actionId}`

    if (actionId === COPY_TOTP_ACTION_ID) {
      try {
        await navigator.clipboard.writeText(acc.totpSecret)
        message.success('2FA 密钥已复制')
      } catch {
        message.error('复制失败，请在账号详情里手动复制')
      }
      return
    }

    if (actionId === 'payment_channel_link' || actionId === 'payment_channel_pay') {
      setPaymentOpen(true)
      return
    }

    if (TASK_BACKED_ACTIONS[actionId]) {
      await runAsTask(actionId, actionLabel)
      return
    }

    setRunningActionId(actionId)
    message.loading({ content: `${actionLabel}运行中...`, key: toastKey, duration: 0 })

    try {
      const r = await apiFetch(`/actions/${acc.platform}/${acc.id}/${actionId}`, {
        method: 'POST',
        body: JSON.stringify({ params: {} }),
      })
      if (!r.ok) {
        const data = r.data || {}
        const probe = typeof data === 'object' && data ? data.probe || null : null
        const cliproxySync = typeof data === 'object' && data ? data.sync || null : null
        message.error({ content: `${actionLabel}失败`, key: toastKey })
        showResult(actionLabel, 'error', r.error || data.message || '操作失败', '', probe, cliproxySync)
        onRefresh()
        return
      }
      const data = r.data || {}
      // 绑 2FA 只在这一次响应里下发密钥，弹窗里必须能一键复制，关掉就没了
      const secret = typeof data === 'object' && data ? String(data.totp_secret || '') : ''
      if (secret) {
        message.success({ content: data.message || `${actionLabel}完成`, key: toastKey })
        showResult(actionLabel, 'success', String(data.message || '操作成功'), '', null, null, secret)
        onRefresh()
        return
      }
      if (data.url || data.checkout_url || data.cashier_url) {
        const targetUrl = data.url || data.checkout_url || data.cashier_url
        message.success({ content: `${actionLabel}完成`, key: toastKey })
        showResult(actionLabel, 'success', '操作成功，请在弹窗中打开或复制链接。', targetUrl)
      } else {
        message.success({ content: data.message || `${actionLabel}完成`, key: toastKey })
        const probe = typeof data === 'object' && data ? data.probe || null : null
        const cliproxySync = typeof data === 'object' && data ? data.sync || null : null
        const text =
          probe
            ? String(data.message || '操作成功')
            : cliproxySync
            ? String(data.message || '操作成功')
            : typeof data === 'string'
            ? data
            : Object.keys(data).length > 0
              ? JSON.stringify(data, null, 2)
              : '操作成功'
        showResult(actionLabel, 'success', text, '', probe, cliproxySync)
      }
      onRefresh()
    } catch (e: any) {
      const detail = e?.message ? String(e.message) : '请求失败'
      message.error({ content: detail, key: toastKey })
      showResult(actionLabel, 'error', detail)
    } finally {
      setRunningActionId(null)
    }
  }

  const menuItems: MenuProps['items'] = [
    ...(acc.totpSecret
      ? [{ key: COPY_TOTP_ACTION_ID, label: '复制 2FA 密钥' }]
      : []),
    ...actions.map((a) => ({
      key: a.id,
      label: runningActionId === a.id ? `${a.label}（运行中）` : a.label,
      disabled: Boolean(runningActionId),
    })),
  ]

  if (menuItems.length === 0) return null

  return (
    <>
      <Dropdown
        menu={{
          items: menuItems,
          onClick: ({ key }) => handleAction(String(key)),
        }}
      >
        <Button
          type="link"
          size="small"
          icon={<MoreOutlined />}
          loading={Boolean(runningActionId)}
        />
      </Dropdown>
      <Modal
        title={taskModalTitle}
        open={Boolean(actionTaskId)}
        onCancel={() => { setActionTaskId(null); onRefresh() }}
        footer={null}
        width={620}
        maskClosable={false}
      >
        {taskModalKind === 'bind_2fa' && acc.totpSecret ? (
          <div style={{ marginBottom: 12 }}>
            <TotpSecretAlert secret={acc.totpSecret} />
          </div>
        ) : null}
        {actionTaskId ? <TaskLogPanel taskId={actionTaskId} kind={taskModalKind} onDone={onRefresh} /> : null}
      </Modal>
      <Modal
        title={resultTitle}
        open={resultOpen}
        onCancel={() => setResultOpen(false)}
        footer={[
          resultUrl ? (
            <Button key="copy" onClick={copyResultUrl}>
              复制链接
            </Button>
          ) : null,
          resultUrl ? (
            <Button
              key="open"
              type="primary"
              onClick={() => window.open(resultUrl, '_blank', 'noopener,noreferrer')}
            >
              打开链接
            </Button>
          ) : null,
          <Button key="ok" type={resultUrl ? 'default' : 'primary'} onClick={() => setResultOpen(false)}>
            确定
          </Button>,
        ].filter(Boolean)}
        maskClosable={false}
      >
        <Alert
          type={resultStatus}
          showIcon
          message={resultStatus === 'success' ? '操作完成' : '操作失败'}
          style={{ marginBottom: 12 }}
        />
        {resultProbe ? (
          <div style={{ marginBottom: 12 }}>
            <LocalProbeSummary probe={resultProbe} />
          </div>
        ) : null}
        {resultCliproxySync ? (
          <div style={{ marginBottom: 12 }}>
            <CliproxySyncSummary sync={resultCliproxySync} />
          </div>
        ) : null}
        {resultSecret ? (
          <div style={{ marginBottom: 12 }}>
            <TotpSecretAlert secret={resultSecret} />
          </div>
        ) : null}
        {resultUrl ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Text copyable={{ text: resultUrl }} style={{ wordBreak: 'break-all' }}>
              {resultUrl}
            </Text>
          </Space>
        ) : null}
        {resultText ? (
          <pre
            style={{
              margin: 0,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              fontFamily: 'monospace',
              fontSize: 12,
            }}
          >
            {resultText}
          </pre>
        ) : null}
      </Modal>
      <PaymentOperationDrawer
        account={acc}
        open={paymentOpen}
        onClose={() => setPaymentOpen(false)}
        onDone={onRefresh}
      />
    </>
  )
}

export default function Accounts() {
  const { platform } = useParams<{ platform: string }>()
  const { token } = theme.useToken()
  const [currentPlatform, setCurrentPlatform] = useState(platform || 'chatgpt')
  const [accounts, setAccounts] = useState<any[]>([])
  const [platformActions, setPlatformActions] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [filterPlusStatus, setFilterPlusStatus] = useState('')
  const [filterSmsVerified, setFilterSmsVerified] = useState('')
  const [createdAtStart, setCreatedAtStart] = useState('')
  const [createdAtEnd, setCreatedAtEnd] = useState('')
  const [dateFiltersOpen, setDateFiltersOpen] = useState(false)
  const { viewportRef, bodyHeight } = useTableViewport()
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])

  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [exportModalOpen, setExportModalOpen] = useState(false)
  const [detailModalOpen, setDetailModalOpen] = useState(false)
  const [currentAccount, setCurrentAccount] = useState<any>(null)

  const [registerForm] = Form.useForm()
  const [addForm] = Form.useForm()
  const [detailForm] = Form.useForm()
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()
  const { registerFlow: chatgptRegisterFlow, setRegisterFlow: setChatgptRegisterFlow } =
    usePersistentChatGPTRegisterFlow()
  const { bind2fa: chatgptBind2fa, setBind2fa: setChatgptBind2fa } =
    usePersistentChatGPTBind2fa()
  const [importText, setImportText] = useState('')
  const [importLoading, setImportLoading] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [registerLoading, setRegisterLoading] = useState(false)
  const [batchUploadLoading, setBatchUploadLoading] = useState<'' | `${BatchUploadActionId}_${'all' | 'selected'}`>('')
  const [statusSyncLoading, setStatusSyncLoading] = useState<
    'probe_selected' | 'probe_all' | 'remote_selected' | 'remote_all' | 'plus_selected' | 'plus_all' | ''
  >('')
  const [backfillRtModalOpen, setBackfillRtModalOpen] = useState(false)
  const [backfillRtLoading, setBackfillRtLoading] = useState(false)
  const [backfillRtTaskId, setBackfillRtTaskId] = useState<string | null>(null)
  const [backfillRtForm] = Form.useForm()

  useEffect(() => {
    if (platform) setCurrentPlatform(platform)
  }, [platform])

  useEffect(() => {
    if (!detailModalOpen || !currentAccount) return
    detailForm.setFieldsValue({
      status: currentAccount.status,
      token: currentAccount.token,
    })
  }, [detailModalOpen, currentAccount, detailForm])

  const load = useCallback(async () => {
    if (createdAtStart && createdAtEnd && new Date(createdAtStart).getTime() > new Date(createdAtEnd).getTime()) {
      message.warning('开始时间不能晚于结束时间')
      setAccounts([])
      setTotal(0)
      return
    }

    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: currentPlatform, page: String(page), page_size: String(pageSize) })
      if (search) params.set('email', search)
      if (filterStatus) params.set('status', filterStatus)
      if (filterPlusStatus) params.set('plus_status', filterPlusStatus)
      if (filterSmsVerified) params.set('sms_verified', filterSmsVerified)
      if (createdAtStart) params.set('created_at_start', createdAtStart)
      if (createdAtEnd) params.set('created_at_end', createdAtEnd)
      const data = await apiFetch(`/accounts?${params}`)
      setAccounts((data.items || []).map(normalizeAccount))
      setTotal(data.total)
    } finally {
      setLoading(false)
    }
  }, [currentPlatform, search, filterStatus, filterPlusStatus, filterSmsVerified, createdAtStart, createdAtEnd, page, pageSize])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    apiFetch(`/actions/${currentPlatform}`)
      .then((data) => setPlatformActions(data.actions || []))
      .catch(() => setPlatformActions([]))
  }, [currentPlatform])

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text)
    message.success('已复制')
  }

  const copySecret = (label: string, text: string) => {
    if (!text) {
      message.warning(`该账号没有${label}`)
      return
    }
    navigator.clipboard.writeText(text)
    message.success(`${label}已复制`)
  }

  const getRefreshToken = (record: { extra?: Record<string, unknown>; extra_json?: string }): string => {
    const extra = record.extra || parseExtraJson(record.extra_json)
    const refreshToken = extra.refresh_token || extra.refreshToken
    return typeof refreshToken === 'string' ? refreshToken : ''
  }

  const handleDelete = async (id: number) => {
    await apiFetch(`/accounts/${id}`, { method: 'DELETE' })
    message.success('删除成功')
    load()
  }

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return
    await apiFetch('/accounts/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids: Array.from(selectedRowKeys) }),
    })
    message.success('批量删除成功')
    setSelectedRowKeys([])
    load()
  }

  const handleAdd = async () => {
    const values = await addForm.validateFields()
    await apiFetch('/accounts', {
      method: 'POST',
      body: JSON.stringify({ ...values, platform: currentPlatform }),
    })
    message.success('添加成功')
    setAddModalOpen(false)
    addForm.resetFields()
    load()
  }

  const handleImport = async () => {
    if (!importText.trim()) return
    setImportLoading(true)
    try {
      const lines = importText.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', {
        method: 'POST',
        body: JSON.stringify({ platform: currentPlatform, lines }),
      })
      message.success(`导入成功 ${res.created} 个`)
      setImportModalOpen(false)
      setImportText('')
      load()
    } catch (e: any) {
      message.error(`导入失败: ${e.message}`)
    } finally {
      setImportLoading(false)
    }
  }

  const handleRegister = async () => {
    const values = await registerForm.validateFields()
    setRegisterLoading(true)
    try {
      const cfg = await apiFetch('/config')
      const executorType = normalizeExecutorForPlatform(currentPlatform, cfg.default_executor)
      const registerExtra = {
        mail_provider: cfg.mail_provider || 'luckmail',
        applemail_base_url: cfg.applemail_base_url,
        applemail_pool_dir: cfg.applemail_pool_dir,
        applemail_pool_file: cfg.applemail_pool_file,
        applemail_mailboxes: cfg.applemail_mailboxes,
        laoudo_auth: cfg.laoudo_auth,
        laoudo_email: cfg.laoudo_email,
        laoudo_account_id: cfg.laoudo_account_id,
        gptmail_base_url: cfg.gptmail_base_url,
        gptmail_api_key: cfg.gptmail_api_key,
        gptmail_domain: cfg.gptmail_domain,
        maliapi_base_url: cfg.maliapi_base_url,
        maliapi_api_key: cfg.maliapi_api_key,
        maliapi_domain: cfg.maliapi_domain,
        maliapi_auto_domain_strategy: cfg.maliapi_auto_domain_strategy,
        yescaptcha_key: cfg.yescaptcha_key,
        moemail_api_url: cfg.moemail_api_url,
        moemail_api_key: cfg.moemail_api_key,
        skymail_api_base: cfg.skymail_api_base,
        skymail_token: cfg.skymail_token,
        skymail_domain: cfg.skymail_domain,
        cloudmail_api_base: cfg.cloudmail_api_base,
        cloudmail_admin_email: cfg.cloudmail_admin_email,
        cloudmail_admin_password: cfg.cloudmail_admin_password,
        cloudmail_domain: cfg.cloudmail_domain,
        cloudmail_subdomain: cfg.cloudmail_subdomain,
        cloudmail_timeout: cfg.cloudmail_timeout,
        duckmail_address: cfg.duckmail_address,
        duckmail_password: cfg.duckmail_password,
        duckmail_api_url: cfg.duckmail_api_url,
        duckmail_provider_url: cfg.duckmail_provider_url,
        duckmail_bearer: cfg.duckmail_bearer,
        freemail_api_url: cfg.freemail_api_url,
        freemail_admin_token: cfg.freemail_admin_token,
        freemail_username: cfg.freemail_username,
        freemail_password: cfg.freemail_password,
        freemail_domain: cfg.freemail_domain,
        cfworker_api_url: cfg.cfworker_api_url,
        cfworker_admin_token: cfg.cfworker_admin_token,
        cfworker_custom_auth: cfg.cfworker_custom_auth,
        cfworker_domain: cfg.cfworker_domain,
        cfworker_subdomain: cfg.cfworker_subdomain,
        cfworker_random_subdomain: parseBooleanConfigValue(cfg.cfworker_random_subdomain),
        cfworker_random_name_subdomain: parseBooleanConfigValue(cfg.cfworker_random_name_subdomain),
        cfworker_fingerprint: cfg.cfworker_fingerprint,
        luckmail_base_url: cfg.luckmail_base_url,
        luckmail_api_key: cfg.luckmail_api_key,
        luckmail_email_type: cfg.luckmail_email_type,
        luckmail_domain: cfg.luckmail_domain,
      }
      const chatgptRegistrationRequestAdapter =
        buildChatGPTRegistrationRequestAdapter(
          currentPlatform,
          chatgptRegistrationMode,
          chatgptRegisterFlow,
          chatgptBind2fa,
        )
      const adaptedRegisterExtra = chatgptRegistrationRequestAdapter
        ? chatgptRegistrationRequestAdapter.extendExtra(registerExtra)
        : registerExtra

      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: currentPlatform,
          count: values.count,
          concurrency: values.concurrency,
          register_retry_times: normalizeRegisterRetryTimes(values.register_retry_times),
          register_delay_seconds: values.register_delay_seconds || 0,
          executor_type: executorType,
          captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
          proxy: null,
          extra: adaptedRegisterExtra,
        }),
      })
      setTaskId(res.task_id)
    } finally {
      setRegisterLoading(false)
    }
  }

  const handleDetailSave = async () => {
    const values = await detailForm.validateFields()
    await apiFetch(`/accounts/${currentAccount.id}`, {
      method: 'PATCH',
      body: JSON.stringify(values),
    })
    message.success('保存成功')
    setDetailModalOpen(false)
    load()
  }

  const showBatchActionResult = (title: string, result: any) => {
    const lines = (result.items || [])
      .filter((item: any) => !item.ok)
      .map((item: any) => `[${item.id || '-'}] ${item.email || '-'}: ${item.message || '失败'}`)

    if (lines.length === 0) return

    Modal.info({
      title,
      width: 760,
      content: (
        <pre
          style={{
            margin: 0,
            maxHeight: 360,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'var(--bg-subtle)',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </pre>
      ),
    })
  }

  const handleBatchStatusSync = async (kind: 'probe' | 'remote' | 'plus', scope: 'selected' | 'all') => {
    if (currentPlatform !== 'chatgpt') return

    const loadingKey = `${kind}_${scope}` as typeof statusSyncLoading
    const actionId =
      kind === 'probe' ? 'probe_local_status' : kind === 'plus' ? 'check_plus_trial' : 'sync_cliproxyapi_status'
    const actionLabel =
      kind === 'probe' ? '本地状态同步' : kind === 'plus' ? 'Plus 试用检测' : 'CLIProxyAPI 状态同步'
    const scopeLabel = scope === 'selected' ? '所选账号' : '当前筛选账号'
    const toastKey = `status-sync:${loadingKey}`

    const body: Record<string, unknown> = {
      params: {},
    }

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning('请先选择要同步的账号')
        return
      }
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
      if (filterPlusStatus) body.plus_status = filterPlusStatus
      if (filterSmsVerified) body.sms_verified = filterSmsVerified
    }

    setStatusSyncLoading(loadingKey)
    message.loading({ content: `${scopeLabel}${actionLabel}进行中...`, key: toastKey, duration: 0 })
    try {
      const result = await apiFetch(`/actions/${currentPlatform}/${actionId}/batch`, {
        method: 'POST',
        body: JSON.stringify(body),
      })

      if (!result.total) {
        message.info({ content: '没有可处理的账号', key: toastKey })
      } else if (!result.failed) {
        message.success({ content: `${scopeLabel}${actionLabel}完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else if (!result.success) {
        message.error({ content: `${scopeLabel}${actionLabel}失败：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else {
        message.warning({ content: `${scopeLabel}${actionLabel}部分完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      }

      showBatchActionResult(`${scopeLabel}${actionLabel}结果`, result)
      await load()
    } catch (e: any) {
      message.error({ content: `${actionLabel}失败: ${e.message}`, key: toastKey })
    } finally {
      setStatusSyncLoading('')
    }
  }

  const handleBatchUpload = async (actionId: BatchUploadActionId, scope: 'selected' | 'all') => {
    const action = BATCH_UPLOAD_ACTIONS.find((item) => item.id === actionId)
    if (!action) return

    const toastKey = `batch-upload:${actionId}:${scope}`
    const scopeLabel = scope === 'selected' ? '所选账号' : '当前筛选账号'

    const body: Record<string, unknown> = {
      params: {},
    }

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning(`请先选择要${action.label}的账号`)
        return
      }
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
      if (filterPlusStatus) body.plus_status = filterPlusStatus
      if (filterSmsVerified) body.sms_verified = filterSmsVerified
    }

    setBatchUploadLoading(`${actionId}_${scope}`)
    message.loading({ content: `${scopeLabel}${action.label}进行中...`, key: toastKey, duration: 0 })
    try {
      const result = await apiFetch(`/actions/${currentPlatform}/${actionId}/batch`, {
        method: 'POST',
        body: JSON.stringify(body),
      })

      if (!result.total) {
        message.info({ content: '没有可处理的账号', key: toastKey })
      } else if (!result.failed) {
        message.success({ content: `${scopeLabel}${action.label}完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else if (!result.success) {
        message.error({ content: `${scopeLabel}${action.label}失败：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else {
        message.warning({ content: `${scopeLabel}${action.label}部分完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      }

      showBatchActionResult(`${scopeLabel}${action.label}结果`, result)
      await load()
    } catch (e: any) {
      message.error({ content: `${action.label}失败: ${e.message}`, key: toastKey })
    } finally {
      setBatchUploadLoading('')
    }
  }

  const missingRtCount = accounts.filter((item) => !getRefreshToken(item)).length

  const handleBackfillRt = async () => {
    const values = await backfillRtForm.validateFields()
    const scope = selectedRowKeys.length > 0 ? 'selected' : 'all'

    const body: Record<string, unknown> = {
      only_missing_rt: values.only_missing_rt !== false,
      allow_login: values.allow_login !== false,
      concurrency: Number(values.concurrency) || 1,
      delay_seconds: Number(values.delay_seconds) || 0,
    }

    if (scope === 'selected') {
      body.account_ids = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
      if (filterPlusStatus) body.plus_status = filterPlusStatus
      if (filterSmsVerified) body.sms_verified = filterSmsVerified
    }

    setBackfillRtLoading(true)
    try {
      const result = await apiFetch('/tasks/backfill-rt', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      setBackfillRtTaskId(result.task_id)
      message.success(`已开始给 ${result.total} 个账号补 RT`)
    } catch (e) {
      message.error(`补 RT 启动失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBackfillRtLoading(false)
    }
  }

  const closeBackfillRtModal = () => {
    setBackfillRtModalOpen(false)
    setBackfillRtTaskId(null)
    backfillRtForm.resetFields()
  }

  const getStatusSyncScope = (): 'selected' | 'all' => (selectedRowKeys.length > 0 ? 'selected' : 'all')

  const getBatchUploadScope = (): 'selected' | 'all' => (selectedRowKeys.length > 0 ? 'selected' : 'all')

  const isChatgptPlatform = currentPlatform === 'chatgpt'
  const hasUploadCpaAction = platformActions.some((item) => item?.id === 'upload_cpa')
  const batchUploadActions = BATCH_UPLOAD_ACTIONS.filter((action) =>
    platformActions.some((item) => item?.id === action.id),
  )
  const monospaceStyle: React.CSSProperties = {
    fontFamily: 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    fontSize: 12,
  }
  const secondaryTextStyle: React.CSSProperties = {
    fontSize: 12,
    color: token.colorTextSecondary,
  }
  const cellStackStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    minWidth: 0,
  }
  // 复制按钮紧跟在截断后的密文右边，别被超长 token 顶到列的最右侧。
  const secretCellStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    minWidth: 0,
  }
  const secretPreviewStyle: React.CSSProperties = {
    ...monospaceStyle,
    flex: 1,
    minWidth: 0,
    filter: 'blur(4px)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    opacity: 0.9,
  }
  const compactPanelStyle: React.CSSProperties = {
    padding: '8px 10px',
    borderRadius: token.borderRadiusLG,
    border: `1px solid ${token.colorBorder}`,
    background: token.colorFillAlter,
  }

  const columns: any[] = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 260,
      render: (text: string, record: any) => (
        <div style={cellStackStyle}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
            <Text
              style={{ ...monospaceStyle, flex: 1, minWidth: 0, whiteSpace: 'nowrap' }}
              ellipsis={{ tooltip: text }}
            >
              {text}
            </Text>
            <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
          </div>
          <Text type="secondary" style={secondaryTextStyle} ellipsis={{ tooltip: record.user_id || `账号 #${record.id}` }}>
            {record.user_id ? `UID: ${record.user_id}` : `账号 #${record.id}`}
          </Text>
          {record.webPoolOnly ? <Tag color="cyan">Web 注册来源</Tag> : null}
        </div>
      ),
    },
    {
      title: '密码',
      dataIndex: 'password',
      key: 'password',
      width: 150,
      render: (text: string) => (
        <div style={secretCellStyle}>
          <Text style={secretPreviewStyle} title={text}>
            {text}
          </Text>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
        </div>
      ),
    },
    {
      title: 'RT',
      key: 'refresh_token',
      width: 150,
      render: (_: any, record: any) => {
        const rt = getRefreshToken(record)
        if (!rt) return <span style={{ color: 'var(--text-muted)' }}>-</span>
        return (
          <div style={secretCellStyle}>
            <Text style={{ ...secretPreviewStyle, fontSize: 11 }} title={rt}>
              {rt}
            </Text>
            <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(rt)} />
          </div>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: string) => <Tag color={STATUS_COLORS[status] || 'default'}>{status}</Tag>,
    },
    {
      title: '接码记录',
      key: 'sms_verification',
      width: 130,
      render: (_: unknown, record: { smsVerification?: SmsVerification }) => {
        const meta = smsVerificationMeta(record.smsVerification)
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
  ]

  if (isChatgptPlatform) {
    columns.push(
      {
        title: 'RT 测活',
        key: 'rt_probe',
        width: 150,
        render: (_: unknown, record: { chatgptLocal?: AccountProbe; extra_json?: string }) => {
          const meta = rtProbeMeta(record.chatgptLocal, Boolean(getRefreshToken(record)))
          const probeMessage = record.chatgptLocal?.codex?.message || record.chatgptLocal?.auth?.message
          return <Tag color={meta.color} title={probeMessage}>{meta.label}</Tag>
        },
      },
      {
        title: '本地状态',
        key: 'chatgpt_local_state',
        width: 320,
        render: (_: any, record: any) => {
          const auth = record.chatgptLocal?.auth || {}
          const subscription = record.chatgptLocal?.subscription || {}
          const codex = record.chatgptLocal?.codex || {}
          const cpaSync = record.cpaSync || {}
          const sub2apiSync = record.sub2apiSync || {}
          const authMeta = authStateMeta(auth.state)
          const planTag = planMeta(subscription.plan)
          const codexMeta = codexStateMeta(codex.state)
          const cpaMeta = uploadSyncMeta(cpaSync)
          const sub2apiMeta = uploadSyncMeta(sub2apiSync)
          const smsMeta = smsVerificationMeta(record.smsVerification)
          const hasRefreshToken = Boolean(getRefreshToken(record))

          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              {record.webPoolOnly ? <WebPoolSyncSummary sync={record.webPoolSync} /> : null}
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                <Tag color={authMeta.color}>{authMeta.label}</Tag>
                <Tag color={planTag.color}>{planTag.label}</Tag>
                <Tag color={codexMeta.color}>Codex {codexMeta.label}</Tag>
              </div>
              {record.webPoolOnly ? (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  <Tag>{hasRefreshToken ? '有 RT' : '无 RT'}</Tag>
                  <Tag color={smsMeta.color}>{smsMeta.label}</Tag>
                </div>
              ) : null}
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                <Tag color={cpaMeta.color} title={uploadSyncTitle('CPA', cpaSync)}>
                  CPA {cpaMeta.label}
                </Tag>
                <Tag color={sub2apiMeta.color} title={uploadSyncTitle('Sub2API', sub2apiSync)}>
                  Sub2API {sub2apiMeta.label}
                </Tag>
                {record.totpSecret ? (
                  <Tag
                    color="purple"
                    title="点击复制 TOTP 密钥，可直接导入验证器"
                    style={{ cursor: 'pointer', marginInlineEnd: 0 }}
                    onClick={() => copySecret('2FA 密钥', record.totpSecret)}
                  >
                    <Space size={4}>
                      2FA 已绑
                      <CopyOutlined />
                    </Space>
                  </Tag>
                ) : null}
              </div>
            </div>
          )
        },
      },
      {
        title: 'Plus 试用',
        key: 'plus_check',
        width: 140,
        render: (_: unknown, record: { plusCheck?: PlusCheck }) => {
          const check = record.plusCheck || {}
          const meta = plusTrialMeta(check.status)
          const checkedAt = formatSyncTime(check.checked_at)
          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <Tag color={meta.color} title={check.message || ''}>
                {meta.label}
              </Tag>
              {checkedAt && (
                <Text type="secondary" style={secondaryTextStyle} ellipsis={{ tooltip: checkedAt }}>
                  {checkedAt}
                </Text>
              )}
            </div>
          )
        },
      },
    )
  } else {
    if (hasUploadCpaAction) {
      columns.push({
        title: 'CPA',
        key: 'cpa_sync',
        width: 120,
        render: (_: any, record: any) => {
          const cpaMeta = uploadSyncMeta(record.cpaSync || {})
          return (
            <Tag color={cpaMeta.color} title={uploadSyncTitle('CPA', record.cpaSync || {})}>
              {cpaMeta.label}
            </Tag>
          )
        },
      })
    }

    columns.push(
      {
        title: '地区',
        dataIndex: 'region',
        key: 'region',
        width: 100,
        render: (text: string) => text || '-',
      },
      {
        title: '试用链接',
        dataIndex: 'cashier_url',
        key: 'cashier_url',
        width: 120,
        render: (url: string) =>
          url ? (
            <Space size={0}>
              <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(url)} />
              <Button type="text" size="small" icon={<LinkOutlined />} onClick={() => window.open(url, '_blank')} />
            </Space>
          ) : (
            '-'
          ),
      },
    )
  }

  columns.push(
    {
      title: '注册时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 132,
      render: (text: string) => {
        const formatted = formatCreatedAt(text)
        return (
          <div style={cellStackStyle}>
            <Text style={{ fontSize: 13 }}>{formatted.date}</Text>
            {formatted.time ? <Text type="secondary" style={secondaryTextStyle}>{formatted.time}</Text> : null}
          </div>
        )
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      fixed: isChatgptPlatform ? 'right' : undefined,
      render: (_: any, record: any) => (
        <Space size={4} wrap>
          <Button type="link" size="small" onClick={() => { setCurrentAccount(record); setDetailModalOpen(true); }}>
            详情
          </Button>
          <Popconfirm
            title="确认删除该账号吗？"
            onConfirm={() => handleDelete(record.id)}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
          <ActionMenu acc={record} onRefresh={load} actions={platformActions} />
        </Space>
      ),
    },
  )

  const statusSyncMenuItems: MenuProps['items'] = [
    {
      key: `probe:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选本地状态 (${selectedRowKeys.length})`
          : `同步当前筛选本地状态 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
    {
      key: `plus:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `检测所选 Plus 试用资格 (${selectedRowKeys.length})`
          : `检测当前筛选 Plus 试用资格 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
    {
      key: `remote:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选 CLIProxyAPI 状态 (${selectedRowKeys.length})`
          : `同步当前筛选 CLIProxyAPI 状态 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
  ]

  const batchUploadScope = getBatchUploadScope()
  const batchUploadCount = batchUploadScope === 'selected' ? selectedRowKeys.length : total
  const batchUploadMenuItems: MenuProps['items'] = batchUploadActions.map((action) => ({
    key: action.id,
    label: action.label,
    disabled: batchUploadCount === 0 || batchUploadLoading !== '',
  }))

  return (
    <div className="admin-page admin-list-page">
      <AdminPageHeader title={`${currentPlatform === 'chatgpt' ? 'ChatGPT' : currentPlatform} 账号`} kicker="ACCOUNTS"
        description="按账号来源、当前能力与同步状态统一管理。"
        extra={<><Text type="secondary">共 {total} 个账号</Text><Button icon={<ReloadOutlined spin={loading} />} onClick={load} aria-label="刷新账号" /></>} />
      <Card className="admin-panel admin-list-controls">
      <div className="admin-compact-toolbar">
        <div className="admin-filter-group">
          <Input.Search
            placeholder="搜索邮箱..."
            allowClear
            onSearch={(v) => { setPage(1); setSearch(v) }}
            style={{ width: 200 }}
          />
          <Select
            placeholder="状态筛选"
            allowClear
            style={{ width: 120 }}
            onChange={(v) => { setPage(1); setFilterStatus(v) }}
            options={[
              { value: 'registered', label: '已注册' },
              { value: 'trial', label: '试用中' },
              { value: 'subscribed', label: '已订阅' },
              { value: 'expired', label: '已过期' },
              { value: 'invalid', label: '已失效' },
              { value: 'deleted', label: '已删除/停用' },
            ]}
          />
          {currentPlatform === 'chatgpt' && (
            <Select
              placeholder="Plus 试用"
              allowClear
              style={{ width: 150 }}
              onChange={(v) => { setPage(1); setFilterPlusStatus(v || '') }}
              options={PLUS_TRIAL_FILTERS}
            />
          )}
          <Select
            placeholder="接码记录筛选"
            allowClear
            style={{ width: 130 }}
            onChange={(v) => { setPage(1); setFilterSmsVerified(v || '') }}
            options={[
              { value: 'true', label: '已接过码' },
              { value: 'false', label: '无接码记录' },
            ]}
          />
          <Button icon={<FilterOutlined />} type={dateFiltersOpen || createdAtStart || createdAtEnd ? 'primary' : 'default'} onClick={() => setDateFiltersOpen(!dateFiltersOpen)}>时间筛选</Button>
          <div style={{ display: dateFiltersOpen || createdAtStart || createdAtEnd ? 'flex' : 'none', gap: 8, flexWrap: 'wrap' }}>
          <DatePicker
            showTime
            allowClear
            placeholder="开始时间"
            onChange={(value) => { setPage(1); setCreatedAtStart(value ? value.toISOString() : '') }}
          />
          <DatePicker
            showTime
            allowClear
            placeholder="结束时间"
            onChange={(value) => { setPage(1); setCreatedAtEnd(value ? value.toISOString() : '') }}
          />
          </div>
        </div>
      </div>
      <div className="admin-save-bar">
        <Text type={selectedRowKeys.length > 0 ? 'success' : 'secondary'} style={{ fontSize: 12 }}>{selectedRowKeys.length > 0 ? `已选 ${selectedRowKeys.length} 个账号` : '未勾选时，批量操作使用当前筛选范围'}</Text>
        <Space wrap className="admin-action-group">
          {currentPlatform === 'chatgpt' && (
            <Dropdown
              trigger={['click']}
              menu={{
                items: statusSyncMenuItems,
                onClick: ({ key }) => {
                  const [kind, scope] = String(key).split(':') as [
                    'probe' | 'remote' | 'plus',
                    'selected' | 'all',
                  ]
                  handleBatchStatusSync(kind, scope)
                },
              }}
            >
              <Button
                icon={<SyncOutlined />}
                loading={statusSyncLoading !== ''}
                disabled={total === 0}
              >
                状态同步
              </Button>
            </Dropdown>
          )}
          {currentPlatform === 'chatgpt' && (
            // 备注：把常用的 RT 测活直接露出，范围沿用当前勾选或筛选结果。
            <Button
              icon={<SyncOutlined />}
              loading={statusSyncLoading === `probe_${getStatusSyncScope()}`}
              disabled={total === 0}
              onClick={() => handleBatchStatusSync('probe', getStatusSyncScope())}
            >
              RT 测活
            </Button>
          )}
          {currentPlatform === 'chatgpt' && (
            <Button
              icon={<KeyOutlined />}
              onClick={() => setBackfillRtModalOpen(true)}
              disabled={total === 0}
            >
              {selectedRowKeys.length > 0 ? `补 RT (${selectedRowKeys.length})` : '补 RT'}
            </Button>
          )}
          {isChatgptPlatform && batchUploadMenuItems.length > 0 && (
            <Dropdown
              trigger={['click']}
              menu={{
                items: batchUploadMenuItems,
                onClick: ({ key }) => {
                  const action = BATCH_UPLOAD_ACTIONS.find((item) => item.id === key)
                  if (!action) return
                  const scopeLabel = batchUploadScope === 'selected' ? `所选 ${batchUploadCount} 个账号` : `当前筛选 ${batchUploadCount} 个账号`
                  Modal.confirm({
                    title: `确认${scopeLabel}${action.label}？`,
                    content: '批量操作将逐个执行，并在完成后显示失败明细。',
                    okText: '确认',
                    cancelText: '取消',
                    onOk: () => handleBatchUpload(action.id, batchUploadScope),
                  })
                },
              }}
            >
              <Button
                icon={<UploadOutlined />}
                loading={batchUploadLoading !== ''}
                disabled={batchUploadCount === 0}
              >
                {batchUploadScope === 'selected' ? `上传 (${batchUploadCount})` : '上传'}
              </Button>
            </Dropdown>
          )}
          {!isChatgptPlatform && hasUploadCpaAction && (
            <Popconfirm
              title={
                getBatchUploadScope() === 'selected'
                  ? `确认上传所选 ${selectedRowKeys.length} 个账号到 CPA？`
                  : `确认上传当前筛选范围内 ${total} 个账号到 CPA？`
              }
              onConfirm={() => handleBatchUpload('upload_cpa', getBatchUploadScope())}
              okText="确认"
              cancelText="取消"
            >
              <Button
                loading={batchUploadLoading === 'upload_cpa_selected' || batchUploadLoading === 'upload_cpa_all'}
                icon={<UploadOutlined />}
                disabled={getBatchUploadScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0}
              >
                {getBatchUploadScope() === 'selected' ? `上传所选 CPA (${selectedRowKeys.length})` : `上传筛选 CPA (${total})`}
              </Button>
            </Popconfirm>
          )}
          {selectedRowKeys.length > 0 && (
            <Popconfirm
              title={`确认删除选中的 ${selectedRowKeys.length} 个账号？`}
              onConfirm={handleBatchDelete}
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
            >
              <Button danger icon={<DeleteOutlined />}>删除 {selectedRowKeys.length} 个</Button>
            </Popconfirm>
          )}
          <Button icon={<UploadOutlined />} onClick={() => setImportModalOpen(true)}>导入</Button>
          <Button
            icon={<DownloadOutlined />}
            onClick={() => setExportModalOpen(true)}
            disabled={total === 0}
          >
            {selectedRowKeys.length > 0 ? `导出 (${selectedRowKeys.length})` : '导出'}
          </Button>
          <Button icon={<PlusOutlined />} onClick={() => setAddModalOpen(true)}>新增</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setRegisterModalOpen(true)}>注册</Button>
        </Space>
      </div>
      </Card>

      <div ref={viewportRef} className="admin-table-viewport">
      <Table className="admin-table"
        rowKey="id"
        columns={columns}
        dataSource={accounts}
        loading={loading}
        size="middle"
        rowSelection={{
          selectedRowKeys,
          onChange: setSelectedRowKeys,
        }}
        pagination={{ total, current: page, pageSize, showSizeChanger: true, pageSizeOptions: ['20', '50', '100'], onChange: (p, ps) => { setPage(p); setPageSize(ps) } }}
        scroll={{ x: isChatgptPlatform ? 1300 : 980, y: bodyHeight }}
        onRow={(record) => ({
          onDoubleClick: () => {
            setCurrentAccount(record)
            setDetailModalOpen(true)
          },
        })}
      />
      </div>

      <Modal
        title={`注册 ${currentPlatform}`}
        open={registerModalOpen}
        onCancel={() => { setRegisterModalOpen(false); setTaskId(null); registerForm.resetFields(); }}
        footer={null}
        width={500}
        maskClosable={false}
      >
        {!taskId ? (
          <Form form={registerForm} layout="vertical" onFinish={handleRegister}>
            <Form.Item name="count" label="注册数量" initialValue={1} rules={[{ required: true }]}>
              <Input type="number" min={1} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数" initialValue={1} rules={[{ required: true }]}>
              <Input type="number" min={1} />
            </Form.Item>
            <Form.Item name="register_delay_seconds" label="每个注册延迟(秒)" initialValue={0}>
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 = 不延迟" />
            </Form.Item>
            <Form.Item
              name="register_retry_times"
              label="失败重试轮数"
              initialValue={DEFAULT_REGISTER_RETRY_TIMES}
              tooltip="整条注册流程失败后自动重开一轮：换新邮箱 / 新号码 / 新会话。0 表示失败即止。手机注册连续两轮都「建出了号却一条短信都没收到」时会提前收手，不再多造孤号。"
            >
              <InputNumber min={0} max={10} precision={0} style={{ width: '100%' }} placeholder="1" />
            </Form.Item>
            {currentPlatform === 'chatgpt' && (
              <>
                <Form.Item label="注册方式">
                  <ChatGPTRegisterFlowSelect
                    flow={chatgptRegisterFlow}
                    onChange={setChatgptRegisterFlow}
                  />
                </Form.Item>
                <Form.Item label="ChatGPT Token 方案">
                  <ChatGPTRegistrationModeSwitch
                    mode={chatgptRegistrationMode}
                    onChange={setChatgptRegistrationMode}
                  />
                </Form.Item>
                <Form.Item label="绑定 2FA">
                  <ChatGPTBind2faSwitch
                    enabled={chatgptBind2fa}
                    onChange={setChatgptBind2fa}
                  />
                </Form.Item>
              </>
            )}
            <Form.Item>
              <Button type="primary" htmlType="submit" block loading={registerLoading}>
                开始注册
              </Button>
            </Form.Item>
          </Form>
        ) : (
          <TaskLogPanel taskId={taskId} onDone={() => { load(); }} />
        )}
      </Modal>

      <Modal
        title="批量补 RT"
        open={backfillRtModalOpen}
        onCancel={closeBackfillRtModal}
        footer={null}
        width={backfillRtTaskId ? 720 : 520}
        maskClosable={false}
      >
        {!backfillRtTaskId ? (
          <>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={
                selectedRowKeys.length > 0
                  ? `处理所选 ${selectedRowKeys.length} 个账号`
                  : `处理当前筛选的 ${total} 个账号（本页缺 RT ${missingRtCount} 个）`
              }
              description="先用库里的会话直接换 refresh_token；会话失效时再用邮箱密码重新登录，可能需要收一封验证码。"
            />
            <Form form={backfillRtForm} layout="vertical" onFinish={handleBackfillRt}>
              <Form.Item
                name="only_missing_rt"
                label="只补缺 RT 的账号"
                initialValue={true}
                valuePropName="checked"
                extra="关掉会给已有 RT 的账号也重新换一次，没必要时别开"
              >
                <Switch />
              </Form.Item>
              <Form.Item
                name="allow_login"
                label="会话失效时用邮箱密码重登"
                initialValue={true}
                valuePropName="checked"
                extra="关掉则只尝试复用会话，快但成功率低"
              >
                <Switch />
              </Form.Item>
              <Form.Item name="concurrency" label="并发数" initialValue={1}>
                <InputNumber min={1} max={10} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item
                name="delay_seconds"
                label="每个账号间隔(秒)"
                initialValue={5}
                extra="连续打授权链容易触发风控，建议留几秒"
              >
                <InputNumber min={0} precision={1} step={1} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item>
                <Button type="primary" htmlType="submit" block loading={backfillRtLoading}>
                  开始补 RT
                </Button>
              </Form.Item>
            </Form>
          </>
        ) : (
          <TaskLogPanel taskId={backfillRtTaskId} kind="backfill_rt" onDone={() => { load() }} />
        )}
      </Modal>

      <Modal
        title="手动新增账号"
        open={addModalOpen}
        onCancel={() => { setAddModalOpen(false); addForm.resetFields(); }}
        onOk={handleAdd}
        okText="确定"
        cancelText="取消"
        maskClosable={false}
      >
        <Form form={addForm} layout="vertical">
          <Form.Item name="email" label="邮箱" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="token" label="Token">
            <Input />
          </Form.Item>
          <Form.Item name="cashier_url" label="试用链接">
            <Input />
          </Form.Item>
          <Form.Item name="status" label="状态" initialValue="registered">
            <Select
              options={[
                { value: 'registered', label: '已注册' },
                { value: 'trial', label: '试用中' },
                { value: 'subscribed', label: '已订阅' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <AccountExportModal
        open={exportModalOpen}
        onClose={() => setExportModalOpen(false)}
        filters={{
          platform: currentPlatform,
          email: search,
          status: filterStatus,
          plus_status: filterPlusStatus,
          sms_verified: filterSmsVerified,
          created_at_start: createdAtStart,
          created_at_end: createdAtEnd,
        }}
        selectedIds={Array.from(selectedRowKeys)
          .map((value) => Number(value))
          .filter((value) => Number.isInteger(value) && value > 0)}
        filteredTotal={total}
      />

      <Modal
        title="批量导入"
        open={importModalOpen}
        onCancel={() => { setImportModalOpen(false); setImportText(''); }}
        onOk={handleImport}
        okText="确定"
        cancelText="取消"
        confirmLoading={importLoading}
        maskClosable={false}
      >
        <p style={{ marginBottom: 8, fontSize: 12, color: 'var(--text-muted)' }}>
          每行格式: <code style={{ background: 'var(--bg-subtle)', padding: '2px 4px', borderRadius: 4 }}>email password [cashier_url]</code>
        </p>
        <Input.TextArea
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
          rows={8}
          style={{ fontFamily: 'monospace' }}
        />
      </Modal>

      <Modal
        title="账号详情"
        open={detailModalOpen}
        onCancel={() => setDetailModalOpen(false)}
        onOk={handleDetailSave}
        okText="保存"
        cancelText="取消"
        maskClosable={false}
        width={760}
        styles={{ body: { maxHeight: '72vh', overflowY: 'auto' } }}
      >
        {currentAccount && (
          <>
            <Form form={detailForm} layout="vertical" initialValues={currentAccount}>
              <Form.Item name="status" label="状态">
                <Select
                  options={[
                    { value: 'registered', label: '已注册' },
                    { value: 'trial', label: '试用中' },
                    { value: 'subscribed', label: '已订阅' },
                    { value: 'expired', label: '已过期' },
                    { value: 'invalid', label: '已失效' },
                    { value: 'deleted', label: '已删除/停用' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="token" label="Access Token">
                <Input.TextArea rows={2} style={{ fontFamily: 'monospace' }} />
              </Form.Item>
            </Form>
            {(() => {
              const rt = getRefreshToken(currentAccount)
              if (!rt) return null
              return (
                <div style={{ marginTop: 8 }}>
                  <div style={{ marginBottom: 4, fontWeight: 500, fontSize: 13 }}>Refresh Token</div>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'flex-start',
                      gap: 8,
                      background: token.colorFillAlter,
                      border: `1px solid ${token.colorBorder}`,
                      borderRadius: token.borderRadius,
                      padding: '8px 10px',
                    }}
                  >
                    <Text
                      style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all', flex: 1, userSelect: 'text' }}
                      copyable={{ text: rt, tooltips: ['复制 RT', '已复制'] }}
                    >
                      {rt}
                    </Text>
                  </div>
                </div>
              )
            })()}
            {currentAccount.totpSecret ? (
              <div style={{ marginTop: 8 }}>
                <div style={{ marginBottom: 4, fontWeight: 500, fontSize: 13 }}>TOTP 2FA 密钥</div>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 8,
                    background: token.colorFillAlter,
                    border: `1px solid ${token.colorBorder}`,
                    borderRadius: token.borderRadius,
                    padding: '8px 10px',
                  }}
                >
                  <Text
                    style={{ fontFamily: 'monospace', fontSize: 12, wordBreak: 'break-all', flex: 1, userSelect: 'text' }}
                    copyable={{ text: currentAccount.totpSecret, tooltips: ['复制密钥', '已复制'] }}
                  >
                    {currentAccount.totpSecret}
                  </Text>
                </div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  在验证器 App 里选「手动输入密钥」，账户名填 {currentAccount.email}。
                </Text>
              </div>
            ) : null}
            {currentPlatform === 'chatgpt' ? (
              <DetailSection title="本地真实状态">
                {currentAccount.chatgptLocal && Object.keys(currentAccount.chatgptLocal).length > 0 ? (
                  <LocalProbeSummary probe={currentAccount.chatgptLocal} />
                ) : (
                  <Text type="secondary">尚未探测。可在操作菜单中点击“探测本地状态”。</Text>
                )}
              </DetailSection>
            ) : null}
            {currentPlatform === 'chatgpt' ? (
              <DetailSection title="CLIProxyAPI 状态">
                {currentAccount.cliproxySync && Object.keys(currentAccount.cliproxySync).length > 0 ? (
                  <CliproxySyncSummary sync={currentAccount.cliproxySync} />
                ) : (
                  <Text type="secondary">尚未同步。可在操作菜单中点击“同步 CLIProxyAPI 状态”。</Text>
                )}
              </DetailSection>
            ) : null}
          </>
        )}
      </Modal>
    </div>
  )
}
