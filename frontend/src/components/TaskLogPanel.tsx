import { useEffect, useRef, useState } from 'react'
import { Button, message, Space, Tag } from 'antd'
import { CopyOutlined, DownloadOutlined, FastForwardOutlined, StopOutlined } from '@ant-design/icons'

import { API_BASE, apiFetch, getToken } from '@/lib/utils'

interface TaskLogPanelProps {
  taskId: string
  onDone?: () => void
  /** 任务类型只影响文案，日志流对所有后台任务都是同一套 */
  kind?: TaskKind
  operation?: 'link' | 'pay'
}

export type TaskKind = 'register' | 'backfill_rt' | 'bind_2fa' | 'payment' | 'inspection' | 'sms_stock' | 'chatgpt_web_pool' | 'chatgpt_web_pool_register'

const KIND_TEXT: Record<TaskKind, { success: string; registered: string; total: string; done: string }> = {
  register: { success: '注册成功', registered: '已注册', total: '总共注册', done: '注册完成' },
  backfill_rt: { success: '补 RT 成功', registered: '已处理', total: '总共账号', done: '补 RT 完成' },
  bind_2fa: { success: '绑定成功', registered: '已处理', total: '总共账号', done: '绑定 2FA 完成' },
  payment: { success: '支付成功', registered: '已处理', total: '总共账号', done: '支付任务完成' },
  inspection: { success: '巡检成功', registered: '已处理', total: '总共账号', done: '巡检完成' },
  sms_stock: { success: '接码账号补充成功', registered: '已尝试', total: '本轮尝试上限', done: '接码补库结束' },
  chatgpt_web_pool: { success: 'Web 池补充成功', registered: '已尝试', total: '本轮注册尝试上限', done: 'Web 池巡检结束（以远端记录为准）' },
  chatgpt_web_pool_register: { success: '邮箱注册成功', registered: '已尝试', total: '总共注册', done: 'Web 池邮箱注册子任务结束' },
}

type TaskTerminalStatus = 'idle' | 'done' | 'failed' | 'stopped'

interface RegisterSummary {
  success: number
  registered: number
  total: number
}

interface LogPayload extends Partial<RegisterSummary> {
  logs?: string[]
  line?: string
  sequence?: number
  log_offset?: number
  next_since?: number
  log_count?: number
  reset?: boolean
  done?: boolean
  status?: string
  control?: { stop_requested?: boolean }
}

interface LogWindow {
  lines: string[]
  offset: number
  count: number
}

const MAX_VISIBLE_LINES = 2000
const LOG_PAGE_SIZE = 500
const LOG_BATCH_MS = 100
const MAX_STREAM_RETRIES = 5

function cursorValue(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : undefined
}

function isTerminalStatus(status: unknown): status is Exclude<TaskTerminalStatus, 'idle'> {
  return status === 'done' || status === 'failed' || status === 'stopped'
}

function parseCounter(value: unknown): number {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n < 0) return 0
  return Math.floor(n)
}

function normalizeSummary(next: RegisterSummary): RegisterSummary {
  const success = parseCounter(next.success)
  const registered = Math.max(parseCounter(next.registered), success)
  const total = Math.max(parseCounter(next.total), registered)
  return { success, registered, total }
}

function lineToneClass(line: string): string {
  if (line.includes('✓') || line.includes('成功')) return 'log-line--success'
  if (line.includes('✗') || line.includes('失败') || line.includes('错误')) return 'log-line--danger'
  if (line.includes('停止') || line.includes('跳过')) return 'log-line--warning'
  return ''
}

function mergeSummary(previous: RegisterSummary, incoming: Partial<RegisterSummary>): RegisterSummary {
  return normalizeSummary({
    success: incoming.success ?? previous.success,
    registered: incoming.registered ?? previous.registered,
    total: incoming.total ?? previous.total,
  })
}

export function TaskLogPanel(props: TaskLogPanelProps) {
  return <TaskLogContent key={props.taskId} {...props} />
}

function TaskLogContent({ taskId, onDone, kind = 'register', operation }: TaskLogPanelProps) {
  const text = kind === 'payment'
    ? (operation === 'link'
      ? { success: '提链成功', registered: '已处理', total: '总共账号', done: '提链任务完成' }
      : operation === 'pay'
        ? { success: '支付成功', registered: '已处理', total: '总共账号', done: '支付任务完成' }
        : { success: '操作成功', registered: '已处理', total: '总共账号', done: '支付任务完成' })
    : (KIND_TEXT[kind] || KIND_TEXT.register)
  const [logWindow, setLogWindow] = useState<LogWindow>({ lines: [], offset: 0, count: 0 })
  const { lines } = logWindow
  const [summary, setSummary] = useState<RegisterSummary>({ success: 0, registered: 0, total: 0 })
  const [error, setError] = useState('')
  const [terminalStatus, setTerminalStatus] = useState<TaskTerminalStatus>('idle')
  const [skipLoading, setSkipLoading] = useState(false)
  const [stopLoading, setStopLoading] = useState(false)
  const [downloadLoading, setDownloadLoading] = useState(false)
  const [stopRequested, setStopRequested] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)
  const onDoneRef = useRef(onDone)
  const nextSinceRef = useRef(0)
  const lifecycleRef = useRef<AbortController | null>(null)
  const downloadRef = useRef<AbortController | null>(null)

  const isFinished = terminalStatus !== 'idle' || stopRequested

  const handleCopyAll = async () => {
    const signal = lifecycleRef.current?.signal
    if (!signal || signal.aborted) return
    try {
      await navigator.clipboard.writeText(lines.join('\n'))
      if (!signal.aborted) message.success('当前显示日志已复制')
    } catch {
      if (!signal.aborted) message.error('复制失败')
    }
  }

  const handleDownload = async () => {
    const lifecycle = lifecycleRef.current?.signal
    if (!lifecycle || lifecycle.aborted || downloadRef.current) return
    const controller = new AbortController()
    downloadRef.current = controller
    setDownloadLoading(true)
    try {
      // Freeze the target at the first page, so a running task cannot make this loop infinite.
      let targetCount: number | undefined
      let since = 0
      const parts: BlobPart[] = []
      do {
        const page = await apiFetch(`/tasks/${taskId}/logs?since=${since}&limit=${LOG_PAGE_SIZE}`, {
          signal: controller.signal,
        }) as LogPayload
        if (controller.signal.aborted || lifecycle.aborted) return
        const pageLines = Array.isArray(page.logs) ? page.logs : []
        const offset = cursorValue(page.log_offset) ?? since
        if (offset > since) throw new Error('历史日志存在缺口，请稍后重新下载')
        const start = Math.max(0, since - offset)
        const next = cursorValue(page.next_since) ?? offset + pageLines.length
        targetCount ??= cursorValue(page.log_count) ?? next
        const chunk = pageLines.slice(start, start + Math.max(0, targetCount - since))
        if (chunk.length) parts.push(chunk.join('\n') + '\n')
        if (next <= since || !pageLines.length) {
          if (since < targetCount) throw new Error('完整日志尚未就绪，请稍后重试')
          break
        }
        since = next
      } while (since < targetCount)
      if (controller.signal.aborted || lifecycle.aborted) return
      const url = URL.createObjectURL(new Blob(parts, { type: 'text/plain;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `${taskId.replace(/[^a-zA-Z0-9_-]/g, '_')}-logs.txt`
      document.body.appendChild(link)
      try {
        link.click()
      } finally {
        link.remove()
        URL.revokeObjectURL(url)
      }
      message.success('完整日志已下载（截至开始下载时）')
    } catch (error_: unknown) {
      if (!controller.signal.aborted && !lifecycle.aborted) {
        message.error(error_ instanceof Error ? error_.message : '下载日志失败')
      }
    } finally {
      if (downloadRef.current === controller) downloadRef.current = null
      if (!controller.signal.aborted && !lifecycle.aborted) setDownloadLoading(false)
    }
  }

  const handleSkipCurrent = async () => {
    const signal = lifecycleRef.current?.signal
    if (isFinished || !signal || signal.aborted) return
    setSkipLoading(true)
    try {
      const response = await apiFetch(`/tasks/${taskId}/skip-current`, { method: 'POST', signal }) as {
        control?: { targeted_skip_attempts?: number }
      }
      if (signal.aborted) return
      const targeted = Number(response.control?.targeted_skip_attempts || 0)
      message.success(
        targeted > 1
          ? `已发送跳过 ${targeted} 个进行中账号请求`
          : '已发送跳过当前账号请求',
      )
    } catch (error_: unknown) {
      if (!signal.aborted) message.error(error_ instanceof Error ? error_.message : '请求失败')
    } finally {
      if (!signal.aborted) setSkipLoading(false)
    }
  }

  const handleStopTask = async () => {
    const signal = lifecycleRef.current?.signal
    if (isFinished || !signal || signal.aborted) return
    setStopLoading(true)
    try {
      await apiFetch(`/tasks/${taskId}/stop`, { method: 'POST', signal })
      if (signal.aborted) return
      setStopRequested(true)
      message.success('已发送停止任务请求，正在停止进行中的线程')
    } catch (error_: unknown) {
      if (!signal.aborted) message.error(error_ instanceof Error ? error_.message : '请求失败')
    } finally {
      if (!signal.aborted) setStopLoading(false)
    }
  }

  useEffect(() => {
    onDoneRef.current = onDone
  }, [onDone])

  useEffect(() => {
    if (!taskId) return
    const controller = new AbortController()
    lifecycleRef.current = controller
    let doneNotified = false
    let retryTimer: number | undefined
    let resolveRetry: (() => void) | undefined
    let batchTimer: number | undefined
    let visibleLines: string[] = []
    let visibleOffset = 0
    let logCount = 0
    let currentSummary: RegisterSummary = { success: 0, registered: 0, total: 0 }
    let linesDirty = false
    let summaryDirty = false
    nextSinceRef.current = 0

    const flush = () => {
      window.clearTimeout(batchTimer)
      batchTimer = undefined
      if (controller.signal.aborted) return
      if (linesDirty) {
        setLogWindow({ lines: visibleLines.slice(), offset: visibleOffset, count: logCount })
        linesDirty = false
      }
      if (summaryDirty) {
        setSummary(currentSummary)
        summaryDirty = false
      }
    }
    const scheduleFlush = () => {
      if (batchTimer === undefined) batchTimer = window.setTimeout(flush, LOG_BATCH_MS)
    }
    const updateCounters = (payload: LogPayload) => {
      const next = mergeSummary(currentSummary, payload)
      if (next.success !== currentSummary.success || next.registered !== currentSummary.registered || next.total !== currentSummary.total) {
        currentSummary = next
        summaryDirty = true
      }
      const count = Math.max(logCount, cursorValue(payload.log_count) ?? 0, nextSinceRef.current)
      if (count !== logCount) {
        logCount = count
        linesDirty = true
      }
    }
    const finish = (status: string | undefined) => {
      if (controller.signal.aborted || doneNotified) return
      flush()
      setTerminalStatus(isTerminalStatus(status) ? status : 'done')
      setError('')
      doneNotified = true
      onDoneRef.current?.()
    }

    const initSnapshot = async (): Promise<boolean> => {
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`, { signal: controller.signal }) as LogPayload
        if (controller.signal.aborted) return true
        const snapshotLines = Array.isArray(snapshot.logs) ? snapshot.logs : []
        const offset = cursorValue(snapshot.log_offset) ?? 0
        // A tail snapshot does not start at zero; log_count may already be ahead of that snapshot.
        nextSinceRef.current = Math.max(offset + snapshotLines.length, cursorValue(snapshot.next_since) ?? 0)
        visibleLines = snapshotLines.slice(-MAX_VISIBLE_LINES)
        visibleOffset = offset + snapshotLines.length - visibleLines.length
        logCount = Math.max(nextSinceRef.current, cursorValue(snapshot.log_count) ?? 0)
        linesDirty = true
        updateCounters(snapshot)
        setStopRequested(Boolean(snapshot.control?.stop_requested))
        flush()
        if (isTerminalStatus(snapshot.status)) {
          finish(snapshot.status)
          return true
        }
      } catch (error_: unknown) {
        if (controller.signal.aborted) return true
        setError(error_ instanceof Error ? error_.message : '获取任务快照失败')
      }
      return false
    }

    const connectStreamOnce = async (): Promise<boolean> => {
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
      try {
        const token = getToken()
        const headers: Record<string, string> = {}
        if (token) headers.Authorization = `Bearer ${token}`
        const response = await fetch(`${API_BASE}/tasks/${taskId}/logs/stream?since=${nextSinceRef.current}`, {
          headers,
          signal: controller.signal,
        })
        if (controller.signal.aborted) return true
        if (!response.ok) {
          await response.body?.cancel()
          if (controller.signal.aborted) return true
          setError(`日志流连接失败 (${response.status})`)
          return response.status >= 400 && response.status < 500 && response.status !== 429
        }
        if (!response.body) return false
        setError('')
        reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        const consumeEvent = (part: string): boolean => {
          const event = part.match(/^event:\s*(\S+)/m)?.[1]
          const data = part.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
          if (!data) return false
          let payload: LogPayload
          try {
            payload = JSON.parse(data) as LogPayload
          } catch {
            return false
          }
          if (!payload || typeof payload !== 'object') return false
          if (payload.reset || event === 'reset') {
            const next = cursorValue(payload.next_since) ?? cursorValue(payload.log_offset) ?? nextSinceRef.current
            // Tail eviction changes the absolute cursor; never keep old lines adjacent to a gap.
            nextSinceRef.current = next
            visibleLines = []
            visibleOffset = next
            linesDirty = true
          }
          if (typeof payload.line === 'string') {
            const sequence = cursorValue(payload.sequence)
            const next = cursorValue(payload.next_since) ?? (sequence !== undefined ? sequence + 1 : nextSinceRef.current + 1)
            if (next > nextSinceRef.current) {
              const absoluteIndex = sequence ?? next - 1
              if (absoluteIndex !== nextSinceRef.current) {
                visibleLines = []
                visibleOffset = absoluteIndex
              } else if (visibleLines.length === 0) {
                visibleOffset = absoluteIndex
              }
              visibleLines.push(payload.line)
              nextSinceRef.current = next
              linesDirty = true
            }
          }
          updateCounters(payload)
          if (payload.done || event === 'done') {
            trimWindow()
            finish(payload.status)
            return true
          }
          return false
        }
        const trimWindow = () => {
          if (visibleLines.length > MAX_VISIBLE_LINES) {
            const dropped = visibleLines.length - MAX_VISIBLE_LINES
            visibleLines = visibleLines.slice(dropped)
            visibleOffset += dropped
          }
        }

        while (!controller.signal.aborted) {
          const { done, value } = await reader.read()
          if (controller.signal.aborted) return true
          buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
          buffer = buffer.replace(/\r\n/g, '\n')
          const parts = buffer.split('\n\n')
          buffer = parts.pop() || ''
          for (const part of parts) {
            if (consumeEvent(part)) return true
          }
          if (done && buffer.trim() && consumeEvent(buffer)) return true
          trimWindow()
          if (linesDirty || summaryDirty) scheduleFlush()
          if (done) break
        }
        return controller.signal.aborted
      } catch {
        return controller.signal.aborted
      } finally {
        if (reader) {
          try { await reader.cancel() } catch { /* Abort may have already closed it. */ }
          reader.releaseLock()
        }
        if (!controller.signal.aborted) flush()
      }
    }

    const waitForRetry = (ms: number) => new Promise<void>(resolve => {
      resolveRetry = resolve
      retryTimer = window.setTimeout(() => {
        retryTimer = undefined
        resolveRetry = undefined
        resolve()
      }, ms)
    })
    const connectStream = async () => {
      if (await initSnapshot() || controller.signal.aborted) return
      for (let retries = 0; !controller.signal.aborted; retries += 1) {
        if (await connectStreamOnce() || controller.signal.aborted) return
        if (retries >= MAX_STREAM_RETRIES) {
          setError('日志流多次中断，已停止自动重试；请关闭后重新打开日志。完整日志仍可下载。')
          return
        }
        const retryMs = Math.min(1000 * 2 ** retries, 8000)
        setError(`日志流连接中断，${retryMs / 1000}s 后重试（${retries + 1}/${MAX_STREAM_RETRIES}）`)
        await waitForRetry(retryMs)
      }
    }
    void connectStream()

    return () => {
      controller.abort()
      window.clearTimeout(batchTimer)
      window.clearTimeout(retryTimer)
      resolveRetry?.()
      downloadRef.current?.abort()
      downloadRef.current = null
      if (lifecycleRef.current === controller) lifecycleRef.current = null
    }
  }, [taskId])

  useEffect(() => {
    if (!panelRef.current) return
    panelRef.current.scrollTop = panelRef.current.scrollHeight
  }, [lines])

  const footerText =
    terminalStatus === 'done'
      ? { text: text.done, color: 'var(--success)' }
      : terminalStatus === 'stopped'
        ? { text: '任务已停止', color: 'var(--warning)' }
        : terminalStatus === 'failed'
          ? { text: '任务失败', color: 'var(--danger)' }
          : null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Space wrap style={{ marginBottom: 8 }}>
        <Tag className="log-stat log-stat--success" bordered={false}>{text.success}：{summary.success}</Tag>
        <Tag className="log-stat log-stat--info" bordered={false}>{text.registered}：{summary.registered}</Tag>
        <Tag className="log-stat" bordered={false}>{text.total}：{summary.total}</Tag>
      </Space>

      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <Space>
          <Button
            size="small"
            icon={<FastForwardOutlined />}
            onClick={handleSkipCurrent}
            loading={skipLoading}
            disabled={isFinished}
          >
            跳过当前账号
          </Button>
          <Button
            size="small"
            danger
            icon={<StopOutlined />}
            onClick={handleStopTask}
            loading={stopLoading}
            disabled={isFinished}
          >
            停止任务
          </Button>
        </Space>
        <Space wrap>
          <Button size="small" icon={<CopyOutlined />} onClick={handleCopyAll} disabled={lines.length === 0}>
            复制当前显示
          </Button>
          <Button size="small" icon={<DownloadOutlined />} onClick={handleDownload} loading={downloadLoading}>
            下载完整日志
          </Button>
        </Space>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8 }}>
        仅显示最近 {MAX_VISIBLE_LINES} 行，当前 {lines.length} / {logWindow.count} 行；完整历史请点击「下载完整日志」。
      </div>

      <div
        ref={panelRef}
        className="log-panel"
        style={{
          flex: 1,
          overflowY: 'auto',
          overflowX: 'hidden',
          borderRadius: 10,
          padding: '12px 14px',
          fontFamily: '"SFMono-Regular", Menlo, Consolas, monospace',
          fontSize: 12,
          minHeight: 320,
          maxHeight: '65vh',
          userSelect: 'text',
          WebkitUserSelect: 'text',
          cursor: 'text',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {lines.length === 0 && !error && <div className="log-placeholder">等待日志...</div>}
        {error && <div className="log-error">{error}</div>}
        {lines.map((line, index) => (
          <div key={logWindow.offset + index} className={`log-line ${lineToneClass(line)}`}>
            {line}
          </div>
        ))}
      </div>

      {footerText ? (
        <div style={{ fontSize: 12, color: footerText.color, marginTop: 8 }}>
          {footerText.text}
        </div>
      ) : null}
    </div>
  )
}

export default TaskLogPanel
