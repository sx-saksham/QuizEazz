// ============================================================================
// burst_75_leg_a.driver.mjs - scenario driver for LEG A of the 75-submission
// burst validation.
//
// BOOTSTRAP (mirrors scripts/simulate_grading_recovery.driver.mjs)
//   This file is the ENTRY POINT, not a fragment. It is invoked as
//       node burst_75_leg_a.driver.mjs [--mode burst|hang] <harness.ts>
//   It reads the generated TypeScript harness from argv, transpiles it with the
//   repository's installed TypeScript package, imports the result as H, and
//   then calls H's real exports. sim.RUNNER is deliberately NOT used: that
//   stub calls mod.default(), and the generated harness has no default export.
//
// WHAT IS REAL
//   Every function that decides a verdict, schedules a wave, enforces a
//   deadline or isolates a failure is lifted VERBATIM from the working tree by
//   scripts/simulate_grading_recovery.py::build_harness_ts and lives in the
//   generated harness. Nothing here reimplements any of it.
//
// WHAT IS FAKED
//   Only: submitProgram(), fetchProgramResult(), admin.rpc(), and the clock.
//   Those are the four surfaces that cannot be called for real without Judge0
//   and a live Supabase project.
//
// VIRTUAL CLOCK
//   Date.now() and setTimeout() are replaced below. Every duration reported as
//   "virtual" is model time under total control. It is NOT wall-clock time and
//   it proves NOTHING about the Supabase Edge Runtime's execution ceiling.
//   Real wall-clock findings are reported separately by the Python runner.
// ============================================================================

import { readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import ts from 'typescript'

// ------------------------------- BOOTSTRAP ----------------------------------
const argv = process.argv.slice(2)
const modeIdx = argv.indexOf('--mode')
const MODE = modeIdx >= 0 ? argv[modeIdx + 1] : 'burst'
const HARNESS = argv.find((a) => a.endsWith('.ts'))
if (!HARNESS) {
  console.error('usage: node burst_75_leg_a.driver.mjs [--mode burst|hang] <harness.ts>')
  process.exit(2)
}

const src = readFileSync(HARNESS, 'utf8')
const js = ts.transpileModule(src, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
    strict: false,
  },
  fileName: 'harness.ts',
}).outputText
const modFile = join(tmpdir(), `burst75_${MODE}_${process.pid}.mjs`)
writeFileSync(modFile, js)
const H = await import('file://' + modFile.replace(/\\/g, '/'))

// The harness must expose exactly these, or we are not testing the real path.
for (const name of ['runSweepReal', '__gradeAttempt']) {
  if (typeof H[name] !== 'function') {
    console.error(`harness does not export ${name}() - refusing to run`)
    process.exit(3)
  }
}

// ============================== VIRTUAL CLOCK ================================
let NOW = 0
const TIMERS = []
let SEQ = 0

globalThis.setTimeout = (fn, ms) => {
  TIMERS.push({ t: NOW + (Number(ms) || 0), s: SEQ++, fn })
  return 0
}
Date.now = () => NOW

function resetClock() { NOW = 0; SEQ = 0; TIMERS.length = 0 }

// Advances virtual time until the predicate says work is done.
//
//  * timers fire in virtual-time order (ties broken by insertion sequence),
//  * microtasks are flushed between callbacks because setImmediate runs only
//    after the microtask queue has drained - which is what lets awaits inside
//    the lifted grading code make progress,
//  * the stall counter bounds the wait when NO runnable timer exists, so a
//    promise that can never make progress fails loudly instead of spinning
//    for 50 million turns.
async function drive(isDone) {
  let stalled = 0
  for (let guard = 0; guard < 50_000_000; guard++) {
    await new Promise((r) => setImmediate(r))
    if (isDone()) return true
    if (!TIMERS.length) {
      if (++stalled > 1000) throw new Error('virtual clock stalled: no runnable timer')
      continue
    }
    stalled = 0
    TIMERS.sort((a, b) => a.t - b.t || a.s - b.s)
    const e = TIMERS.shift()
    NOW = e.t
    e.fn()
  }
  throw new Error('virtual clock failed to settle')
}

// Runs `promise` to completion WHILE pumping the virtual clock, then returns
// its value. This is the fix for the deadlock: awaiting a sweep directly would
// never let the queued POLL_DELAY_MS timers fire, because nothing advances the
// virtual clock while the sweep's promise is pending.
//
// A rejection is RE-RAISED unchanged. `tracked` only records the outcome and
// never itself rejects, so the original error is propagated verbatim and is
// never converted into a silent success.
async function pump(promise, label) {
  let state = 'pending'
  let value
  let reason
  const tracked = promise.then(
    (v) => { state = 'fulfilled'; value = v },
    (e) => { state = 'rejected'; reason = e },
  )
  await drive(() => state !== 'pending')
  await tracked
  if (state === 'rejected') throw reason
  return value
}

// Never-settling promise, used by the hang probe.
function neverResolves() { return new Promise(() => {}) }

// ============================ SCENARIO CONFIG ===============================
const N_CASES = 5
let CFG = null

// ================================ FAKE JUDGE0 ================================
// Deterministic, fully synthetic. status_id and stdout are CHOSEN by the
// scenario; no code is compiled and no program is executed. Correctness of C++
// compilation and of Judge0 itself are explicitly OUT OF SCOPE.
//
// MAPPING: every hidden test case carries a unique stdin of the form
// "A<attempt>-C<case>" (see makeDb). buildSubmissionBody copies a non-empty
// stdin onto the request body, so the fake recovers the EXACT (attempt, case)
// pair from the real payload instead of guessing from submission order.
//
// This is what fixes the earlier defect: the cursor advanced once per
// SUBMISSION, but runHiddenTests submits once per CASE, so attempt i's later
// cases were handed other attempts' specifications. Keying on the payload
// removes the cursor entirely and is stable across repeated sweeps.
//
// A token is minted monotonically and NEVER reset, so a stale token can never
// be re-associated with a different attempt.
globalThis.__SIM = {
  judge0: {
    async submit(cfg, body) {
      if (CFG.judge0DownOn === 'submit') throw new Error('fake judge0 unreachable')
      NOW += CFG.submitLatency
      if (CFG.hangOn === 'submit') return neverResolves()
      const key = String(body?.stdin ?? '')
      const tok = 'tok-' + (CFG.tokenSeq++)       // monotonic across the whole run
      CFG.tokens.set(tok, { key, polls: 0 })
      return tok
    },
    async result(cfg, token) {
      if (CFG.judge0DownOn === 'fetch') throw new Error('fake judge0 unreachable')
      if (CFG.hangOn === 'fetch') return neverResolves()
      NOW += CFG.pollLatency
      const st = CFG.tokens.get(token)
      if (!st) {
        // Unknown token: stay In Queue forever. Never fabricate a verdict.
        return { status_id: 1, exit_code: null, memory: null, time: null,
                 stdout: null, stderr: null, compile_output: null, token }
      }
      const plan = planFor(st.key)
      st.polls++
      // Stay "In Queue" for inQueuePolls polls, then return the terminal status.
      if (st.polls <= plan.inQueuePolls) {
        return { status_id: 1, exit_code: null, memory: null, time: null,
                 stdout: null, stderr: null, compile_output: null, token }
      }
      return {
        status_id: plan.status,
        exit_code: plan.status === 3 ? 0 : 1,
        memory: 1000,
        time: '0.01',
        stdout: plan.stdout,
        stderr: null,
        compile_output: plan.compileOutput ?? null,
        token,
      }
    },
  },
}

// Resolve "<attempt>-<case>" to that attempt's intended classification.
// Attempt index -> spec; the case index is carried explicitly so per-case
// behaviour stays visible and can diverge later without touching the map.
function planFor(key) {
  const m = /^A(\d+)-C(\d+)$/.exec(key)
  if (!m) {
    // Unrecognised payload: report a compile error rather than guess a verdict.
    return { status: 6, inQueuePolls: 0, stdout: null, compileOutput: 'unkeyed stdin' }
  }
  const i = Number(m[1])
  const spec = CFG.specs[i]
  if (!spec) {
    return { status: 6, inQueuePolls: 0, stdout: null, compileOutput: 'no spec' }
  }
  if (spec.compileError) {
    return { status: 6, inQueuePolls: spec.inQueuePolls, stdout: null,
             compileOutput: 'error: synthetic compile failure' }
  }
  if (spec.expectWrong) {
    // Genuinely wrong: Judge0 status 4. Distinct from a transport failure,
    // which produces NO status at all and therefore leaves the answer pending.
    return { status: 4, inQueuePolls: spec.inQueuePolls, stdout: 'WRONG-OUTPUT' }
  }
  // Healthy: stdout equals this attempt's own expected_stdout for every case.
  return { status: 3, inQueuePolls: spec.inQueuePolls, stdout: spec.expected }
}

// ============================ FAKE ADMIN / POSTGRES =========================
// Models the two RPCs the grading path uses. applyVerdict() enforces the same
// single-verdict rule that migration 20's FOR UPDATE protects, and COUNTS
// rejections. It does NOT and cannot prove row-lock semantics - that is Leg B.
function attemptId(i) { return `3333${String(i).padStart(4, '0')}-3333-3333-3333-333333333333`.slice(0, 36) }
function answerId(i) { return `4444${String(i).padStart(4, '0')}-4444-4444-4444-444444444444`.slice(0, 36) }

function makeDb(nAttempts) {
  const db = []
  for (let i = 0; i < nAttempts; i++) {
    db.push({
      id: attemptId(i),
      status: 'submitted',
      score: 0,
      answers: [{
        answer_id: answerId(i),
        question_id: '5555' + '0'.repeat(27) + '1',
        evaluation_status: 'Pending Evaluation',
        is_correct: false,
        marks_awarded: 0,
        apply_count: 0,
        source_code: '#include <iostream>\nint main(){std::cout<<' + i + ';}',
        language: 'cpp',
        stdin: null,
        marks_total: 10,
        tests: Array.from({ length: N_CASES }, (_, k) => ({
          // The unique per-case key the fake reads back out of the request body.
          case_order: k + 1,
          stdin: 'A' + i + '-C' + k,
          expected_stdout: String(i),
        })),
      }],
    })
  }
  return db
}

function makeStats() {
  return { guardRejections: 0, rpcThrows: 0, doubleApplyDetected: 0, attemptsStarted: [] }
}

function makeAdmin(db, stats) {
  return {
    rpc: async (name, arg) => {
      if (name === 'quizeazz_list_attempts_needing_programming_grading') {
        // MIRRORS migration_17's quizeazz_list_attempts_needing_programming_grading.
        //
        // The real function is driven by the ANSWERS table, not by the attempt's
        // own status:
        //     WHERE a.evaluation_status = 'Pending Evaluation'
        //       AND a.code IS NOT NULL AND btrim(a.code) <> ''
        //     JOIN exam_attempts att ON att.id = a.attempt_id
        //       AND att.status IN ('submitted','timed_out','evaluating','evaluated')
        //       AND COALESCE(att.submitted_at, att.created_at)
        //           < now() - make_interval(secs => 60)
        //
        // So an attempt leaves the list the moment its last pending answer is
        // graded - NOT because its own status changed.
        //
        // DELIBERATE OMISSION: the 60-second staleness guard is not modelled.
        // Modelling it would deadlock this harness - an attempt younger than 60s
        // of VIRTUAL time is ineligible, so nothing is admitted, no Judge0 call
        // happens, no timer is ever queued, and virtual time can never advance
        // to reach the threshold. The fixture instead represents a backlog that
        // has already been pending longer than the guard.
        const ELIGIBLE_STATUS = new Set(['submitted', 'timed_out', 'evaluating', 'evaluated'])
        const ids = db
          .filter((a) => ELIGIBLE_STATUS.has(a.status))
          .filter((a) => a.answers.some(
            (x) => x.evaluation_status === 'Pending Evaluation'
                && typeof x.source_code === 'string'
                && x.source_code.trim() !== ''))
          .map((a) => a.id)
        return { data: ids.map((id) => ({ attempt_id: id })), error: null }
      }
      if (name === 'quizeazz_judge0_pending_answers') {
        if (CFG.rpcRejectAttempts && CFG.rpcRejectAttempts.has(arg.p_attempt_id)) {
          stats.rpcThrows++
          throw new Error('synthetic RPC rejection')
        }
        stats.attemptsStarted.push(arg.p_attempt_id)
        const a = db.find((x) => x.id === arg.p_attempt_id)
        if (!a) return { data: null, error: { message: 'no attempt' } }
        // MIRRORS THE REAL RPC. migration_07's quizeazz_judge0_pending_answers
        // ends its query with
        //     AND a.evaluation_status = 'Pending Evaluation'
        // Without that predicate this fake returned every answer, including
        // ones that already carried a verdict. gradeAttempt's "nothing pending"
        // guard (rows.length === 0) could then never fire, so each sweep
        // re-grounded completed attempts from scratch, burned the full virtual
        // Judge0 time on them, and then had its applyVerdict call refused by
        // the single-verdict guard. Net progress per sweep was zero.
        // An attempt whose answers are all graded now yields [], which lets the
        // lifted guard short-circuit it instantly.
        const pending = a.answers.filter(
          (x) => x.evaluation_status === 'Pending Evaluation')
        return { data: pending.map((x) => ({ ...x })), error: null }
      }
      if (name === 'quizeazz_apply_judge0_verdict') {
        const a = db.find((x) => x.id === arg.p_attempt_id)
        if (!a) return { data: null, error: { message: 'no attempt' } }
        const ans = a.answers.find((x) => x.answer_id === arg.p_answer_id)
        if (!ans) return { data: null, error: { message: 'no answer' } }
        if (ans.evaluation_status !== 'Pending Evaluation') {
          stats.guardRejections++
          return { data: null, error: { message: 'already has a verdict' } }
        }
        if (!['Correct', 'Wrong Answer', 'Compilation Error', 'Runtime Error',
              'Time Limit Exceeded', 'Memory Limit Exceeded', 'System Error',
              'Not Attempted'].includes(arg.p_outcome)) {
          return { data: null, error: { message: 'unknown outcome' } }
        }
        ans.evaluation_status = arg.p_outcome
        ans.is_correct = arg.p_outcome === 'Correct'
        ans.marks_awarded = arg.p_outcome === 'Correct' ? 10 : 0
        ans.apply_count++
        // UNREACHABLE, BY CONSTRUCTION - do not read this as evidence.
        // The single-verdict guard above returns BEFORE this increment, and the
        // answer's status is already terminal by then, so apply_count can never
        // exceed 1 and doubleApplyDetected can never fire. This is a SIMULATION of
        // a single-threaded JavaScript state machine, not a concurrency guarantee.
        // Whether one verdict per answer actually holds under real parallel
        // sessions is what scripts/test_verdict_concurrency.py proves, against a
        // real PostgreSQL using migration 20's FOR UPDATE row lock.
        if (ans.apply_count > 1) stats.doubleApplyDetected++
        return { data: true, error: null }
      }
      if (name === 'quizeazz_finalize_attempt_grading') {
        const a = db.find((x) => x.id === arg.p_attempt_id)
        if (a) {
          const pend = a.answers.filter(
            (x) => x.evaluation_status === 'Pending Evaluation').length
          a.status = pend > 0 ? 'evaluating' : 'evaluated'
        }
        return { data: true, error: null }
      }
      return { data: null, error: { message: 'unknown rpc ' + name } }
    },
  }
}

function tally(db) {
  const byOutcome = {}
  const terminalAttemptIds = []
  const pendingAttemptIds = []
  let done = 0, pending = 0, maxApply = 0
  for (const a of db) {
    let anyPending = false
    for (const ans of a.answers) {
      if (ans.evaluation_status === 'Pending Evaluation') { pending++; anyPending = true }
      else {
        byOutcome[ans.evaluation_status] = (byOutcome[ans.evaluation_status] ?? 0) + 1
        done++
      }
      maxApply = Math.max(maxApply, ans.apply_count)
    }
    // Per-attempt evidence: which attempts actually reached a terminal state
    // and which are still pending. This is derived from the same per-answer
    // evaluation_status the rest of the tally reads - no new measurement.
    if (anyPending) pendingAttemptIds.push(a.id)
    else terminalAttemptIds.push(a.id)
  }
  return {
    done, pending, maxApply, byOutcome, total: db.length,
    terminalAttemptIds, pendingAttemptIds,
  }
}

// ============================== SCENARIO SETUPS ==============================
// 75 synthetic attempts. Profiles are chosen so a timeout or transport failure,
// if it ever leaked into an outcome, would be visibly distinguishable from the
// deliberately-wrong submissions.
//
// TIMING (this is what exercises the shipped 110s sweep budget):
//   submitLatency + pollLatency = 120 + 900 = 1020ms per completed case.
//   A healthy attempt runs ALL 5 cases, each preceded by `inQueuePolls`
//   in-queue polls costing (pollLatency + the shipped POLL_DELAY_MS of 1500).
//     healthy      inQueuePolls 3 -> 5 * (120 + 3*2400) = 36_600ms per attempt
//     wrong/comp   inQueuePolls 1 -> terminal status on the FIRST case, so the
//                                  loop short-circuits: 1 * (120 + 1*2400) = 2520ms
//   SWEEP_CONCURRENCY is 10, so a wave costs the SLOWEST member (~36.6s) and
//   roughly three waves (~110s) fit inside SWEEP_BUDGET_MS before the admission
//   check starts deferring the remainder to the next sweep.
//   Nothing here overrides SWEEP_BUDGET_MS, SWEEP_CONCURRENCY,
//   GRADING_ATTEMPT_BUDGET_MS, POLL_ATTEMPTS or POLL_DELAY_MS: all of those
//   still come from the lifted shipped source.
function mkSpecs(n) {
  const specs = []
  for (let i = 0; i < n; i++) {
    const s = { status: 3, inQueuePolls: 3, stdout: null, expected: String(i),
                expectWrong: false, compileError: false }
    if (i % 16 === 3) {
      s.expectWrong = true; s.status = 4; s.inQueuePolls = 1
      s.expected = '999999'; s.stdout = 'WRONG-OUTPUT'
    }
    if (i % 16 === 7) {
      s.compileError = true; s.status = 6; s.inQueuePolls = 1
      s.expected = null; s.stdout = null
    }
    specs.push(s)
  }
  return specs
}

function buildBurstDb(specs) {
  const db = makeDb(specs.length)
  specs.forEach((sp, i) => {
    if (sp.expectWrong) {
      db[i].answers[0].tests = db[i].answers[0].tests.map((t) => ({ ...t, expected_stdout: '999999' }))
    }
  })
  return db
}

// Drives sweeps until the DB drains or the cap is hit. Nothing about token
// identity or the plan is reset between sweeps: CFG.tokens and CFG.tokenSeq
// persist for the whole scenario, so a token minted in sweep 1 can never be
// re-read as belonging to a different attempt in sweep 5.
async function runSweeps(db, admin, specs) {
  const sweeps = []
  for (let s = 0; s < 60; s++) {
    if (tally(db).pending === 0) break
    const t0 = NOW
    // Pumped, not awaited directly: the lifted pollUntilVerdict awaits
    // setTimeout(POLL_DELAY_MS), which the virtual clock enqueues. Only
    // drive() can drain that queue, so the sweep promise is raced against it.
    const res = await pump(
      H.runSweepReal(true, admin, { baseUrl: 'http://fake', token: 't' }),
      `sweep ${s}`,
    )
    sweeps.push({
      index: s, virtualMs: NOW - t0, swept: res.swept, graded: res.graded,
      failed: res.failed, deferred: res.deferred, budget_exhausted: res.budget_exhausted,
    })
  }
  return sweeps
}

// ================================ SCENARIOS =================================
async function scenarioBurst() {
  const specs = mkSpecs(75)
  resetClock()
  CFG = { submitLatency: 120, pollLatency: 900, judge0DownOn: 'submit', hangOn: null,
          tokenSeq: 1, tokens: new Map(), specs: null, rpcRejectAttempts: new Set() }
  CFG.specs = specs
  const db = buildBurstDb(specs)
  const stats = makeStats()
  const admin = makeAdmin(db, stats)

  // First pass: Judge0 unreachable for everything -> nothing may be recorded.
  const sweeps = await runSweeps(db, admin, specs)
  const afterDown = tally(db)

  // Second pass: Judge0 healthy. Everything still pending must recover.
  CFG.judge0DownOn = null
  sweeps.push(...await runSweeps(db, admin, specs))

  return {
    name: 'B75 75 attempts, Judge0 unreachable then recovery',
    n: 75, sweeps, final: tally(db), afterJudge0Down: afterDown,
    totalVirtualMs: NOW, stats,
    expected_wrong: specs.filter((s) => s.expectWrong).length,
    expected_compile_error: specs.filter((s) => s.compileError).length,
  }
}

async function scenarioRpcFail() {
  const specs = mkSpecs(75)
  resetClock()
  CFG = { submitLatency: 120, pollLatency: 900, judge0DownOn: null, hangOn: null,
          tokenSeq: 1, tokens: new Map(), specs,
          rpcRejectAttempts: new Set([attemptId(7)]) }
  CFG.specs = specs
  const db = buildBurstDb(specs)
  const stats = makeStats()
  const admin = makeAdmin(db, stats)
  const sweeps = await runSweeps(db, admin, specs)
  return {
    name: 'B75-RPCFAIL one attempt RPC rejects, 74 unaffected',
    n: 75, sweeps, final: tally(db), totalVirtualMs: NOW, stats,
    rejected_attempt: attemptId(7),
  }
}

// ================================ HANG PROBE ================================
// Drives the lifted gradeAttempt() on the BROWSER path. deadlineAt is passed as
// null, which is exactly what index.ts does for a non-sweep invocation.
async function scenarioHang() {
  resetClock()
  CFG = { submitLatency: 1, pollLatency: 1, judge0DownOn: null, hangOn: 'fetch',
          tokenSeq: 1, tokens: new Map(), specs: null, rpcRejectAttempts: new Set() }
  const db = makeDb(1)
  const stats = makeStats()
  const admin = makeAdmin(db, stats)
  CFG.specs = [{ status: 3, inQueuePolls: 0, expected: '0' }]

  let settled = false
  // NOTE: deadlineAt === null -> the browser-triggered path.
  const work = H.__gradeAttempt(
    { baseUrl: 'http://fake', token: 't' }, admin, db[0].id, 'frontend', null,
  ).then(() => { settled = true }, () => { settled = true })

  // Give the event loop a bounded number of turns. If the promise never
  // settles, `settled` stays false and we report exactly that.
  for (let i = 0; i < 200 && !settled; i++) {
    await new Promise((r) => setImmediate(r))
  }

  return {
    mode: 'hang',
    control_returned: settled,
    deadline_was_null: true,
    browser_path: true,
    virtual_ms: NOW,
    pending_after: tally(db).pending,
    applied_after: tally(db).byOutcome,
    note: 'index.ts passes deadlineAt=null for a browser invocation; fetch() '
        + 'carries no AbortSignal, so no deadline can interrupt an in-flight call',
  }
}

// ================================= ENTRY POINT ===============================
async function main() {
  if (MODE === 'hang') return scenarioHang()
  return { mode: 'burst', scenarios: [await scenarioBurst(), await scenarioRpcFail()] }
}

process.stdout.write(JSON.stringify(await main()))