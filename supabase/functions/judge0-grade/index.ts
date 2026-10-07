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
  normalizeProgramOutput,
  outputsMatch,
  pollUntilVerdict,
  submitProgram,
  type HiddenTestCase,
  type Judge0Outcome,
  type Judge0ResultFields,
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

    // The CALLER's client. It carries the student's own JWT in the
    // Authorization header, so auth.uid() resolves inside SQL. It is used
    // for exactly two things: resolving the caller, and the ownership check
    // below. It must NOT be used for the grading RPCs.
    //
    // The second createClient argument must be the platform-provided anon
    // key, NEVER the student's JWT: supabase-js sends that argument verbatim
    // as the `apikey` header, and the Supabase gateway rejects a user JWT in
    // `apikey` with 401 before GoTrue ever validates the user (which surfaced
    // as {"error":"unauthenticated"}). The student's identity travels only in
    // Authorization. SUPABASE_ANON_KEY is auto-injected by Supabase and is
    // public by design, so this adds no secret.
    const caller = createClient(
      Deno.env.get('SUPABASE_URL') as string,
      Deno.env.get('SUPABASE_ANON_KEY') as string,
      {
        global: {
          headers: {
            Authorization: `Bearer ${bearer}`,
          },
        },
        auth: { persistSession: false, autoRefreshToken: false },
      },
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

    let payload: { attempt_id?: string; sweep?: boolean; validate_reference?: boolean; question_id?: string }
    try {
      payload = await req.json()
    } catch {
      return json({ error: 'invalid json body' }, 400)
    }

    // ---------------------------------------------------------------
    // Reference-validation mode: run the faculty reference solution through
    // Judge0 once per hidden-test stdin and store the produced stdout as the
    // expected output. Teacher-only (service role or faculty JWT). The
    // reference solution is fetched service-role-only and never leaves the
    // server; Judge0 is the sole executor. A case whose reference run does
    // not cleanly succeed keeps expected_stdout NULL (unvalidated), so the
    // paper gate keeps blocking publication until every case validates.
    // ---------------------------------------------------------------
    if (payload.validate_reference === true) {
      const questionId = payload.question_id
      if (!questionId || typeof questionId !== 'string') {
        return json({ error: 'question_id is required' }, 400)
      }
      if (!isServiceRole) {
        const { data: userData, error: userErr } = await caller.auth.getUser(bearer)
        if (userErr || !userData?.user) return json({ error: 'unauthenticated' }, 401)
        const { data: isFaculty, error: facultyErr } = await caller.rpc('quizeazz_caller_is_faculty')
        if (facultyErr || !isFaculty) return json({ error: 'faculty authorization required' }, 403)
      }
      return json(await validateReference(cfg, admin, questionId))
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
    question_id: string
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

    // Semantic grading: run the submission once per VALIDATED hidden test
    // case (expected_stdout IS NOT NULL) and compare normalized stdout.
    // Stops at the first decisive case. 'Correct' requires every validated
    // case to pass. A fetch failure leaves the answer pending (retryable);
    // zero usable cases is a stable spec state recorded as System Error.
    const { data: caseRows, error: casesErr } = await admin.rpc(
      'quizeazz_judge0_test_cases',
      { p_question_id: row.question_id },
    )
    if (casesErr) {
      graded.push({
        answer_id: row.answer_id,
        outcome: null,
        pending: true,
        error: `test case lookup failed: ${casesErr.message}`,
      })
      continue
    }
    const usableCases = ((caseRows ?? []) as HiddenTestCase[])
      .filter((tc) => tc.expected_stdout !== null && tc.expected_stdout !== undefined)
      .sort((a, b) => a.case_order - b.case_order)

    if (usableCases.length === 0) {
      // No validated hidden test exists for this question, so correctness
      // is unprovable. An honest System Error (zero marks), never a guess
      // and never an exit-code-only 'Correct'.
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
        message: 'no validated hidden tests for this question',
      })
      graded.push({ answer_id: row.answer_id, outcome: 'System Error', applied })
      continue
    }

    const caseRun = await runHiddenTests(cfg, {
      sourceCode: row.source_code,
      cases: usableCases,
    })
    if (!caseRun.ok) {
      graded.push({
        answer_id: row.answer_id,
        outcome: null,
        pending: true,
        error: caseRun.transportError,
      })
      continue
    }

    const applied = await applyVerdict(admin, {
      attemptId,
      answerId: row.answer_id,
      submissionToken: caseRun.submissionToken,
      outcome: caseRun.outcome,
      statusId: caseRun.statusId,
      exitCode: caseRun.exitCode,
      memory: caseRun.memory,
      time: caseRun.time,
      stdout: caseRun.stdout,
      stderr: caseRun.stderr,
      compileOutput: caseRun.compileOutput,
      message: `Hidden tests passed: ${caseRun.passedCases}/${caseRun.totalCases}`,
    })

    graded.push({
      answer_id: row.answer_id,
      outcome: caseRun.outcome,
      status_id: caseRun.statusId,
      passed_cases: caseRun.passedCases,
      total_cases: caseRun.totalCases,
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

/**
 * Runs one submission per hidden test case, in case_order, stopping at the
 * first decisive case. A non-Accepted terminal status (CE/RE/TLE/MLE/System)
 * is preserved as-is; an Accepted run whose normalized stdout mismatches is
 * Wrong Answer. Only all-cases-pass yields Correct.
 *
 * Transport failures and poll deadlines are NOT verdicts: they return
 * ok:false so the caller leaves the answer pending for a later sweep.
 * Judge0 is the sole executor; nothing here runs student code locally.
 */
interface CaseRunSuccess {
  ok: true
  outcome: Judge0Outcome
  statusId: number | null
  exitCode: number | null
  memory: number | null
  time: string | number | null
  stdout: string | null
  stderr: string | null
  compileOutput: string | null
  submissionToken: string
  passedCases: number
  totalCases: number
}

interface CaseRunTransport {
  ok: false
  transportError: string
}

async function runHiddenTests(
  cfg: { baseUrl: string; token: string },
  input: { sourceCode: string; cases: HiddenTestCase[] },
): Promise<CaseRunSuccess | CaseRunTransport> {
  let passedCases = 0
  // Decisive run evidence. Initialized empty; always overwritten by the
  // first completed Judge0 run before any ok:true return below.
  let last: {
    submissionToken: string
    statusId: number | null
    exitCode: number | null
    memory: number | null
    time: string | number | null
    stdout: string | null
    stderr: string | null
    compileOutput: string | null
  } = {
    submissionToken: '',
    statusId: null,
    exitCode: null,
    memory: null,
    time: null,
    stdout: null,
    stderr: null,
    compileOutput: null,
  }

  for (const tc of input.cases) {
    const built = buildSubmissionBody({
      language: 'cpp',
      sourceCode: input.sourceCode,
      stdin: tc.stdin ?? null,
    })
    if (!built.ok) {
      // Unreachable in practice (language is forced, source non-empty), but
      // fail safe rather than submitting something unvalidated.
      return {
        ok: true,
        outcome: 'System Error',
        ...last,
        passedCases,
        totalCases: input.cases.length,
      }
    }

    let submissionToken: string
    try {
      submissionToken = await submitProgram(cfg, built.body)
    } catch (err) {
      return {
        ok: false,
        transportError: err instanceof Error ? err.message : 'judge0 submission failed',
      }
    }

    let result: Judge0ResultFields
    try {
      result = await pollUntilVerdict(cfg, submissionToken, {
        attempts: POLL_ATTEMPTS,
        delayMs: POLL_DELAY_MS,
      })
    } catch (err) {
      return {
        ok: false,
        transportError: err instanceof Error ? err.message : 'judge0 poll failed',
      }
    }

    if (result.status_id === null) {
      // Poll deadline with no verdict. Not a verdict — leave pending.
      return { ok: false, transportError: 'judge0 verdict deadline exceeded' }
    }

    last = {
      submissionToken,
      statusId: result.status_id,
      exitCode: typeof result.exit_code === 'number' ? result.exit_code : null,
      memory: typeof result.memory === 'number' ? result.memory : null,
      time: result.time ?? null,
      stdout: result.stdout ?? null,
      stderr: result.stderr ?? null,
      compileOutput: result.compile_output ?? null,
    }

    if (result.status_id !== 3) {
      // First meaningful execution failure wins; further cases cannot change
      // a non-Accepted terminal status into Correct. Stop here.
      const mapped = mapJudge0Result(result, SUBMISSION_LIMITS.memory_limit)
      return { ok: true, outcome: mapped.outcome, ...last, passedCases, totalCases: input.cases.length }
    }

    if (!outputsMatch(result.stdout, tc.expected_stdout)) {
      return {
        ok: true,
        outcome: 'Wrong Answer',
        ...last,
        passedCases,
        totalCases: input.cases.length,
      }
    }

    passedCases += 1
  }

  return { ok: true, outcome: 'Correct', ...last, passedCases, totalCases: input.cases.length }
}

interface ReferenceValidation {
  ok: boolean
  validated?: number
  total?: number
  error?: string
  results?: Array<{
    case_order: number
    ok: boolean
    status_id: number | null
    stdout_preview: string | null
    error?: string
  }>
}

/**
 * Reference-validation run for teacher-authored hidden tests. Executes the
 * faculty reference solution through Judge0 (never locally) once per stdin
 * and stores each cleanly-succeeded run's normalized stdout as that case's
 * expected output. Cases whose reference run fails keep expected_stdout NULL
 * (unvalidated), so the paper gate keeps blocking publication.
 */
async function validateReference(
  cfg: { baseUrl: string; token: string },
  admin: ReturnType<typeof createClient>,
  questionId: string,
): Promise<ReferenceValidation> {
  const { data: refRows, error: refErr } = await admin.rpc('quizeazz_question_reference', {
    p_question_id: questionId,
  })
  if (refErr) {
    return { ok: false, error: `reference lookup failed: ${refErr.message}` }
  }
  const ref = ((refRows ?? []) as Array<{ reference_solution: string | null; language: string | null }>)[0]
  if (!ref || !ref.reference_solution || ref.reference_solution.trim() === '') {
    return { ok: false, error: 'no reference solution stored for this question' }
  }

  const { data: caseRows, error: casesErr } = await admin.rpc('quizeazz_judge0_test_cases', {
    p_question_id: questionId,
  })
  if (casesErr) {
    return { ok: false, error: `test case lookup failed: ${casesErr.message}` }
  }
  const cases = ((caseRows ?? []) as HiddenTestCase[]).sort((a, b) => a.case_order - b.case_order)
  if (cases.length === 0) {
    return { ok: false, error: 'no hidden test cases defined for this question' }
  }

  const results: NonNullable<ReferenceValidation['results']> = []
  const outputs: Array<{ case_order: number; expected_stdout: string }> = []

  for (const tc of cases) {
    const built = buildSubmissionBody({
      language: 'cpp',
      sourceCode: ref.reference_solution,
      stdin: tc.stdin ?? null,
    })
    if (!built.ok) {
      results.push({ case_order: tc.case_order, ok: false, status_id: null, stdout_preview: null, error: built.reason })
      continue
    }

    let submissionToken: string
    try {
      submissionToken = await submitProgram(cfg, built.body)
    } catch (err) {
      results.push({
        case_order: tc.case_order,
        ok: false,
        status_id: null,
        stdout_preview: null,
        error: err instanceof Error ? err.message : 'judge0 submission failed',
      })
      continue
    }

    let result: Judge0ResultFields
    try {
      result = await pollUntilVerdict(cfg, submissionToken, {
        attempts: POLL_ATTEMPTS,
        delayMs: POLL_DELAY_MS,
      })
    } catch (err) {
      results.push({
        case_order: tc.case_order,
        ok: false,
        status_id: null,
        stdout_preview: null,
        error: err instanceof Error ? err.message : 'judge0 poll failed',
      })
      continue
    }

    if (result.status_id !== 3) {
      const mapped = mapJudge0Result(result, SUBMISSION_LIMITS.memory_limit)
      results.push({
        case_order: tc.case_order,
        ok: false,
        status_id: result.status_id,
        stdout_preview: (result.stdout ?? '').slice(0, 500),
        error: `reference did not succeed (mapped: ${mapped.outcome})`,
      })
      continue
    }

    const expected = normalizeProgramOutput(result.stdout)
    outputs.push({ case_order: tc.case_order, expected_stdout: expected })
    results.push({
      case_order: tc.case_order,
      ok: true,
      status_id: result.status_id,
      stdout_preview: expected.slice(0, 500),
    })
  }

  if (outputs.length > 0) {
    const { error: applyErr } = await admin.rpc('quizeazz_apply_reference_outputs', {
      p_question_id: questionId,
      p_outputs: outputs,
    })
    if (applyErr) {
      return { ok: false, error: `storing expected outputs failed: ${applyErr.message}`, results }
    }
  }

  return { ok: true, validated: outputs.length, total: cases.length, results }
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
