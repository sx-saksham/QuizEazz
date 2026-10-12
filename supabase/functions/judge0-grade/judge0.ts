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
 * QuizEazz V1 supports exactly one programming language: C++.
 *
 * Every programming submission is executed as C++ (GCC 9.2.0), Judge0
 * language_id 54. The stored `answers.language` value is informational only
 * and is never trusted for compiler selection: a malicious or stale browser
 * value cannot divert execution to another language. Python, Java and C
 * mappings were removed permanently.
 */
export const QUIZEAZZ_CPP_JUDGE0_LANGUAGE_ID = 54

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
 * Resolves the Judge0 language_id for a programming submission.
 *
 * Always returns C++ (54). The caller-supplied identifier is accepted only
 * as `'cpp'` or empty (legacy rows predate strict validation); anything else
 * yields null so the caller reports an honest System Error instead of
 * executing code under the wrong compiler. The language_id itself never
 * comes from the browser.
 */
export function resolveLanguageId(identifier: string | null | undefined): number | null {
  if (identifier === null || identifier === undefined || identifier === '' || identifier === 'cpp') {
    return QUIZEAZZ_CPP_JUDGE0_LANGUAGE_ID
  }
  return null
}

/**
 * A hidden test case for semantic grading. Fetched service-role-only; never
 * present in any browser request, response, or bundle.
 */
export interface HiddenTestCase {
  case_order: number
  stdin: string | null
  expected_stdout: string | null
}

/**
 * Deterministic stdout normalization for expected-output comparison.
 *
 * Exactly this, nothing more:
 *  - CRLF and lone CR become LF
 *  - trailing spaces/tabs are removed at the end of every line
 *  - all trailing newlines at the very end are removed ("final newline"
 *    normalized, so "0", "0\n" and "0\n\n" are equal)
 *
 * Significant on purpose: leading whitespace, internal whitespace, blank
 * lines in the middle, and any other content difference still mismatch.
 * No fuzzy matching, ever.
 */
export function normalizeProgramOutput(text: string | null | undefined): string {
  if (text === null || text === undefined) return ''
  const lf = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  const stripped = lf
    .split('\n')
    .map((line) => line.replace(/[ \t]+$/, ''))
    .join('\n')
  return stripped.replace(/\n+$/, '')
}

/** True only when the normalized outputs are exactly equal. */
export function outputsMatch(
  actual: string | null | undefined,
  expected: string | null | undefined,
): boolean {
  return normalizeProgramOutput(actual) === normalizeProgramOutput(expected)
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

/**
 * Decodes a Judge0 `base64_encoded=true` text attribute back to a string.
 *
 * Judge0 base64-encodes exactly the free-text attributes when
 * `base64_encoded=true`: `stdout`, `stderr`, `compile_output` and `message`.
 * Those four are the ONLY fields decoded here — `status`, `time`, `memory` and
 * `exit_code` stay JSON scalars and must never be run through this.
 *
 * Robustness rules, both required by the grading contract:
 *
 *  - Invalid UTF-8 becomes U+FFFD REPLACEMENT CHARACTER instead of throwing.
 *    GCC emits non-UTF-8 bytes in `compile_output` for ordinary invalid C++,
 *    which is exactly the case that made Judge0 answer HTTP 400. Throwing here
 *    would replace one stuck evaluation with another.
 *  - A malformed base64 payload degrades to `null`, never an exception.
 *
 * `null`/`undefined` in means `null` out, so "Judge0 said there is no output"
 * stays distinguishable from "the output was empty".
 */
export function decodeJudge0Text(value: unknown): string | null {
  if (value === null || value === undefined) return null
  // Defensive only: with base64_encoded=true Judge0 always sends base64 text.
  // A plain string is passed through rather than mangled, so a proxy or a
  // future Judge0 build that ignores the flag cannot corrupt the output.
  if (typeof value !== 'string') return null
  if (value === '') return ''

  let binary: string
  try {
    binary = atob(value)
  } catch {
    return null
  }

  // Build the byte array first, then decode once with explicit
  // non-fatal error handling: invalid sequences become U+FFFD.
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i) & 0xff
  }
  try {
    return new TextDecoder('utf-8').decode(bytes)
  } catch {
    return new TextDecoder('utf-8', { fatal: false }).decode(bytes)
  }
}

/** Fetches the current state of a submission. */
export async function fetchProgramResult(
  config: Judge0ClientConfig,
  submissionToken: string,
): Promise<Judge0ResultFields> {
  // base64_encoded=true is REQUIRED, not cosmetic.
  //
  // With base64_encoded=false Judge0 refuses to serialise any attribute that
  // is not valid UTF-8 and answers HTTP 400 with "some attributes for this
  // submission cannot be converted to UTF-8, use base64_encoded=true query
  // parameter". GCC routinely puts non-UTF-8 bytes in `compile_output` for
  // ordinary invalid C++, so every wrong-but-compilable-typo submission was
  // wedged as permanently Pending Evaluation. Judge0's own docs state the
  // compiler output must be fetched base64-encoded for exactly this reason.
  const res = await fetch(
    `${config.baseUrl}/submissions/${encodeURIComponent(submissionToken)}?base64_encoded=true&fields=${RESULT_FIELDS}`,
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
    // The four base64 attributes, decoded. Everything else stays a scalar.
    stdout: decodeJudge0Text(parsed.stdout),
    stderr: decodeJudge0Text(parsed.stderr),
    compile_output: decodeJudge0Text(parsed.compile_output),
    message: decodeJudge0Text(parsed.message),
    time: (parsed.time as string | number | null) ?? null,
    token: submissionToken,
  }
}

/**
 * Polls until Judge0 returns a real verdict or the deadline passes.
 *
 * `opts.deadlineAt` is an optional absolute epoch-ms cutoff. It exists so a
 * long sweep can stop one slow attempt instead of being killed by the Edge
 * Function wall clock with half the cohort unexplained. It changes nothing
 * when omitted: the caller then relies on attempts x delayMs exactly as
 * before.
 *
 * Hitting either deadline is NOT a verdict. Both return status_id null, which
 * every caller already treats as "leave this answer Pending Evaluation".
 */
export async function pollUntilVerdict(
  config: Judge0ClientConfig,
  submissionToken: string,
  opts: { attempts: number; delayMs: number; deadlineAt?: number | null },
): Promise<Judge0ResultFields> {
  const deadlineAt = opts.deadlineAt ?? null
  for (let i = 0; i < opts.attempts; i++) {
    if (deadlineAt !== null && Date.now() >= deadlineAt) break
    const result = await fetchProgramResult(config, submissionToken)
    const statusId = result.status_id
    if (statusId !== null && !IN_FLIGHT.has(statusId)) return result
    if (i < opts.attempts - 1) {
      const wait =
        deadlineAt === null
          ? opts.delayMs
          : Math.min(opts.delayMs, deadlineAt - Date.now())
      if (wait <= 0) break
      await new Promise((r) => setTimeout(r, wait))
    }
  }
  // Deadline reached without a verdict. The caller must leave the answer
  // pending; it must never record a failure verdict.
  return { status_id: null, token: submissionToken }
}
