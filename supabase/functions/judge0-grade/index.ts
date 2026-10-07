/**
 * QuizEazz — Judge0 grading function (Supabase Edge Function).
 *
 * ===================================================================
 * WHY THIS EXISTS
 * ===================================================================
 * The Judge0 API token must never reach a browser. QuizEazz has exactly one
 * server-side component that can hold a secret: this Edge Function. It reads
 * the token from the Edge Function environment, which is encrypted at rest
 * and is never bundled, never embedded in JavaScript, and never returned in a
 * response. It is NOT a VITE_* variable and is NOT in Git.
 *
 * This function performs no grading decisions of its own. It submits code to
 * Judge0, polls for the result, maps the objective status onto QuizEazz's
 * vocabulary, and hands the verdict to a SECURITY DEFINER RPC. The database
 * computes marks.
 *
 * It is never called while an exam is in progress. It only ever acts on
 * attempts whose status is already finalized (submitted / timed_out /
 * evaluating / evaluated), and the RPC enforces that again.
 * ===================================================================
 */

// npm: specifier, per Supabase's documented Edge Functions form. The
// jsr: specifier is Deno-native but the Edge Runtime does not reliably
// resolve it, which would fail the deploy.
import { createClient } from 'npm:@supabase/supabase-js@2'
import {
  SUBMISSION_LIMITS,
  buildSubmissionBody,
  mapJudge0Result,
  pollUntilVerdict,
  submitProgram,
  type Judge0Outcome,
} from './judge0.ts'

const POLL_ATTEMPTS = 20
const POLL_DELAY_MS = 1500

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
}

/** Server-side configuration. The token exists only in the Edge Function env. */
function getConfig(): { baseUrl: string; token: string } {
  const baseUrl = Deno.env.get('JUDGE0_API_URL')
  const token = Deno.env.get('JUDGE0_API_TOKEN')
  if (!baseUrl || !token) {
    // Fail closed. Never fall back to a default token or an unauthenticated
    // call, and never echo the value in an error message.
    throw new Error('Judge0 is not configured (JUDGE0_API_URL / JUDGE0_API_TOKEN missing)')
  }
  return { baseUrl: baseUrl.replace(/\/+$/, ''), token }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, 'Content-Type': 'application/json' },
  })
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: CORS })

  // The token is never echoed, not even on an internal error.
  try {
    if (req.method !== 'POST') return json({ error: 'method not allowed' }, 405)

    const cfg = getConfig()

    // A service-role key authorises unattended retries/sweeps. A user JWT is
    // accepted only for grading the caller's own finalized attempt.
    const authHeader = req.headers.get('Authorization') ?? ''
    const bearer = authHeader.replace(/^Bearer\s+/i, '').trim()
    if (!bearer) return json({ error: 'missing authorization' }, 401)

    const serviceRoleKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    const isServiceRole = !!serviceRoleKey && bearer === serviceRoleKey

    // The CALLER's client. It carries the student's own JWT, so auth.uid()
    // resolves inside SQL. It is used for exactly two things: resolving the
    // caller, and the ownership check below. It must NOT be used for the
    // grading RPCs.
    const caller = createClient(
      Deno.env.get('SUPABASE_URL') as string,
      bearer,
      { auth: { persistSession: false, autoRefreshToken: false } },
    )

    // The SERVICE-ROLE client. Migration 7 REVOKEs every Judge0 grading RPC
    // from `authenticated`, so those calls must be made with the service role
    // or they fail with a permission error and nothing is ever graded. This
    // key is read only here, is never logged, and never leaves the function.
    // Reaching it is safe because the ownership check above has already
    // proved this caller owns the attempt, and the SQL re-binds each answer to
    // its attempt.
    if (!serviceRoleKey) {
      // Fail closed, exactly like getConfig(). Never fall back to a default.
      throw new Error('SUPABASE_SERVICE_ROLE_KEY is not configured')
    }
    const admin = createClient(
      Deno.env.get('SUPABASE_URL') as string,
      serviceRoleKey,
      { auth: { persistSession: false, autoRefreshToken: false } },
    )

    let payload: { attempt_id?: string; sweep?: boolean }
    try {
      payload = await req.json()
    } catch {
      return json({ error: 'invalid json body' }, 400)
    }

    // ---------------------------------------------------------------
    // Sweep mode: retry everything still pending. Used when Judge0 was
    // unavailable at submission time. Safe to call repeatedly.
    // ---------------------------------------------------------------
    if (payload.sweep === true) {
      if (!isServiceRole) return json({ error: 'sweep requires service role' }, 403)
      const { data: attemptIds, error: listErr } = await admin.rpc(
        'quizeazz_list_attempts_needing_programming_grading',
      )
      if (listErr) return json({ error: `sweep failed: ${listErr.message}` }, 500)
      const ids = (attemptIds ?? []) as string[]
      const results = await Promise.all(
        ids.map((id) => gradeAttempt(cfg, admin, id)),
      )
      return json({ swept: ids.length, results })
    }

    const attemptId = payload.attempt_id
    if (!attemptId || typeof attemptId !== 'string') {
      return json({ error: 'attempt_id is required' }, 400)
    }

    // Ownership check for a student caller, done here AND again in SQL.
    //
    // This deliberately stays on `caller`, not `admin`. The RPC resolves the
    // owner via auth.uid(), which is only meaningful when the request carries
    // the student's own JWT. Grading below uses `admin`, so this check is the
    // gate that stops one student grading another's attempt: it must pass
    // BEFORE `admin` is ever used, and the SQL independently re-binds every
    // answer to the attempt.
    if (!isServiceRole) {
      // Pass the request's bearer JWT explicitly. This client is built with
      // persistSession: false and has no session in storage, so a bare
      // getUser() validates the (absent) session instead of the token and
      // always fails with AuthSessionMissingError -> a spurious 401. Passing
      // the JWT takes the direct validation path in auth-js.
      const { data: userData, error: userErr } = await caller.auth.getUser(bearer)
      if (userErr || !userData?.user) return json({ error: 'unauthenticated' }, 401)

      const { data: owns, error: ownErr } = await caller.rpc('quizeazz_attempt_belongs_to_caller', {
        p_attempt_id: attemptId,
      })
      if (ownErr) return json({ error: 'ownership check failed' }, 403)
      if (!owns) return json({ error: 'attempt does not belong to the caller' }, 403)
    }

    const result = await gradeAttempt(cfg, admin, attemptId)
    return json(result)
  } catch (err) {
    const message = err instanceof Error ? err.message : 'unexpected error'
    return json({ error: message }, 500)
  }
})

/**
 * Grades every programming answer of one finalized attempt.
 *
 * `admin` is the service-role client. It must always be the service-role
 * client: every RPC used below is REVOKEd from `authenticated` in Migration 7.
 *
 * Only the id of the answer that is being written is passed through, so a
 * service-role sweep cannot write to an arbitrary answer row.
 */
async function gradeAttempt(
  cfg: { baseUrl: string; token: string },
  admin: ReturnType<typeof createClient>,
  attemptId: string,
): Promise<Record<string, unknown>> {
  const { data: pending, error: pendingErr } = await admin.rpc(
    'quizeazz_judge0_pending_answers',
    { p_attempt_id: attemptId },
  )
  if (pendingErr) {
    return { attempt_id: attemptId, ok: false, error: `pending lookup failed: ${pendingErr.message}` }
  }

  const rows = (pending ?? []) as Array<{
    answer_id: string
    language: string | null
    source_code: string | null
    stdin: string | null
    marks_total: number
  }>

  if (rows.length === 0) {
    return { attempt_id: attemptId, ok: true, graded: 0, note: 'nothing pending' }
  }

  const graded: Array<Record<string, unknown>> = []

  for (const row of rows) {
    // No code at all -> genuinely not attempted. This is a fact about the
    // submission, not a guess about correctness.
    if (!row.source_code || row.source_code.trim() === '') {
      const applied = await applyVerdict(admin, {
        attemptId,
        answerId: row.answer_id,
        submissionToken: null,
        outcome: 'Not Attempted',
        statusId: null,
        exitCode: null,
        memory: null,
        time: null,
        stdout: null,
        stderr: null,
        compileOutput: null,
        message: null,
      })
      graded.push({ answer_id: row.answer_id, outcome: 'Not Attempted', applied })
      continue
    }

    const built = buildSubmissionBody({
      language: row.language,
      sourceCode: row.source_code,
      stdin: row.stdin,
    })

    if (!built.ok) {
      // Unsupported language: an honest System Error, zero marks. We do not
      // guess a compiler and we do not claim the code was wrong.
      const applied = await applyVerdict(admin, {
        attemptId,
        answerId: row.answer_id,
        submissionToken: null,
        outcome: 'System Error',
        statusId: null,
        exitCode: null,
        memory: null,
        time: null,
        stdout: null,
        stderr: null,
        compileOutput: null,
        message: built.reason,
      })
      graded.push({ answer_id: row.answer_id, outcome: 'System Error', applied })
      continue
    }

    let submissionToken: string
    try {
      submissionToken = await submitProgram(cfg, built.body)
    } catch (err) {
      // TRANSPORT FAILURE. The answer stays exactly as it is: pending.
      // A Judge0 outage must never be recorded as Wrong Answer.
      graded.push({
        answer_id: row.answer_id,
        outcome: null,
        pending: true,
        error: err instanceof Error ? err.message : 'judge0 submission failed',
      })
      continue
    }

    let result
    try {
      result = await pollUntilVerdict(cfg, submissionToken, {
        attempts: POLL_ATTEMPTS,
        delayMs: POLL_DELAY_MS,
      })
    } catch (err) {
      graded.push({
        answer_id: row.answer_id,
        outcome: null,
        pending: true,
        error: err instanceof Error ? err.message : 'judge0 poll failed',
      })
      continue
    }

    const verdict = mapJudge0Result(result, SUBMISSION_LIMITS.memory_limit)

    if (verdict.pending) {
      // Deadline reached with no real verdict. Leave it pending for a sweep.
      graded.push({
        answer_id: row.answer_id,
        outcome: null,
        pending: true,
        submission_token: submissionToken,
      })
      continue
    }

    const applied = await applyVerdict(admin, {
      attemptId,
      answerId: row.answer_id,
      submissionToken,
      outcome: verdict.outcome as Judge0Outcome,
      statusId: verdict.statusId,
      exitCode: typeof result.exit_code === 'number' ? result.exit_code : null,
      memory: typeof result.memory === 'number' ? result.memory : null,
      time: result.time ?? null,
      stdout: result.stdout ?? null,
      stderr: result.stderr ?? null,
      compileOutput: result.compile_output ?? null,
      message: result.message ?? null,
    })

    graded.push({
      answer_id: row.answer_id,
      outcome: verdict.outcome,
      status_id: verdict.statusId,
      applied,
    })
  }

  // Recompute the attempt totals now that programming verdicts landed.
  const { error: finalizeErr } = await admin.rpc('quizeazz_finalize_attempt_grading', {
    p_attempt_id: attemptId,
  })

  return {
    attempt_id: attemptId,
    ok: true,
    graded: graded.length,
    results: graded,
    finalize_error: finalizeErr ? finalizeErr.message : null,
  }
}

interface VerdictPayload {
  attemptId: string
  answerId: string
  submissionToken: string | null
  outcome: Judge0Outcome
  statusId: number | null
  exitCode: number | null
  memory: number | null
  time: string | number | null
  stdout: string | null
  stderr: string | null
  compileOutput: string | null
  message: string | null
}

async function applyVerdict(
  admin: ReturnType<typeof createClient>,
  v: VerdictPayload,
): Promise<boolean> {
  const { error } = await admin.rpc('quizeazz_apply_judge0_verdict', {
    p_attempt_id: v.attemptId,
    p_answer_id: v.answerId,
    p_submission_token: v.submissionToken,
    p_outcome: v.outcome,
    p_status_id: v.statusId,
    p_exit_code: v.exitCode,
    p_memory: v.memory,
    p_time: v.time,
    p_stdout: v.stdout,
    p_stderr: v.stderr,
    p_compile_output: v.compileOutput,
    p_message: v.message,
  })
  if (error) {
    console.error('[judge0-grade] apply verdict failed:', error.message)
    return false
  }
  return true
}
