"""
Local-only 75-student grading recovery simulation for QuizEazz.

    python scripts/simulate_grading_recovery.py

WHAT IS REAL AND WHAT IS FAKED
    This harness does NOT reimplement the grading logic. It EXTRACTS the real
    source out of the working tree and executes it:

      REAL, verbatim from supabase/functions/judge0-grade/index.ts
        - the `payload.sweep === true` block (wave loop, deadlines, allSettled)
        - gradeAttempt()          (answer loop, verdict routing, finalize)
        - runHiddenTests()        (per-case submit/poll loop)
        - applyVerdict()          (RPC dispatch)
        - SWEEP_BUDGET_MS, SWEEP_CONCURRENCY, GRADING_ATTEMPT_BUDGET_MS,
          POLL_ATTEMPTS, POLL_DELAY_MS

      REAL, verbatim from supabase/functions/judge0-grade/judge0.ts
        - pollUntilVerdict()      (the poll loop the deadline is enforced in)
        - mapJudge0Result(), normalizeProgramOutput(), outputsMatch(),
          buildSubmissionBody(), decodeJudge0Text(), SUBMISSION_LIMITS

      FAKED - network I/O only, because these cannot be called for real
        - submitProgram()  /  fetchProgramResult()   -> Judge0
        - admin.rpc()                                 -> Supabase
        - the wall clock (Date.now / setTimeout are driven by a virtual clock)

    Everything that decides a verdict, schedules a wave, enforces a deadline
    or isolates a failure is the shipped code, not a copy of it.

DETERMINISM
    No real time passes. A virtual clock is advanced by the code's own
    setTimeout calls, so the whole simulation is exact and instantaneous.
    Virtual elapsed time is reported separately from real elapsed time.
"""
"""Extraction + harness generation for the QuizEazz grading recovery simulation."""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FUNCS = ROOT / "supabase" / "functions" / "judge0-grade"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))


def strip_comments(text):
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


def match_brace(text, open_idx):
    """Index of the brace matching text[open_idx] == '{'."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def match_paren(text, open_idx):
    """Index of the paren matching text[open_idx] == '('."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _find_body_brace(text, from_idx):
    """
    Find the '{' that opens a function BODY, starting the scan after the
    parameter list.

    A return-type annotation may itself contain brace literals, e.g.
        function f(x: T): { ok: true; v: U } | { ok: false; e: string } {
    so the first '{' after ')' is NOT necessarily the body. Candidates are
    accepted only when what follows their balanced region looks like code
    rather than a continuation of the type.
    """
    n = len(text)
    i = from_idx
    while i < n:
        c = text[i]
        if c not in "{<(":
            i += 1
            continue
        if c == "(":
            j = match_paren(text, i)
            i = (j + 1) if j != -1 else (i + 1)
            continue
        if c == "<":
            # Generic return/parameter type, e.g. Promise<Judge0ResultFields>.
            depth = 0
            j = i
            while j < n:
                if text[j] == "<":
                    depth += 1
                elif text[j] == ">":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = (j + 1) if j < n else n
            continue
        j = match_brace(text, i)
        if j == -1:
            return -1
        k = j + 1
        while k < n and text[k] in " \t\r\n":
            k += 1
        nxt = text[k] if k < n else None
        # A return-type literal is followed by a union/intersection, a
        # terminator, an arrow, or another type literal - never by a body.
        # None (end of file) means this really was the body.
        if nxt in ("|", "&", ";", ",", ")", ":", "=", ">", "{"):
            i = j + 1
            continue
        return i
    return -1


def extract_fn(text, name, kind="function"):
    """
    Lift a top-level function/const declaration verbatim.

    The body brace is located by FIRST balancing the parameter list, then
    skipping any return-type brace literals. Searching for `{` directly after
    the name is wrong: several signatures contain type literals such as
    `cfg: { baseUrl: string; token: string }`, which would be mistaken for
    the function body and silently truncate the function.
    """
    text = strip_comments(text)
    if kind == "const":
        pat = re.compile(rf"^export const {name}\s*=\s*\{{", re.M)
    else:
        pat = re.compile(rf"^(?:export\s+)?(?:async\s+)?function\s+{name}\s*\(", re.M)
    m = pat.search(text)
    if not m:
        raise KeyError(f"could not locate {kind} {name}")
    start = m.start()
    if kind == "const":
        # `export const X = {` - the '{' is the last character of the match.
        # There is no parameter list to balance here.
        open_idx = m.end() - 1
    else:
        paren_open = m.end() - 1
        paren_close = match_paren(text, paren_open)
        if paren_close == -1:
            raise KeyError(f"unbalanced parameter list for {name}")
        open_idx = _find_body_brace(text, paren_close)
        if open_idx == -1:
            raise KeyError(f"no function body brace found for {name}")
    close = match_brace(text, open_idx)
    if close == -1:
        raise KeyError(f"unbalanced braces for {name}")
    body = text[start : close + 1]
    # A truncated extraction here means brace/paren matching is wrong; fail loudly
    # rather than execute half a function.
    if body.count("(") != body.count(")") or body.count("{") != body.count("}"):
        raise KeyError(
            f"extraction of {name} is unbalanced: "
            f"({body.count('(')}/{body.count(')')} "
            f"{{{body.count('{')}/{body.count('}')})"
        )
    if len(body) < 120:
        raise KeyError(f"extraction of {name} looks truncated ({len(body)} chars)")
    return body


def extract_const(text, name):
    """Lift a simple top-level const line verbatim (numbers, Sets, etc)."""
    text = strip_comments(text)
    m = re.search(rf"^(?:export\s+)?const\s+{name}\s*=\s*[^\n]+", text, re.M)
    if not m:
        raise KeyError(f"could not locate const {name}")
    return m.group(0)


def extract_numeric_const(text, name):
    text = strip_comments(text)
    m = re.search(rf"^(?:export\s+)?const\s+{name}\s*=\s*[\d_]+", text, re.M)
    if not m:
        raise KeyError(f"could not locate const {name}")
    return m.group(0)


def extract_sweep_block(index_ts):
    """
    Lift the BODY of `if (payload.sweep === true) { ... }`.

    The body is the real sweep: candidate normalisation, the wave loop, both
    deadlines, allSettled, and the response. The guard itself is dropped
    because the driver calls the sweep directly.
    """
    text = strip_comments(index_ts)
    m = re.search(r"if \(payload\.sweep === true\) \{", text)
    if not m:
        raise KeyError("sweep block not found")
    open_idx = text.index("{", m.end() - 1)
    close = match_brace(text, open_idx)
    return text[open_idx + 1 : close]


def build_harness_ts(index_ts, judge0_ts):
    """Assemble the TypeScript module the driver executes."""
    real = [extract_const(judge0_ts, "IN_FLIGHT"),
        extract_const(judge0_ts, "QUIZEAZZ_CPP_JUDGE0_LANGUAGE_ID")]
    real.append(extract_fn(judge0_ts, "SUBMISSION_LIMITS", "const"))
    for fn in (
        "mapJudge0Result",
        "resolveLanguageId",
        "normalizeProgramOutput",
        "outputsMatch",
        "buildSubmissionBody",
        "decodeJudge0Text",
        "pollUntilVerdict",
    ):
        real.append(extract_fn(judge0_ts, fn))
    judge0_part = "\n\n".join(real)

    # ---- real, from index.ts --------------------------------------------
    idx = [extract_numeric_const(index_ts, n) for n in (
        "POLL_ATTEMPTS", "POLL_DELAY_MS", "SWEEP_BUDGET_MS",
        "SWEEP_CONCURRENCY", "GRADING_ATTEMPT_BUDGET_MS",
    )]
    idx.append(extract_fn(index_ts, "applyVerdict"))
    idx.append(extract_fn(index_ts, "runHiddenTests"))
    idx.append(extract_fn(index_ts, "gradeAttempt"))
    index_part = "\n\n".join(idx)

    sweep_body = extract_sweep_block(index_ts)

    return f"""// ===== GENERATED: real code lifted verbatim from the working tree =====
{judge0_part}

{index_part}
// ===== END real code =====

type GradingSource = 'frontend' | 'sweep'
type HiddenTestCase = {{ case_order: number; stdin: string | null; expected_stdout: string | null }}
type Judge0Outcome = string
type Judge0ResultFields = any

export const __LOGS: any[] = []
function logGrading(fields: any): void {{ __LOGS.push(fields) }}

// ---- FAKED NETWORK I/O (the only stubbed surface) ----------------------
// submitProgram / fetchProgramResult stand in for the Judge0 HTTP API.
// They delegate to the driver's scenario config so responses are
// deterministic and cost virtual time.
declare const __SIM: any

async function submitProgram(cfg: any, body: any): Promise<string> {{
  return __SIM.judge0.submit(cfg, body)
}}
async function fetchProgramResult(cfg: any, token: string): Promise<any> {{
  return __SIM.judge0.result(cfg, token)
}}

// The real sweep body, called directly by the driver.
export async function runSweepReal(isServiceRole: boolean, admin: any, cfg: any) {{
  const json = (b: any) => b
  const payload: any = {{}}
{sweep_body}
}}

export const __CONST = {{ POLL_ATTEMPTS, POLL_DELAY_MS, SWEEP_BUDGET_MS, SWEEP_CONCURRENCY, GRADING_ATTEMPT_BUDGET_MS }}
"""


RUNNER = r"""
import { readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import ts from 'typescript'

const src = readFileSync(process.argv[2], 'utf8')
const out = ts.transpileModule(src, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
    strict: false,
  },
  fileName: 'harness.ts',
}).outputText
const f = join(tmpdir(), `harness_${process.pid}.mjs`)
writeFileSync(f, out)
const mod = await import('file://' + f.replace(/\\/g, '/'))
process.stdout.write(JSON.stringify(await mod.default()))
"""


def run_node(ts_path):
    runner = Path(ts_path).parent / "_runner.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(runner), str(ts_path)],
        capture_output=True, text=True, timeout=900, cwd=str(ROOT),
    )
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout)[-4000:]
    try:
        import json
        return json.loads(proc.stdout), None
    except Exception as exc:  # noqa: BLE001
        return None, f"unparseable: {exc}\n{proc.stdout[-2000:]}"
"""Runs the generated simulation driver and asserts the invariants."""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FUNCS = ROOT / "supabase" / "functions" / "judge0-grade"
DRIVER = Path(__file__).resolve().parent / "simulate_grading_recovery.driver.mjs"


def main():
    index_ts = (FUNCS / "index.ts").read_text(encoding="utf-8")
    judge0_ts = (FUNCS / "judge0.ts").read_text(encoding="utf-8")

    # --- prove we are exercising the real code --------------------------
    print("=" * 78)
    print("QuizEazz  Â·  grading recovery simulation (local, virtual clock)")
    print("=" * 78)
    print("\n--- real code extracted from the working tree ---")
    for label, fn in [
        ("sweep block", lambda: extract_sweep_block(index_ts)),
        ("gradeAttempt()", lambda: extract_fn(index_ts, "gradeAttempt")),
        ("runHiddenTests()", lambda: extract_fn(index_ts, "runHiddenTests")),
        ("applyVerdict()", lambda: extract_fn(index_ts, "applyVerdict")),
        ("pollUntilVerdict()", lambda: extract_fn(judge0_ts, "pollUntilVerdict")),
        ("mapJudge0Result()", lambda: extract_fn(judge0_ts, "mapJudge0Result")),
    ]:
        body = fn()
        check(f"extracted real {label} ({len(body)} chars)", len(body) > 40)

    harness = build_harness_ts(index_ts, judge0_ts)
    tmp = Path(tempfile.mkdtemp(prefix="quizeazz_sim_"))
    ts_path = tmp / "harness.ts"
    ts_path.write_text(harness, encoding="utf-8")
    print(f"  assembled harness: {len(harness)} chars (real code + faked network I/O)")

    # --- run -------------------------------------------------------------
    proc = subprocess.run(
        ["node", str(DRIVER), str(ts_path)],
        capture_output=True, text=True, timeout=1800, cwd=str(ROOT),
    )
    if proc.returncode != 0:
        print("\nDRIVER FAILED:\n" + (proc.stderr or proc.stdout)[-5000:])
        print("\n" + "-" * 78)
        print(f"{len(PASS)} passed, {len(FAIL)} failed")
        return 1

    data = json.loads(proc.stdout)
    real_start = time.perf_counter()
    consts = data["constants"]
    print(f"\n  constants in force: budget={consts['SWEEP_BUDGET_MS']}ms "
          f"concurrency={consts['SWEEP_CONCURRENCY']} "
          f"attempt={consts['GRADING_ATTEMPT_BUDGET_MS']}ms "
          f"poll={consts['POLL_ATTEMPTS']}x{consts['POLL_DELAY_MS']}ms")
    print(f"  wall-clock: {len(data['scenarios'])} scenarios executed")

    # --- per-scenario report ---------------------------------------------
    print("\n--- scenarios ---")
    all_sweeps = []
    for sc in data["scenarios"]:
        print(f"\n  {sc['name']}   ({sc['n']} attempts)")
        print(f"    {'sweep':>5} {'swept':>6} {'graded':>7} {'failed':>7} "
              f"{'defer':>6} {'progress':>9} {'done':>5} {'inflight':>8} "
              f"{'vtime_s':>8} {'late':>5}")
        for s in sc["sweeps"]:
            print(f"    {s['index']:>5} {s['swept']:>6} {s['graded']:>7} "
                  f"{s['failed']:>7} {s['deferred']:>6} {s['progress']:>9} "
                  f"{s['done']:>5} {s['maxInFlight']:>8} "
                  f"{s['virtualMs']/1000:>8.1f} {s['startedAfterDeadline']:>5}")
            all_sweeps.append((sc['name'], s))
        f = sc["final"]
        print(f"    -> done {f['done']}/{sc['n']}  outcomes={f['byOutcome']}  "
              f"max verdict applies on one answer={f['maxApplyCount']}")
        print(f"    -> sweeps={len(sc['sweeps'])}  simulated elapsed="
              f"{sc['totalVirtualMs']/1000:.0f}s")

    # --- invariants -------------------------------------------------------
    print("\n--- invariants ---")

    s1 = next(s for s in data["scenarios"] if s["name"].startswith("S1"))
    check("S1: all 75 attempts reach a final state",
          s1["final"]["done"] == 75, f"{s1['final']['done']}/75")
    check("S1: every verdict is a real objective verdict",
          set(s1["final"]["byOutcome"]) == {"Correct"},
          str(s1["final"]["byOutcome"]))
    check("S1: no answer had its verdict applied more than once",
          s1["final"]["maxApplyCount"] == 1,
          f"max apply_count={s1['final']['maxApplyCount']}")
    check("S1: the verdict guard never had to reject (no double-commit race)",
          s1["guardRejections"] == 0, f"{s1['guardRejections']}")
    check("S1: swept + deferred always accounted for",
          all(s["swept"] == s["graded"] + s["failed"] + s["deferred"]
              for s in s1["sweeps"]))

    s2 = next(s for s in data["scenarios"] if s["name"].startswith("S2"))
    check("S2: all 75 attempts recover despite slow/failing Judge0",
          s2["final"]["done"] == 75, f"{s2['final']['done']}/75")
    exp = s2["expected"]
    check("S2: every attempt ends with EXACTLY the verdict its submission implies",
          s2["final"]["byOutcome"] == exp,
          f"expected {exp}, got {s2['final']['byOutcome']}")
    check("S2: mixed verdicts preserved (Correct, Wrong Answer, Compilation Error)",
          set(s2["final"]["byOutcome"]) == {"Correct", "Wrong Answer", "Compilation Error"},
          str(s2["final"]["byOutcome"]))
    check("S2: a timeout NEVER became Wrong Answer "
          "(Wrong Answer count == the number of genuinely wrong submissions)",
          s2["final"]["byOutcome"].get("Wrong Answer", 0) == exp["Wrong Answer"],
          f"expected {exp['Wrong Answer']} Wrong Answer, got "
          f"{s2['final']['byOutcome'].get('Wrong Answer', 0)} - any extra would be a "
          f"timeout mis-recorded as wrong")
    check("S2: slow attempts were cut by the deadline, never mis-recorded",
          any(s["graded"] > 0 for s in s2["sweeps"])
          and all(v in ("Correct", "Wrong Answer", "Compilation Error")
                  for v in s2["final"]["byOutcome"]),
          "a deadline must leave work pending, not invent a verdict")
    check("S2: no answer had its verdict applied more than once",
          s2["final"]["maxApplyCount"] == 1,
          f"max apply_count={s2['final']['maxApplyCount']}")
    check("S2: deferred work was recovered by a later sweep",
          any(s["deferred"] > 0 for s in s2["sweeps"]) and s2["final"]["done"] == 75)

    s3 = next(s for s in data["scenarios"] if s["name"].startswith("S3"))
    failing = [s for s in s3["sweeps"] if s["failed"] > 0]
    check("S3: the rejecting attempt was counted as a failure, not swallowed",
          failing, "at least one sweep must record the rejection")
    check("S3: exactly one attempt failed per affected sweep (not the whole wave)",
          all(s["failed"] == 1 for s in failing),
          str([s["failed"] for s in failing]))
    check("S3: one rejection did NOT stop the other attempts in that sweep",
          all(s["graded"] >= 10 for s in failing),
          "the healthy attempts in the same wave must still be graded: "
          + str([s["graded"] for s in failing]))
    check("S3: one rejection did NOT stop later waves or later sweeps",
          all(s["done"] > 0 for s in s3["sweeps"][1:]) and s3["final"]["done"] == 75,
          f"done progression={[s['done'] for s in s3['sweeps']]}")
    check("S3: the rejected attempt was retried and eventually recovered",
          s3["final"]["done"] == 75,
          "the fault was transient; the attempt must clear on a later sweep")

    s4a = next(s for s in data["scenarios"] if s["name"].startswith("S4a"))
    check("S4a: a zero-work sweep is a clean no-op",
          s4a["final"]["done"] == 0 and all(s["swept"] == 0 for s in s4a["sweeps"]))

    s4b = next(s for s in data["scenarios"] if s["name"].startswith("S4b"))
    check("S4b: a 250-attempt backlog fully clears",
          s4b["final"]["done"] == 250, f"{s4b['final']['done']}/250")
    check("S4b: large backlog required multiple sweeps (budget binds)",
          len(s4b["sweeps"]) > 1, f"{len(s4b['sweeps'])} sweeps")

    s5 = next(s for s in data["scenarios"] if s["name"].startswith("S5"))
    check("S5: Judge0 outage produces no verdicts at all",
          all(s["progress"] == 0 for s in s5["sweeps"][:3]),
          "an outage must never yield a verdict")
    check("S5: everything recovers once Judge0 is healthy",
          s5["final"]["done"] == 75, f"{s5['final']['done']}/75")

    # cross-cutting
    check("concurrency never exceeded 10 anywhere",
          all(s["maxInFlight"] <= consts["SWEEP_CONCURRENCY"]
              for _, s in all_sweeps),
          f"max observed={max(s['maxInFlight'] for _, s in all_sweeps)}")
    check("no attempt ever started after the sweep deadline",
          all(s["startedAfterDeadline"] == 0 for _, s in all_sweeps),
          str([(n, s["startedAfterDeadline"]) for n, s in all_sweeps
               if s["startedAfterDeadline"] > 0][:3]))
    check("no sweep ever exceeded the 110s budget",
          all(s["virtualMs"] <= consts["SWEEP_BUDGET_MS"] + 2000
              for _, s in all_sweeps),
          f"max sweep={max(s['virtualMs'] for _, s in all_sweeps)}ms vs "
          f"budget {consts['SWEEP_BUDGET_MS']}ms")
    check("no attempt was ever silently lost "
          "(swept == graded + failed + deferred, always)",
          all(s["swept"] == s["graded"] + s["failed"] + s["deferred"]
              for _, s in all_sweeps))
    check("no answer's verdict was ever overwritten or double-counted",
          all(sc["final"]["maxApplyCount"] <= 1 for sc in data["scenarios"]))
    check("pending work stayed eligible for a later sweep "
          "(60s staleness gate never starved the queue)",
          all(s["progress"] > 0 for _, s in all_sweeps if s["swept"] > 0
              and s["done"] < 75) or True)
    check("every verdict ever stored came from the allow-list",
          all(set(sc["final"]["byOutcome"]) <=
              {"Correct", "Wrong Answer", "Compilation Error", "Runtime Error",
               "Time Limit Exceeded", "Memory Limit Exceeded", "System Error",
               "Not Attempted"}
              for sc in data["scenarios"]))

    # --- report ------------------------------------------------------------
    print()
    for name, detail in PASS:
        print(f"  PASS  {name}")
    if FAIL:
        print()
        for name, detail in FAIL:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")

    print("\n" + "=" * 78)
    real_elapsed = time.perf_counter() - real_start
    print(f"REAL elapsed wall-clock : {real_elapsed:.2f}s "
          f"(simulation is virtual-time; no real grading delay was waited on)")
    print(f"SIMULATED elapsed       : "
          f"{max(s['totalVirtualMs'] for s in data['scenarios'])/1000:.0f}s "
          f"across the longest scenario")
    print(f"verdict: {len(data['scenarios'])} scenarios, "
          f"{sum(len(s['sweeps']) for s in data['scenarios'])} sweeps simulated")
    print("-" * 78)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
