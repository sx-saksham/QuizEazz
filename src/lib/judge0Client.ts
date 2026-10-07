import type { SupabaseClient } from '@supabase/supabase-js'
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

const TIMEOUT_MS = 45_000

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
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)

  try {
    const { data, error } = await supabase.functions.invoke(EDGE_FUNCTION, {
      body: { attempt_id: attemptId },
      signal: controller.signal,
    })

    if (error) {
      return {
        attempt_id: attemptId,
        ok: false,
        error: toErrorMessage(error, 'The grading service could not be reached.'),
      }
    }

    const payload = (data ?? {}) as Judge0GradeSummary
    return { ...payload, attempt_id: payload.attempt_id ?? attemptId }
  } catch (err) {
    return {
      attempt_id: attemptId,
      ok: false,
      error: toErrorMessage(err, 'The grading service could not be reached.'),
    }
  } finally {
    clearTimeout(timer)
  }
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
