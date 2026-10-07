/**
 * Judge0 CE client — pure logic, no Deno globals except `fetch`.
 *
 * SERVER SIDE ONLY. This module must never be imported by anything under
 * `src/`. The API token is read from the Edge Function environment
 * (`Deno.env`) and is never a module constant, never a literal, and never a
 * `VITE_*` variable.
 *
 * QuizEazz is the authority for when to run code and for what a verdict means.
 * Judge0 is the sole authority for what the code actually did. This module
 * never invents, guesses, softens or estimates a verdict.
 */

/**
 * QuizEazz canonical internal language id -> Judge0 language_id.
 *
 * These QuizEazz identifiers are what `answers.language` already stores and
 * what the student-facing selector already sends. They are NOT changed by the
 * Judge0 integration.
 *
 * `java` maps to 90 (`Java (OpenJDK 17)`). Judge0's own id 62 is
 * `Java (OpenJDK 13.0.1)`; it is deliberately NOT used by QuizEazz and is
 * left active and untouched on the Judge0 server.
 */
export const JUDGE0_LANGUAGE_IDS: Readonly<Record<string, number>> = {
  python: 71, // Python (3.8.1)
  cpp: 54, //    C++ (GCC 9.2.0)
  java: 90, //    Java (OpenJDK 17)
  c: 50, //      C (GCC 9.2.0)
}

/** Java must resolve to the Java 17 language, never to 13. */
export const QUIZEAZZ_JAVA_JUDGE0_LANGUAGE_ID = 90

/**
 * Execution limits for every programming submission.
 *
 * `max_processes_and_or_threads` must stay at 60: a JVM needs far more
 * processes/threads than a Python or C program to boot, and a lower value
 * makes the JVM fail to initialise before it can run.
 *
 * Nothing that could widen the sandbox is ever sent: no compiler_options,
 * no command_line_arguments, no additional_files, no callback_url, and
 * `enable_network` is never set (Judge0 defaults it to false server-side and
 * QuizEazz also refuses to allow a caller to enable it).
 */
export const SUBMISSION_LIMITS = {
  cpu_time_limit: 3,
  cpu_extra_time: 1,
  wall_time_limit: 10,
  memory_limit: 512000,
  max_processes_and_or_threads: 60,
  max_file_size: 1024,
} as const

/**
 * Canonical QuizEazz outcome vocabulary. These are the only values ever
 * written to `answers.evaluation_status` by the Judge0 path.
 */
export type Judge0Outcome =
  | 'Correct'
  | 'Wrong Answer'
  | 'Compilation Error'
  | 'Runtime Error'
  | 'Time Limit Exceeded'
  | 'Memory Limit Exceeded'
  | 'System Error'
  | 'Pending Evaluation'
  | 'Not Attempted'

export interface Judge0ResultFields {
  status_id?: number | null
  exit_code?: number | null
  memory?: number | null
  stdout?: string | null
  stderr?: string | null
  compile_output?: string | null
  message?: string | null
  time?: string | number | null
  token?: string | null
}

export interface MappedVerdict {
  outcome: Judge0Outcome
  /** True only when the attempt is genuinely not finished yet. */
  pending: boolean
  /**
   * Marks are NOT decided here. This module only reports the objective
   * verdict; the database computes marks in SQL from the outcome, so a mark
   * can never be inflated by anything outside the database.
   */
  statusId: number | null
}

/** Judge0 "not finished yet" statuses. Never a verdict. */
const IN_FLIGHT = new Set([1, 2])

/**
 * Maps a raw Judge0 result onto QuizEazz's objective vocabulary.
 *
 * Rules, in order:
 *  - 1 / 2 (In Queue, Processing) are NOT verdicts. They stay pending.
 *  - exit_code 137 together with memory at/over the limit is a genuine
 *    memory kill. Judge0 CE has no dedicated "Memory Limit Exceeded" status,
 *    so it reports this as a Runtime Error with exit 137. Detect it here so
 *    the candidate is told the truth instead of seeing a generic crash.
 *  - every other status maps through the fixed table.
 *  - an unrecognised or missing status becomes System Error, which awards
 *    nothing. It is never silently treated as Correct or Wrong Answer.
 */
export function mapJudge0Result(
  result: Judge0ResultFields | null | undefined,
  memoryLimit: number,
): MappedVerdict {
  const statusId =
    typeof result?.status_id === 'number' ? result.status_id : null

  if (statusId === null) {
    return { outcome: 'System Error', pending: false, statusId: null }
  }
  if (IN_FLIGHT.has(statusId)) {
    return { outcome: 'Pending Evaluation', pending: true, statusId }
  }

  // SIGKILL by the cgroup memory limiter.
  if (result?.exit_code === 137 && typeof result.memory === 'number' && result.memory >= memoryLimit) {
    return { outcome: 'Memory Limit Exceeded', pending: false, statusId }
  }

  switch (statusId) {
    case 3:
      return { outcome: 'Correct', pending: false, statusId }
    case 4:
      return { outcome: 'Wrong Answer', pending: false, statusId }
    case 5:
      return { outcome: 'Time Limit Exceeded', pending: false, statusId }
    case 6:
      return { outcome: 'Compilation Error', pending: false, statusId }
    case 7: // SIGSEGV
    case 8: // SIGXFSZ
    case 9: // SIGFPE
    case 10: // SIGABRT
    case 11: // NZEC
    case 12: // Other runtime error
    case 14: // Exec format error
      return { outcome: 'Runtime Error', pending: false, statusId }
    case 13:
      return { outcome: 'System Error', pending: false, statusId }
    default:
      // Never fabricate. Unknown status awards nothing and says so.
      return { outcome: 'System Error', pending: false, statusId }
  }
}

/**
 * Resolves a stored QuizEazz language id to a Judge0 language_id.
 * Returns null when the language is not supported, so the caller reports an
 * honest System Error instead of guessing a compiler.
 */
export function resolveLanguageId(identifier: string | null | undefined): number | null {
  if (!identifier) return null
  const id = JUDGE0_LANGUAGE_IDS[identifier]
  return typeof id === 'number' ? id : null
}

export interface BuildSubmissionInput {
  language: string | null | undefined
  sourceCode: string
  stdin?: string | null
}

/**
 * Builds the Judge0 submission body.
 *
 * The returned object contains ONLY the fields QuizEazz is allowed to send.
 * base64_encoded is added as a query parameter by the caller, not in the body.
 */
export function buildSubmissionBody(input: BuildSubmissionInput): {
  ok: true
  body: Record<string, unknown>
} | {
  ok: false
  reason: string
} {
  const languageId = resolveLanguageId(input.language)
  if (languageId === null) {
    return { ok: false, reason: `unsupported language: ${input.language ?? '(none)'}` }
  }
  if (typeof input.sourceCode !== 'string' || input.sourceCode.trim() === '') {
    return { ok: false, reason: 'empty source code' }
  }

  const body: Record<string, unknown> = {
    language_id: languageId,
    source_code: input.sourceCode,
    ...SUBMISSION_LIMITS,
    // Defence in depth: Judge0 already refuses these server-side, but the
    // request body itself must never carry them.
    enable_network: false,
  }
  if (typeof input.stdin === 'string' && input.stdin !== '') {
    body.stdin = input.stdin
  }
  return { ok: true, body }
}

export interface Judge0ClientConfig {
  baseUrl: string
  token: string
}

/** Fields requested back from Judge0 for a verdict. */
const RESULT_FIELDS = [
  'stdout',
  'stderr',
  'compile_output',
  'message',
  'status',
  'time',
  'memory',
  'exit_code',
  'exit_signal',
  'wall_time',
].join(',')

/**
 * Submits one program. Throws only on transport/protocol failure, which the
 * caller MUST treat as "no verdict" rather than as a failed program.
 */
export async function submitProgram(
  config: Judge0ClientConfig,
  body: Record<string, unknown>,
): Promise<string> {
  const res = await fetch(`${config.baseUrl}/submissions?base64_encoded=false&wait=false`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Auth-Token': config.token,
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    throw new Error(`Judge0 submission rejected: HTTP ${res.status}`)
  }
  const parsed = (await res.json()) as { token?: string }
  if (!parsed.token) throw new Error('Judge0 did not return a submission token')
  return parsed.token
}

/** Fetches the current state of a submission. */
export async function fetchProgramResult(
  config: Judge0ClientConfig,
  submissionToken: string,
): Promise<Judge0ResultFields> {
  const res = await fetch(
    `${config.baseUrl}/submissions/${encodeURIComponent(submissionToken)}?base64_encoded=false&fields=${RESULT_FIELDS}`,
    {
      method: 'GET',
      headers: { 'X-Auth-Token': config.token },
    },
  )
  if (!res.ok) {
    throw new Error(`Judge0 result fetch failed: HTTP ${res.status}`)
  }
  const parsed = (await res.json()) as {
    status?: { id?: number; description?: string }
    status_id?: number
    [k: string]: unknown
  }
  const statusId = parsed.status_id ?? parsed.status?.id ?? null
  return {
    status_id: typeof statusId === 'number' ? statusId : null,
    exit_code: typeof parsed.exit_code === 'number' ? parsed.exit_code : null,
    memory: typeof parsed.memory === 'number' ? parsed.memory : null,
    stdout: (parsed.stdout as string | null) ?? null,
    stderr: (parsed.stderr as string | null) ?? null,
    compile_output: (parsed.compile_output as string | null) ?? null,
    message: (parsed.message as string | null) ?? null,
    time: (parsed.time as string | number | null) ?? null,
    token: submissionToken,
  }
}

/** Polls until Judge0 returns a real verdict or the deadline passes. */
export async function pollUntilVerdict(
  config: Judge0ClientConfig,
  submissionToken: string,
  opts: { attempts: number; delayMs: number },
): Promise<Judge0ResultFields> {
  for (let i = 0; i < opts.attempts; i++) {
    const result = await fetchProgramResult(config, submissionToken)
    const statusId = result.status_id
    if (statusId !== null && !IN_FLIGHT.has(statusId)) return result
    if (i < opts.attempts - 1) await new Promise((r) => setTimeout(r, opts.delayMs))
  }
  // Deadline reached without a verdict. The caller must leave the answer
  // pending; it must never record a failure verdict.
  return { status_id: null, token: submissionToken }
}
