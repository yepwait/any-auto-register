import { useCallback, useEffect, useRef, useState } from 'react'

interface PollingRequestOptions<T> {
  request: (signal: AbortSignal) => Promise<T>
  /** Runs once before status-only polling; independent initial requests can use Promise.all. */
  initialRequest?: (signal: AbortSignal) => Promise<T>
  onSuccess: (result: T) => void
  onError?: (error: unknown) => void
  intervalMs?: number
}

/** Deduplicates refreshes, schedules after completion, and cancels work on unmount. */
export function usePollingRequest<T>({
  request,
  initialRequest,
  onSuccess,
  onError,
  intervalMs,
}: PollingRequestOptions<T>) {
  const [loading, setLoading] = useState(true)
  const callbacksRef = useRef({ onSuccess, onError })
  const refreshRef = useRef<(() => Promise<void>) | null>(null)
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    callbacksRef.current = { onSuccess, onError }
  }, [onSuccess, onError])

  const refresh = useCallback(() => refreshRef.current?.() ?? Promise.resolve(), [])
  const getSignal = useCallback(() => controllerRef.current?.signal, [])

  // Action requests share component lifetime, not the current page/filter request lifetime.
  useEffect(() => {
    const controller = new AbortController()
    controllerRef.current = controller
    return () => {
      controller.abort()
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined
    let inFlight: Promise<void> | null = null
    let initialized = !initialRequest

    const schedule = () => {
      window.clearTimeout(timer)
      if (controller.signal.aborted || !intervalMs || document.hidden) return
      timer = window.setTimeout(() => void execute(), intervalMs)
    }

    const execute = (): Promise<void> => {
      if (controller.signal.aborted) return Promise.resolve()
      if (inFlight) return inFlight
      window.clearTimeout(timer)
      setLoading(true)
      const pending = (async () => {
        const attempt = new AbortController()
        const abortAttempt = () => attempt.abort()
        controller.signal.addEventListener('abort', abortAttempt, { once: true })
        try {
          const result = await (!initialized && initialRequest ? initialRequest : request)(attempt.signal)
          if (controller.signal.aborted) return
          initialized = true
          callbacksRef.current.onSuccess(result)
        } catch (error) {
          if (!controller.signal.aborted) callbacksRef.current.onError?.(error)
        } finally {
          // If one parallel initial request failed, cancel any remaining sibling request.
          attempt.abort()
          controller.signal.removeEventListener('abort', abortAttempt)
          inFlight = null
          if (!controller.signal.aborted) {
            setLoading(false)
            schedule()
          }
        }
      })()
      inFlight = pending
      return pending
    }

    refreshRef.current = execute
    const onVisibilityChange = () => {
      window.clearTimeout(timer)
      if (!document.hidden) void execute()
    }
    if (intervalMs) document.addEventListener('visibilitychange', onVisibilityChange)
    void execute()

    return () => {
      controller.abort()
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
      if (refreshRef.current === execute) refreshRef.current = null
    }
  }, [request, initialRequest, intervalMs])

  return { loading, refresh, getSignal }
}
