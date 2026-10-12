"""Static sweep-loop invariants for P2/P2b, plus typecheck baseline comparison."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
F = (ROOT / "supabase/functions/judge0-grade/index.ts").read_text(encoding="utf-8")
J = (ROOT / "supabase/functions/judge0-grade/judge0.ts").read_text(encoding="utf-8")

CHECKS = [
    ("bounded total sweep budget (110s)",
     "const SWEEP_BUDGET_MS = 110_000" in F),
    ("bounded concurrency cap (10)",
     "const SWEEP_CONCURRENCY = 10" in F),
    ("bounded per-attempt budget (90s)",
     "const GRADING_ATTEMPT_BUDGET_MS = 90_000" in F),
    ("work processed in waves of SWEEP_CONCURRENCY",
     "for (let i = 0; i < ids.length; i += SWEEP_CONCURRENCY)" in F),
    ("deadline checked BEFORE each attempt is admitted",
     "if (Date.now() >= sweepDeadline)" in F),
    ("attempt deadline = min(attempt budget, sweep deadline)",
     bool(re.search(
         r"Math\.min\(\s*Date\.now\(\)\s*\+\s*GRADING_ATTEMPT_BUDGET_MS\s*,\s*sweepDeadline\s*,?\s*\)",
         F))),
    ("failure isolation via Promise.allSettled",
     "await Promise.allSettled(" in F),
    ("the old unbounded wave Promise.all is gone",
     "const waveResults = await Promise.all(" not in F),
    ("rejections counted, not swallowed", "failed += 1" in F),
    ("rejections logged without secrets",
     "attempt raised:" in F and "submissionToken" not in F.split("attempt raised:")[1][:200]),
    ("unfinished work reported as deferred",
     "const deferred = ids.length - started" in F),
    ("empty wave terminates the loop", "if (wave.length === 0)" in F),
    ("answer loop stops at its own deadline",
     "deadlineAt !== null && Date.now() >= deadlineAt" in F),
    ("deadline yields a transport error, NOT a verdict",
     "transportError: 'grading deadline reached'" in F),
    ("hidden-test loop also checks the deadline",
     "input.deadlineAt" in F or "deadlineAt !== null && Date.now() >= deadlineAt" in F),
    ("finalize still runs after a deadline break",
     "quizeazz_finalize_attempt_grading" in F),
    ("unreached answers still counted as pending",
     "deadlineHit ? rows.length - graded.length : 0" in F),
    ("deadline_exhausted surfaced in the result",
     "deadline_exhausted: deadlineHit" in F),
    ("pollUntilVerdict honours the deadline",
     "deadlineAt !== null && Date.now() >= deadlineAt) break" in J),
    ("poll sleep clipped so it cannot overshoot",
     "Math.min(opts.delayMs, deadlineAt - Date.now())" in J),
    ("frontend path still passes NO deadline (behaviour unchanged)",
     "gradeAttempt(cfg, admin, attemptId, 'frontend')" in F),
    ("deadline param is optional (callers unaffected)",
     "deadlineAt?: number | null" in J),
]

print("=== sweep-loop invariants (static, from source) ===")
ok = 0
for name, cond in CHECKS:
    print(f"  [{'x' if cond else ' '}] {name}")
    ok += bool(cond)
print(f"\n  {ok}/{len(CHECKS)} invariants hold")


import shutil

NPX = shutil.which("npx") or shutil.which("npx.cmd")


def typecheck(path):
    args = [NPX, "tsc", "--noEmit", "--ignoreConfig", "--target", "es2022",
            "--module", "esnext", "--moduleResolution", "bundler",
            "--lib", "es2022,dom", "--strict", "--skipLibCheck", path]
    r = subprocess.run(args, capture_output=True, text=True, cwd=str(ROOT))
    # npx on Windows may emit tsc diagnostics on stdout or stderr.
    combined = (r.stdout or "") + (r.stderr or "")
    codes = re.findall(r"error (TS\d+)", combined)
    d = {}
    for c in codes:
        d[c] = d.get(c, 0) + 1
    return d


print("\n=== Edge Function typecheck: HEAD baseline vs current ===")
# Materialise the HEAD sources into a uniquely named temporary directory so the
# baseline typecheck runs against real files. TemporaryDirectory removes the
# tree on exit, including on error, so no residue is left behind and no
# machine-specific path is baked into this script.
with tempfile.TemporaryDirectory(prefix="zz_sweep_tsbase_") as _base:
    base = Path(_base)
    (base / "index.ts").write_text(
        subprocess.run(["git", "show", "HEAD:supabase/functions/judge0-grade/index.ts"],
                       capture_output=True, text=True, cwd=str(ROOT)).stdout, encoding="utf-8")
    (base / "judge0.ts").write_text(
        subprocess.run(["git", "show", "HEAD:supabase/functions/judge0-grade/judge0.ts"],
                       capture_output=True, text=True, cwd=str(ROOT)).stdout, encoding="utf-8")
    b = typecheck(str(base / "index.ts"))
c = typecheck(str(ROOT / "supabase/functions/judge0-grade/index.ts"))
print("  HEAD    :", dict(sorted(b.items())))
print("  current :", dict(sorted(c.items())))
new_codes = {k: v for k, v in c.items() if v > b.get(k, 0)}
print("  new errors introduced :", new_codes if new_codes else "NONE")
print("  errors eliminated     :",
      {k: b[k] - c.get(k, 0) for k in b if b[k] > c.get(k, 0)} or "none")
sys.exit(0 if ok == len(CHECKS) and not new_codes else 1)