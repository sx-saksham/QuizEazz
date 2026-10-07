import { useCallback, useEffect, useRef, useState } from 'react'
import { toErrorMessage } from '../lib/supabase'

export interface AsyncData<T> {
  data: T
  error: string | null
  loading: boolean
  /** Re-runs the fetcher. Safe to call from a click handler. */
  reload: () => void
}

/**
 * Loads data from an authorized RPC on mount and exposes an explicit reload.
 *
 * Every screen in QuizEazz reads from a SECURITY DEFINER function, so this is
 * the single place where that call happens.
 *
 * Two invariants:
 *  - state is only written from an async continuation, never synchronously
 *    during render, so a load never cascades extra renders
 *  - a response is discarded if a newer request or an unmount superseded it,
 *    so a slow reply cannot overwrite fresher state
 *
 * @param fetcher must be stable (wrap it in useCallback at the call site)
 * @param initial value exposed before the first response
 */
export function useAsyncData<T>(
  fetcher: () => Promise<T>,
  initial: T,
  options: { errorFallback?: string } = {},
): AsyncData<T> {
  const { errorFallback = 'Unable to load data.' } = options

  const [data, setData] = useState<T>(initial)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState<boolean>(true)

  const requestId = useRef(0)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const run = useCallback(async () => {
    const id = ++requestId.current

    try {
      const result = await fetcher()
      if (!mounted.current || id !== requestId.current) return
      setData(result)
      setError(null)
    } catch (err) {
      if (!mounted.current || id !== requestId.current) return
      setError(toErrorMessage(err, errorFallback))
    } finally {
      if (mounted.current && id === requestId.current) setLoading(false)
    }
  }, [fetcher, errorFallback])

  // Fetching an authorized RPC on mount is exactly what an effect is for.
  // run() writes state only from its own async continuation, never
  // synchronously, so this does not cascade renders.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void run()
  }, [run])

  const reload = useCallback(() => {
    void run()
  }, [run])

  return { data, error, loading, reload }
}
