import type { SupabaseClient } from '@supabase/supabase-js'
import {
  FunctionsFetchError,
  FunctionsHttpError,
  FunctionsRelayError,
} from '@supabase/supabase-js'
import { toErrorMessage } from './supabase'

/**
 * Triggers Judge0 grading for a finalized attempt.
 *
 * ===================================================================
 * THE TOKEN IS NOT HERE, AND MUST NEVER BE HERE.
 * ===================================================================
 * This module runs in the browser. It calls the Supabase Edge Function
 * `judge0-grade`, which holds `JUDGE0_API_TOKEN` in its own encrypted
 * server-side environment. The browser sends only the attempt id and its
 * Supabase session JWT. No Judge0 URL, token or submission payload is ever
 * present in the client bundle or in any network response.
 *
 * There is deliberately no Run / Compile / Test control anywhere in QuizEazz.
 * Programming code is executed only after the attempt has been finalized by
 * the server, and only by Judge0's sandbox.
 */

/** Outcomes the server may write. Mirrors the database allow-list. */
export type ProgrammingOutcome =
  | 'Correct'
  | 'Wrong Answer'
  | 'Compilation Error'
  | 'Runtime Error'
  | 'Time Limit Exceeded'
  | 'Memory Limit Exceeded'
  | 'System Error'
  | 'Not Attempted'
  | 'Pending Evaluation'

export interface Judge0GradeSummary {
  attempt_id: string
  ok: boolean
  graded?: number
  results?: Array<{
    answer_id: string
    outcome?: ProgrammingOutcome | null
    status_id?: number | null
    pending?: boolean
    error?: string
    applied?: boolean
  }>
  note?: string
  error?: string
}

/** Name of the Edge Function. Nothing secret is encoded in it. */
const EDGE_FUNCTION = 'judge0-grade'

/**
 * Total wall-clock budget for the whole grading trigger, including retries.
 *
 * It is deliberately the SAME 45s budget the trigger has always had, so the
 * student's UI is never blocked for longer than before. Retries only spend
 * time that was already available; they do not extend it.
 */
const TIMEOUT_MS = 45_000

/** Total invocation attempts (1 initial + up to 2 retries). */
const MAX_ATTEMPTS = 3

/** Base backoff. Attempt N waits BASE_RETRY_DELAY_MS * 2 ** N. */
const BASE_RETRY_DELAY_MS = 1_000

/**
 * Classifies a failed `functions.invoke` call as TRANSIENT (worth retrying)
 * or TERMINAL (retrying cannot help).
 *
 * This uses the typed error classes the installed Supabase JS SDK
 * exposes — never message-string matching:
 *   - FunctionsFetchError  → the request never reached the function (DNS,
 *                            offline, connection reset, CORS preflight).
 *   - FunctionsRelayError  → the Supabase relay could not reach the function
 *                            (gateway/cold-start/routing failure).
 *   - FunctionsHttpError   → the function answered. 5xx, 408 and 429 are
 *                            transient infrastructure conditions; every other
 *                            4xx (400/401/403/404) is a decision the function
 *                            already made and must not be retried.
 *
 * An application-level failure (`ok: false` inside a 200 response) is
 * TERMINAL: the function ran, reported a real grading result, and repeating
 * it would only produce the same answer.
 */
function isTransientInvocationError(error: unknown): boolean {
  if (error instanceof FunctionsFetchError) return true
  if (error instanceof FunctionsRelayError) return true
  if (error instanceof FunctionsHttpError) {
    const status = (error.context as { status?: number } | undefined)?.status
    if (typeof status !== 'number') return true // unknown status: treat as transient
    if (status >= 500) return true // 500, 502, 503, 504, gateway errors
    return status === 408 || status === 429
  }
  return false
}

/** Abortable sleep that never rejects and never leaves a live timer behind. */
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms)
  })
}

/**
 * Asks the server to run every pending programming answer of a finalized
 * attempt through Judge0.
 *
 * Failure is deliberately non-fatal and non-destructive: if the function is
 * unreachable, or Judge0 is down, this resolves `ok: false` and the answers
 * simply stay 'Pending Evaluation'. They are NEVER marked Wrong Answer.
 */
export async function requestProgrammingGrading(
  supabase: SupabaseClient,
  attemptId: string,
): Promise<Judge0GradeSummary> {
  // One overall deadline shared by every attempt, so retries can never push the
  // student's navigation further out than a single attempt ever did.
  const deadline = Date.now() + TIMEOUT_MS
  let lastError = 'The grading service could not be reached.'

  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    const remaining = deadline - Date.now()
    if (remaining <= 0) break

    // A fresh controller per attempt, cleared in `finally` on every path so no
    // timer outlives its request and no aborted body is reused.
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), remaining)

    try {
      const { data, error } = await supabase.functions.invoke(EDGE_FUNCTION, {
        body: { attempt_id: attemptId },
        signal: controller.signal,
      })

      if (!error) {
        const payload = (data ?? {}) as Judge0GradeSummary
        // Success: never retry. The function ran and reported its real result.
        return { ...payload, attempt_id: payload.attempt_id ?? attemptId }
      }

      // The function answered with a non-2xx status.
      lastError = toErrorMessage(error, 'The grading service could not be reached.')
      if (!isTransientInvocationError(error)) break // terminal: retrying cannot help
    } catch (err) {
      // A thrown transport failure (including our own abort on timeout).
      lastError = toErrorMessage(err, 'The grading service could not be reached.')
      if (!isTransientInvocationError(err)) break
    } finally {
      clearTimeout(timer)
    }

    // Back off before the next attempt, but never past the shared deadline.
    const backoff = Math.min(BASE_RETRY_DELAY_MS * 2 ** attempt, deadline - Date.now())
    if (backoff <= 0) break
    await delay(backoff)
  }

  // Every attempt failed (or ran out of budget). This is deliberately
  // non-destructive: the answers stay 'Pending Evaluation' and the existing
  // server-side sweep recovers them. Never recorded as a verdict.
  return { attempt_id: attemptId, ok: false, error: lastError }
}

/**
 * Asks the server to retry every attempt that still has a pending
 * programming answer. Used to recover after a Judge0 outage.
 *
 * This is an administrative retry. It still runs entirely server-side and
 * still executes only finalized attempts.
 */
export async function requestGradingSweep(
  supabase: SupabaseClient,
): Promise<{ swept: number; results: Judge0GradeSummary[] } | { error: string }> {
  try {
    const { data, error } = await supabase.functions.invoke(EDGE_FUNCTION, {
      body: { sweep: true },
    })
    if (error) return { error: toErrorMessage(error, 'The sweep could not be started.') }
    return data as { swept: number; results: Judge0GradeSummary[] }
  } catch (err) {
    return { error: toErrorMessage(err, 'The sweep could not be started.') }
  }
}
