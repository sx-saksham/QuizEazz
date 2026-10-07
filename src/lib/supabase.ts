import { createClient } from '@supabase/supabase-js'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string | undefined
const supabasePublishableKey = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY as
  | string
  | undefined

/**
 * Fail loudly and immediately when the deployment is misconfigured.
 * A silently-undefined client would surface later as a confusing
 * "Failed to fetch" inside an examination.
 */
if (!supabaseUrl || !supabasePublishableKey) {
  throw new Error(
    'QuizEazz is not configured. Set VITE_SUPABASE_URL and ' +
      'VITE_SUPABASE_PUBLISHABLE_KEY in your environment before starting the app.',
  )
}

export const supabase = createClient(supabaseUrl, supabasePublishableKey, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: false,
  },
})

/**
 * Normalizes a PostgREST / RPC failure into a single readable string.
 * Errors are never swallowed: every caller surfaces them to the operator.
 */
export function rpcErrorMessage(
  error: { message?: string; details?: string | null } | null,
  fallback: string,
): string {
  if (!error) return fallback
  const message = error.message?.trim()
  const details = typeof error.details === 'string' ? error.details.trim() : ''
  if (message && details && message !== details) return `${message} (${details})`
  return message || details || fallback
}

/** Turns an unknown thrown value into a readable message. */
export function toErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message
  if (typeof err === 'string' && err.trim()) return err
  if (err && typeof err === 'object' && 'message' in err) {
    const m = (err as { message?: unknown }).message
    if (typeof m === 'string' && m.trim()) return m
  }
  return fallback
}
