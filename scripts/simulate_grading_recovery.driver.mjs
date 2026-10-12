// Driver for the QuizEazz grading recovery simulation.
// Virtual clock + simulated Postgres + deterministic Judge0.
// Calls the REAL sweep body extracted from index.ts.

import { readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import ts from 'typescript'

const src = readFileSync(process.argv[2], 'utf8')
const js = ts.transpileModule(src, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, strict: false },
  fileName: 'harness.ts',
}).outputText
const modFile = join(tmpdir(), `quizeazz_sim_${process.pid}.mjs`)
writeFileSync(modFile, js)
const H = await import('file://' + modFile.replace(/\\/g, '/'))

// ================= VIRTUAL CLOCK =================
let NOW = 0, SEQ = 0
const TIMERS = []
globalThis.setTimeout = (fn, ms) => { TIMERS.push({ t: NOW + (Number(ms) || 0), s: SEQ++, fn }); return 0 }
Date.now = () => NOW

// Advances virtual time until the supplied work reports done. Microtasks are
// flushed each turn so awaits inside the real grading code can make progress.
async function drive(isDone) {
  for (let guard = 0; guard < 50_000_000; guard++) {
    await new Promise((r) => setImmediate(r))
    if (isDone()) return
    if (!TIMERS.length) continue
    TIMERS.sort((a, b) => a.t - b.t || a.s - b.s)
    const e = TIMERS.shift()
    NOW = e.t
    e.fn()
  }
  throw new Error('virtual clock failed to settle')
}

// ================= SCENARIO CONFIG =================
const N_CASES = 5
let CFG = null
const pollCounts = {}

globalThis.__SIM = {
  judge0: {
    submit(cfg, body) {
      // Routing metadata rides in the hidden-case stdin, which the REAL
      // buildSubmissionBody copies into the Judge0 body verbatim.
      const m = /^A(\d+):C(\d+)$/.exec(String(body.stdin || ''))
      const a = m ? Number(m[1]) : -1
      const c = m ? Number(m[2]) : -1
      NOW += CFG.submitLatency
      if (CFG.judge0Down) throw new Error('judge0 unreachable')
      const spec = CFG.attempts[a] || {}
      if (spec.failCase === c) throw new Error('connection reset by peer')
      return `A${a}:C${c}`
    },
    result(cfg, token) {
      const m = /^A(\d+):C(\d+)$/.exec(String(token))
      const a = m ? Number(m[1]) : -1
      const c = m ? Number(m[2]) : -1
      NOW += CFG.pollLatency
      if (CFG.judge0Down) throw new Error('judge0 unreachable')
      const spec = CFG.attempts[a] || {}
      const key = a + ':' + c
      pollCounts[key] = (pollCounts[key] || 0) + 1
      // Slow Judge0: stays "In Queue" for N polls, then returns a verdict.
      if (spec.slowPolls && pollCounts[key] <= spec.slowPolls) {
        return { status_id: 1, exit_code: null, memory: null, time: null,
                 stdout: null, stderr: null, compile_output: null, message: null, token }
      }
      const kind = spec.kind || 'correct'
      const statusId = kind === 'ce' ? 6 : kind === 'wrong' ? 4 : 3
      return { status_id: statusId, exit_code: 0, memory: 1000, time: 0.01,
               stdout: kind === 'correct' ? `out-${c}` : 'nope',
               stderr: null, compile_output: statusId === 6 ? 'error: bad' : null,
               message: null, token }
    },
  },
}

// ================= SIMULATED POSTGRES =================
function makeDb(n) {
  const attempts = []
  for (let i = 0; i < n; i++) {
    attempts.push({
      id: 'attempt-' + String(i).padStart(3, '0'),
      status: 'evaluating',
      submitted_at: NOW - 120_000, // comfortably past the 60s staleness gate
      answers: [{
        answer_id: 'answer-' + String(i).padStart(3, '0'),
        question_type: 'coding',
        evaluation_status: 'Pending Evaluation',
        code: 'int main(){return 0;}',
        language: 'cpp',
        marks_awarded: 0,
        is_correct: null,
        apply_count: 0,
      }],
    })
  }
  return attempts
}

function makeAdmin(db, stats) {
  return {
    async rpc(name, args) {
      const arg = args || {}
      if (name === 'quizeazz_list_attempts_needing_programming_grading') {
        // UNCHANGED 60s staleness threshold, mirrored exactly.
        const rows = db.filter((a) =>
          a.submitted_at < NOW - 60_000 &&
          a.answers.some((x) => x.evaluation_status === 'Pending Evaluation' && x.code && x.code.trim() !== '')
        ).map((a) => ({ attempt_id: a.id }))
        return { data: rows, error: null }
      }
      if (name === 'quizeazz_judge0_pending_answers') {
        const a = db.find((x) => x.id === arg.p_attempt_id)
        stats.attemptsStarted.push(a.id)
        stats.startTimes.push(NOW - stats.sweepStart)
        if (a.throwsRpc) { stats.rpcThrows++; throw new Error('supabase rpc transport failure') }
        if (!a) return { data: null, error: { message: 'no attempt' } }
        if (a.status === 'in_progress') return { data: null, error: { message: 'still in progress' } }
        if (stats.inFlight === 0) stats.waves++
        a._t0 = NOW
        stats.inFlight++
        stats.maxInFlight = Math.max(stats.maxInFlight, stats.inFlight)
        const idx = Number(a.id.split('-')[1])
        const data = a.answers
          .filter((x) => x.evaluation_status === 'Pending Evaluation')
          .map((x) => ({
            answer_id: x.answer_id, question_id: 'q', language: 'cpp',
            source_code: x.code, stdin: null, marks_total: 10,
            tests: Array.from({ length: N_CASES }, (_, c) => ({
              case_order: c, stdin: `A${idx}:C${c}`, expected_stdout: `out-${c}`,
            })),
          }))
        return { data, error: null }
      }
      if (name === 'quizeazz_apply_judge0_verdict') {
        const a = db.find((x) => x.id === arg.p_attempt_id)
        if (!a) return { data: null, error: { message: 'no attempt' } }
        const ans = a.answers.find((x) => x.answer_id === arg.p_answer_id)
        if (!ans) return { data: null, error: { message: 'no answer' } }
        // The single-verdict guard that migration 20's FOR UPDATE protects.
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
        return { data: true, error: null }
      }
      if (name === 'quizeazz_finalize_attempt_grading') {
        const a = db.find((x) => x.id === arg.p_attempt_id)
        if (a) {
          const pending = a.answers.filter((x) => x.evaluation_status === 'Pending Evaluation').length
          a.score = a.answers.reduce((s, x) => s + (x.marks_awarded || 0), 0)
          a.status = pending > 0 ? 'evaluating' : 'evaluated'
          if (a._t0 !== undefined) {
            const d = NOW - a._t0
            stats.attemptMs.push(d)
            stats.maxAttemptMs = Math.max(stats.maxAttemptMs, d)
          }
        }
        stats.inFlight--
        return { data: true, error: null }
      }
      return { data: null, error: { message: 'unexpected rpc ' + name } }
    },
  }
}

// ================= DRIVER =================
async function sweepOnce(db, stats, cfg) {
  CFG = cfg
  stats.sweepStart = NOW
  stats.attemptsStarted = []
  stats.startTimes = []
  H.__LOGS.length = 0
  stats.attemptMs = []; stats.maxAttemptMs = 0; stats.waves = 0
  const admin = makeAdmin(db, stats)
  let done = false
  const p = H.runSweepReal(true, admin, { url: 'sim', token: 'sim' }).then((r) => { done = true; return r })
  await drive(() => done)
  const resp = await p
  await drive(() => done)
  for (const l of H.__LOGS) {
    if (l.error && String(l.error).startsWith('attempt raised')) {
      if (stats.rejections.length < 3) stats.rejections.push(String(l.error))
      stats.rejectionCount++
    }
  }
  return resp
}

function freshStats() {
  return { inFlight: 0, maxInFlight: 0, guardRejections: 0, rpcThrows: 0,
           attemptsStarted: [], startTimes: [], sweepStart: 0,
           rejections: [], rejectionCount: 0, waves: 0,
           attemptMs: [], maxAttemptMs: 0 }
}

function tally(db) {
  const byOutcome = {}
  let done = 0, maxApply = 0
  for (const a of db) {
    const s = a.answers[0].evaluation_status
    if (s !== 'Pending Evaluation') { done++; byOutcome[s] = (byOutcome[s] || 0) + 1 }
    maxApply = Math.max(maxApply, a.answers[0].apply_count)
  }
  return { done, byOutcome, maxApplyCount: maxApply }
}

function mkCfg(n, opts = {}) {
  const attempts = []
  for (let i = 0; i < n; i++) {
    attempts.push({
      kind: opts.kinds ? (opts.kinds[i] || 'correct') : 'correct',
      slowPolls: opts.slow && opts.slow.includes(i) ? (opts.slowPolls ?? 0) : 0,
      failCase: opts.fail && opts.fail.includes(i) ? (opts.failCase ?? 0) : null,
    })
  }
  return { attempts, submitLatency: opts.submitLatency ?? 120, pollLatency: opts.pollLatency ?? 900, judge0Down: !!opts.judge0Down }
}

async function drain(db, cfgFactory, stats, maxSweeps = 40) {
  const sweeps = []
  let cfg = cfgFactory(0)
  for (let i = 0; i < maxSweeps; i++) {
    const before = tally(db).done
    const resp = await sweepOnce(db, stats, cfg)
    const after = tally(db).done
    sweeps.push({
      index: i, swept: resp.swept, graded: resp.graded, failed: resp.failed,
      deferred: resp.deferred, budget_exhausted: resp.budget_exhausted,
      results: resp.results.length, virtualMs: NOW - stats.sweepStart,
      progress: after - before, done: after,
      maxInFlight: stats.maxInFlight, waves: stats.waves, maxAttemptMs: stats.maxAttemptMs,
      startedAfterDeadline: stats.startTimes.filter((t) => t >= H.__CONST.SWEEP_BUDGET_MS).length,
    })
    stats.maxInFlight = 0
    if (after === db.length) break
    NOW += 130_000 // next 2-minute cron tick; 60s gate re-opens
    cfg = cfgFactory(i + 1)
  }
  return sweeps
}

export async function main() {
  const out = { constants: H.__CONST, scenarios: [] }

  // ---- S1: 75 healthy attempts -----------------------------------------
  {
    const db = makeDb(75), stats = freshStats()
    const sweeps = await drain(db, () => mkCfg(75, { kinds: Array(75).fill('correct') }), stats)
    out.scenarios.push({ name: 'S1 75 healthy, all Correct', n: 75, sweeps,
      final: tally(db), totalVirtualMs: NOW, guardRejections: stats.guardRejections,
      rpcThrows: stats.rpcThrows, sampleRejection: stats.rejections[0] || null })
  }

  // ---- S2: mixed verdicts, slow attempts, Judge0 failures ----------------
  {
    const kinds = Array.from({ length: 75 }, (_, i) =>
      i % 10 === 3 ? 'wrong' : i % 10 === 7 ? 'ce' : 'correct')
    // Expected outcomes expressed in the REAL verdict vocabulary, so the
// comparison is exact on both counts and names.
const VERDICT_OF = { correct: 'Correct', wrong: 'Wrong Answer', ce: 'Compilation Error' }
const expected = {}
    for (const k of kinds) {
      const v = VERDICT_OF[k]
      expected[v] = (expected[v] || 0) + 1
    }
    const db = makeDb(75), stats = freshStats()
    const sweeps = await drain(db, (i) => mkCfg(75, {
      kinds,
      // slowPolls 12 x 1500ms x 5 cases = 90s -> exceeds the 90s attempt budget
      slow: i < 2 ? [5, 6, 40] : [],
      slowPolls: 12,
      fail: i < 2 ? [11] : [],
      failCase: 2,
    }), stats)
    out.scenarios.push({ name: 'S2 mixed + slow + failing', n: 75, sweeps,
      expected, final: tally(db), totalVirtualMs: NOW,
      guardRejections: stats.guardRejections,
      rpcThrows: stats.rpcThrows, sampleRejection: stats.rejections[0] || null })
  }

  // ---- S3: one attempt's RPC rejects outright --------------------------
  // The fault is TRANSIENT: it clears after two sweeps, so the attempt must
  // recover on a later sweep like any other pending work.
  {
    const db = makeDb(75)
    db[3].throwsRpc = true
    const stats = freshStats()
    const sweeps = []
    for (let i = 0; i < 8; i++) {
      if (i === 2) db[3].throwsRpc = false
      const before = tally(db).done
      const resp = await sweepOnce(db, stats, mkCfg(75, { kinds: Array(75).fill('correct') }))
      const after = tally(db).done
      sweeps.push({ index: i, swept: resp.swept, graded: resp.graded, failed: resp.failed,
        deferred: resp.deferred, budget_exhausted: resp.budget_exhausted,
        results: resp.results.length, virtualMs: NOW - stats.sweepStart,
        progress: after - before, done: after, maxInFlight: stats.maxInFlight,
        startedAfterDeadline: stats.startTimes.filter((t) => t >= H.__CONST.SWEEP_BUDGET_MS).length })
      stats.maxInFlight = 0
      if (after === 75) break
      NOW += 130_000
    }
    out.scenarios.push({ name: 'S3 one rejecting attempt', n: 75, sweeps,
      final: tally(db), totalVirtualMs: NOW, guardRejections: stats.guardRejections,
      rpcThrows: stats.rpcThrows, sampleRejection: stats.rejections[0] || null })
  }

  // ---- S4a: zero work ---------------------------------------------------
  {
    const db = makeDb(0), stats = freshStats()
    const sweeps = await drain(db, () => mkCfg(0, {}), stats, 3)
    out.scenarios.push({ name: 'S4a zero work', n: 0, sweeps,
      final: tally(db), totalVirtualMs: NOW, guardRejections: 0, rpcThrows: 0 })
  }

  // ---- S4b: very large backlog -----------------------------------------
  {
    const db = makeDb(250), stats = freshStats()
    const sweeps = await drain(db, () => mkCfg(250, { kinds: Array(250).fill('correct') }), stats, 60)
    out.scenarios.push({ name: 'S4b 250-attempt backlog', n: 250, sweeps,
      final: tally(db), totalVirtualMs: NOW, guardRejections: stats.guardRejections,
      rpcThrows: stats.rpcThrows })
  }

  // ---- S5: Judge0 fully down -------------------------------------------
  {
    const db = makeDb(75), stats = freshStats()
    const sweeps = []
    for (let i = 0; i < 3; i++) {
      const resp = await sweepOnce(db, stats, mkCfg(75, { judge0Down: true }))
      sweeps.push({ index: i, swept: resp.swept, graded: resp.graded, failed: resp.failed,
        deferred: resp.deferred, budget_exhausted: resp.budget_exhausted,
        results: resp.results.length, virtualMs: NOW - stats.sweepStart, progress: 0,
        done: tally(db).done, maxInFlight: stats.maxInFlight,
        startedAfterDeadline: stats.startTimes.filter((t) => t >= H.__CONST.SWEEP_BUDGET_MS).length })
      stats.maxInFlight = 0
      NOW += 130_000
    }
    // then Judge0 recovers
    const rest = await drain(db, () => mkCfg(75, { kinds: Array(75).fill('correct') }), stats, 20)
    out.scenarios.push({ name: 'S5 Judge0 down x3, then healthy', n: 75,
      sweeps: [...sweeps, ...rest.map((s, i) => ({ ...s, index: i + 3 }))],
      final: tally(db), totalVirtualMs: NOW, guardRejections: stats.guardRejections,
      rpcThrows: stats.rpcThrows })
  }

  // ---- S6-S9: scale ladder (1 / 10 / 25 / 50 pending attempts) ---------
  // Identical healthy workload at each scale, so sweep duration, budget
  // exhaustion and drain time are directly comparable. These answer ONLY
  // what the shipped wave/deadline logic does at a given N. They cannot
  // answer whether a real Judge0 tolerates the resulting concurrency.
  for (const n of [1, 10, 25, 50]) {
    const db = makeDb(n), stats = freshStats()
    const sweeps = await drain(db, () => mkCfg(n, { kinds: Array(n).fill('correct') }), stats, 20)
    out.scenarios.push({
      name: `S${n} scale ladder, ${n} pending attempt${n === 1 ? '' : 's'}`,
      n, sweeps,
      final: tally(db),
      totalVirtualMs: NOW,
      drainVirtualMs: sweeps.reduce((a, s) => a + (s.virtualMs || 0), 0),
      budgetExhaustedSweeps: sweeps.filter((s) => s.budget_exhausted).length,
      deferredTotal: sweeps.reduce((a, s) => a + (s.deferred || 0), 0),
      maxInFlight: Math.max(0, ...sweeps.map((s) => s.maxInFlight || 0)),
      startedAfterDeadline: sweeps.reduce((a, s) => a + (s.startedAfterDeadline || 0), 0),
      guardRejections: stats.guardRejections,
      rpcThrows: stats.rpcThrows,
    })
  }

  return out
}

const RESULT = await main()
process.stdout.write(JSON.stringify(RESULT))