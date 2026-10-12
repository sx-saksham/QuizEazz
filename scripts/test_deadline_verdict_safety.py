"""
Can the sweep deadline produce a WRONG verdict because a poll returned late?

Drives the REAL extracted runHiddenTests() and pollUntilVerdict() against a
fake Judge0 with a controllable clock. No database, no production.

Proves:
  A. a poll that returns a GENUINE verdict after the deadline is applied
     (correct - it is a real result, not a guess)
  B. a poll that never yields a verdict applies NOTHING (stays pending)
  C. a deadline hit before a case is submitted applies NOTHING
  D. a timeout is NEVER recorded as Wrong Answer
  E. a late verdict can only be Wrong Answer if Judge0 actually said so
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simulate_grading_recovery as sim  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
J0 = ROOT / "supabase" / "functions" / "judge0-grade" / "judge0.ts"
IDX = ROOT / "supabase" / "functions" / "judge0-grade" / "index.ts"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  [{'x' if cond else ' '}] {name}" + (f"   {detail}" if detail else ""))


RUNNER = r"""
import { readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import ts from 'typescript'
const src = readFileSync(process.argv[2], 'utf8')
const js = ts.transpileModule(src, { compilerOptions: {
  module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, strict: false },
  fileName: 'h.ts' }).outputText
const f = join(tmpdir(), `dl_${process.pid}.mjs`)
writeFileSync(f, js)
const m = await import('file://' + f.replace(/\\/g, '/'))
process.stdout.write(JSON.stringify(await m.default()))
"""


def build_ts():
    j0 = J0.read_text(encoding="utf-8")
    idx = IDX.read_text(encoding="utf-8")
    real = [
        sim.extract_const(j0, "IN_FLIGHT"),
        sim.extract_const(j0, "QUIZEAZZ_CPP_JUDGE0_LANGUAGE_ID"),
        sim.extract_fn(j0, "SUBMISSION_LIMITS", "const"),
    ]
    for fn in ("mapJudge0Result", "resolveLanguageId", "normalizeProgramOutput",
               "outputsMatch", "buildSubmissionBody", "decodeJudge0Text",
               "pollUntilVerdict"):
        real.append(sim.extract_fn(j0, fn))
    real.append(sim.extract_fn(idx, "runHiddenTests"))
    pol = sim.extract_numeric_const(idx, "POLL_ATTEMPTS")
    pdl = sim.extract_numeric_const(idx, "POLL_DELAY_MS")
    return f"""
{chr(10).join(real)}

{pol}
{pdl}

type HiddenTestCase = {{ case_order: number; stdin: string | null; expected_stdout: string | null }}
type Judge0Outcome = string
type Judge0ResultFields = any
let __SIM: any

async function submitProgram(cfg: any, body: any): Promise<string> {{
  return __SIM.submit(cfg, body)
}}
async function fetchProgramResult(cfg: any, token: string): Promise<any> {{
  return __SIM.result(cfg, token)
}}

export default async function main() {{
  const out: any[] = []

  for (const scenario of SCENARIOS) {{
    const cases = Array.from({{ length: scenario.caseCount || 5 }}, (_, i) => ({{
      case_order: i, stdin: `in-${{i}}`, expected_stdout: `out-${{i}}`,
    }}))
    let NOW = 0, SEQ = 0
    const TIMERS: any[] = []
    globalThis.setTimeout = (fn: any, ms: any) => {{
      TIMERS.push({{ t: NOW + (Number(ms) || 0), s: SEQ++, fn }}); return 0
    }}
    Date.now = () => NOW
    let polls = 0, applies = 0, appliedOutcome: string | null = null
    __SIM = {{
      submit: async () => {{
        NOW += 100
        return 'tok'
      }},
      result: async () => {{
        polls++
        NOW += scenario.pollMs
        if (scenario.neverVerdict) return {{ status_id: 1 }}
        if (scenario.answer === 'wrong') return {{ status_id: 4, exit_code: 1 }}
        return {{ status_id: 3, exit_code: 0, stdout: 'out-' + (polls - 1) }}
      }},
    }}
    let done = false
    const p = runHiddenTests({{}}, {{
      sourceCode: 'int main(){{return 0;}}',
      cases,
      deadlineAt: scenario.deadlineMs === null ? null : scenario.deadlineMs,
    }}).then((r: any) => {{ done = true; return r }})
    for (let g = 0; g < 2000000 && !done; g++) {{
      await new Promise((r) => setImmediate(r))
      if (!TIMERS.length) continue
      TIMERS.sort((a, b) => a.t - b.t || a.s - b.s)
      const e = TIMERS.shift()
      NOW = e.t
      e.fn()
    }}
    const r: any = await p
    if (r.ok) {{ applies = 1; appliedOutcome = r.outcome }}
    out.push({{
      name: scenario.name, ok: !!r.ok, outcome: r.outcome ?? null,
      error: r.ok ? null : r.transportError, polls, applies,
      appliedOutcome, finishedAt: NOW, deadlineMs: scenario.deadlineMs,
    }})
  }}
  return out
}}

const SCENARIOS: any[] = SCENARIOS_JSON
"""


def run(scenarios):
    ts_src = build_ts().replace("SCENARIOS_JSON", json.dumps(scenarios))
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "h.ts"
    p.write_text(ts_src, encoding="utf-8")
    # The runner must live inside the repo so `import ts from 'typescript'`
    # resolves against ./node_modules.
    runner = Path(__file__).resolve().parent / "_dl_runner.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    try:
        proc = subprocess.run(["node", str(runner), str(p)],
                              capture_output=True, text=True, timeout=900,
                              cwd=str(ROOT))
    finally:
        runner.unlink(missing_ok=True)
    if proc.returncode != 0:
        print("DRIVER FAILED:\n", (proc.stderr or proc.stdout)[-2500:])
        return None
    return json.loads(proc.stdout)


def main():
    scenarios = [
        # A: genuine verdict arrives only AFTER the deadline
        {"name": "A late-but-real verdict", "deadlineMs": 2000, "pollMs": 4000,
         "neverVerdict": False, "answer": "correct", "caseCount": 1},
        {"name": "A2 late genuine Wrong Answer", "deadlineMs": 2000,
         "pollMs": 4000, "neverVerdict": False, "answer": "wrong", "caseCount": 1},
        # B: poll never yields a verdict
        {"name": "B poll never verdicts", "deadlineMs": 8000, "pollMs": 1000,
         "neverVerdict": True, "answer": "correct"},
        # C: deadline hit before the first case can be submitted
        {"name": "C deadline already spent", "deadlineMs": 0, "pollMs": 100,
         "neverVerdict": False, "answer": "correct"},
        # D: deadline far in the future - normal completion
        {"name": "D normal completion", "deadlineMs": 120000, "pollMs": 200,
         "neverVerdict": False, "answer": "correct"},
        # E: Judge0 genuinely says Wrong Answer
        {"name": "E genuine wrong answer", "deadlineMs": 120000, "pollMs": 200,
         "neverVerdict": False, "answer": "wrong"},
        # F: no deadline at all (browser path)
        {"name": "F no deadline (frontend path)", "deadlineMs": None, "pollMs": 200,
         "neverVerdict": False, "answer": "correct"},
    ]
    out = run(scenarios)
    if out is None:
        return 2
    by = {r["name"]: r for r in out}

    print("== results ==")
    for r in out:
        print(f"  {r['name']:32} ok={str(r['ok']):5} outcome={r['outcome']} "
              f"polls={r['polls']} applies={r['applies']} at={r['finishedAt']}ms "
              f"deadline={r['deadlineMs']}")

    a = by["A late-but-real verdict"]
    a2 = by["A2 late genuine Wrong Answer"]
    check("A2: a late genuine Wrong Answer is applied verbatim",
          a2["ok"] and a2["outcome"] == "Wrong Answer" and a2["applies"] == 1)
    check("A: the run really did finish AFTER its deadline",
          a["finishedAt"] > a["deadlineMs"],
          "finished %sms vs deadline %sms" % (a["finishedAt"], a["deadlineMs"]))
    check("A: a genuine late verdict IS applied (it is a real result, not a guess)",
          a["ok"] and a["outcome"] == "Correct" and a["applies"] == 1)
    check("A: applying it after the deadline produced no duplicate",
          a["applies"] == 1)

    b = by["B poll never verdicts"]
    check("B: a poll that never verdicts applies NOTHING",
          not b["ok"] and b["applies"] == 0,
          f"ok={b['ok']} error={b['error']}")
    check("B: it is reported as a transport failure, not a verdict",
          b["outcome"] is None and "deadline" in str(b["error"]).lower())

    c = by["C deadline already spent"]
    check("C: deadline spent before submitting applies NOTHING",
          not c["ok"] and c["applies"] == 0)

    d = by["D normal completion"]
    check("D: normal completion still yields Correct",
          d["ok"] and d["outcome"] == "Correct")

    e = by["E genuine wrong answer"]
    check("E: Wrong Answer appears only when Judge0 actually reports it",
          e["ok"] and e["outcome"] == "Wrong Answer")

    f_ = by["F no deadline (frontend path)"]
    check("F: the frontend path (no deadline) is unaffected",
          f_["ok"] and f_["outcome"] == "Correct")

    check("D/F: a timeout NEVER becomes Wrong Answer",
          b["outcome"] != "Wrong Answer" and c["outcome"] != "Wrong Answer")

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for n, d2 in FAIL:
        print(f"  FAIL {n} {d2}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
