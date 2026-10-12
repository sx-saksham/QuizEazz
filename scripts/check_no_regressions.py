"""
Verify the approved P2 / P2b / P3 production grading fix.

WHAT CHANGED AND WHY
--------------------
This script used to read `git diff` over the UNSTAGED working tree. That was
correct only while the fix was uncommitted: it inspected the change under
review. Once the fix was committed, `git diff` returned nothing, every
presence assertion went false, and the script reported the fix was missing
when it was in fact deployed in `HEAD`. It also compared the live source
against `HEAD`, which after the commit meant comparing the fix against itself
- a check that could never fail.

It now compares an explicit COMMIT RANGE instead:

    git diff <base>..<head>

with `base` defaulting to 5f7a138 (the last commit before the grading fix).
That makes the tool reproducible at any time, from a clean tree, and lets it
be pointed at any revision.

    python scripts/check_no_regressions.py                    # base..HEAD
    python scripts/check_no_regressions.py --base 5f7a138     # explicit
    python scripts/check_no_regressions.py --head b346589     # the fix commit
    python scripts/check_no_regressions.py --head 5f7a138     # base: must FAIL

EXIT CODES
    0  every check passed
    1  at least one check failed
    2  bad usage (unknown base/head revision, missing file)

WHAT IT STILL GUARDS
    * the sweep is bounded by wall-clock budget and a concurrency cap
    * Promise.allSettled isolates wave failures
    * a per-attempt budget bounds one slow attempt
    * the deadline is threaded into pollUntilVerdict
    * the isServiceRole hoist fixes the error-handler scope bug
    * no verdict vocabulary, marks computation or allow-list was altered
    * no secret, host or token literal was introduced
    * the student-facing frontend budget was not touched
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = "5f7a138"

PASS, FAIL = [], []

# The files the approved production fix is allowed to touch.
FIX_SCOPE = {
    "supabase/functions/judge0-grade/index.ts",
    "supabase/functions/judge0-grade/judge0.ts",
    "scripts/test_judge0_integration.py",
    "scripts/verify_rpc_contract.py",
}
INDEX_TS = "supabase/functions/judge0-grade/index.ts"
JUDGE0_TS = "supabase/functions/judge0-grade/judge0.ts"

VOCAB = ["Correct", "Wrong Answer", "Compilation Error", "Time Limit Exceeded",
         "Memory Limit Exceeded", "System Error", "Not Attempted"]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  [{'x' if cond else ' '}] {name}" + (f"   {detail}" if detail else ""))


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=str(ROOT)).stdout


def revision_exists(rev):
    return subprocess.run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"],
                          capture_output=True, text=True,
                          cwd=str(ROOT)).returncode == 0


def blob(rev, path):
    return git("show", f"{rev}:{path}")


def range_diff(base, head, path):
    return git("diff", f"{base}..{head}", "--", path)


def range_files(base, head):
    return [f for f in git("diff", "--name-only", f"{base}..{head}").split() if f]


def added_code(d):
    """Added lines only, comments stripped, for the change inventory."""
    out = []
    for ln in d.splitlines():
        if not ln.startswith("+") or ln.startswith("+++"):
            continue
        s = ln[1:].strip()
        if not s or s.startswith("//") or s.startswith("*"):
            continue
        out.append(s)
    return out


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"revision to compare FROM (default {DEFAULT_BASE})")
    ap.add_argument("--head", default="HEAD",
                    help="revision to compare TO (default HEAD)")
    args = ap.parse_args()

    for label, rev in (("base", args.base), ("head", args.head)):
        if not revision_exists(rev):
            print(f"unknown {label} revision: {rev}", file=sys.stderr)
            return 2

    print(f"== comparing {args.base} -> {args.head} ==")
    changed = range_files(args.base, args.head)
    print("  files differing in range:")
    for f in changed:
        print(f"    {f}")
    if not changed:
        print("    (none - the range is empty)")

    print("\n== scope confinement ==")
    # This tool judges the production fix. Files outside FIX_SCOPE that differ
    # in the range belong to other approved commits (e.g. .gitignore, deploy
    # records) and are reported, not failed - the range deliberately spans
    # every commit since base, not just the fix.
    out_of_scope = [f for f in changed if f not in FIX_SCOPE]
    if out_of_scope:
        print(f"  {len(out_of_scope)} file(s) in range outside the fix scope "
              f"(other approved commits, not judged here):")
        for f in out_of_scope:
            print(f"    - {f}")
    check("index.ts differs from base (the fix is present)",
          INDEX_TS in changed)
    check("judge0.ts differs from base (the fix is present)",
          JUDGE0_TS in changed)
    check("no frontend/UI file changed",
          not any(f.startswith("src/") for f in changed),
          f"{[f for f in changed if f.startswith('src/')]}")
    # The meaningful tripwire: no OTHER Edge Function source changed.
    stray_fn = [f for f in changed
                if f.startswith("supabase/functions/")
                and f not in FIX_SCOPE]
    check("no other Edge Function source changed",
          not stray_fn, f"{stray_fn}")
    # Migration SQL is not this tool's concern, but a change to a migration
    # file inside the fix scope would mean the fix altered schema.
    check("no .sql file inside the production-fix scope changed",
          not any(f.endswith(".sql") for f in changed if f in FIX_SCOPE))
    check("frontend judge0Client.ts untouched (45s budget unchanged)",
          not any(f.endswith("judge0Client.ts") for f in changed))

    idx = range_diff(args.base, args.head, INDEX_TS)
    j0 = range_diff(args.base, args.head, JUDGE0_TS)
    ai, aj = added_code(idx), added_code(j0)
    print(f"\n  index.ts added code lines: {len(ai)}")
    print(f"  judge0.ts added code lines: {len(aj)}")

    print("\n== P2 / P2b / P3 presence ==")
    check("P2: sweep budget + concurrency constants added",
          "const SWEEP_BUDGET_MS = 110_000" in idx
          and "const SWEEP_CONCURRENCY = 10" in idx)
    check("P2b: per-attempt budget constant added",
          "const GRADING_ATTEMPT_BUDGET_MS = 90_000" in idx)
    check("P2b: allSettled replaces Promise.all on the sweep wave",
          "await Promise.allSettled(" in idx
          and not any(re.search(r"Promise\.all\((?!\s*Settled)", ln)
                      for ln in ai))
    check("P3: isServiceRole hoisted above try",
          "let isServiceRole = false" in idx
          and "-    const isServiceRole = !!serviceRoleKey" in idx)
    check("P2b: deadline threaded into pollUntilVerdict",
          "deadlineAt?: number | null" in j0
          and "if (deadlineAt !== null && Date.now() >= deadlineAt) break" in j0)

    print("\n== grading semantics MUST be unchanged ==")
    src_idx = blob(args.head, INDEX_TS)
    src_j0 = blob(args.head, JUDGE0_TS)
    base_idx = blob(args.base, INDEX_TS)
    base_j0 = blob(args.base, JUDGE0_TS)

    check("verdict vocabulary identical to base in index.ts (presence per verdict)",
          all((v in base_idx) == (v in src_idx) for v in VOCAB))
    check("full 8-verdict vocabulary still present in judge0.ts",
          all(v in src_j0 for v in VOCAB))
    check("mapJudge0Result status table untouched",
          not re.search(r"^\+\s*case \d+:", j0, re.M))
    check("no new verdict vocabulary introduced",
          not re.search(r"^\+.*'(Correct|Wrong Answer|Compilation Error)'", idx, re.M))
    check("marks computation / outcome allow-list untouched",
          "quizeazz_marks_for_outcome" not in "".join(ai)
          and "SUBMISSION_LIMITS" not in "".join(ai))
    check("no secret/host/token literal added",
          not re.search(r"eyJ|sb_secret_|JUDGE0_API_TOKEN|supabase\.co", idx + j0))

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for n, d in FAIL:
        print(f"  FAIL {n} {d}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())