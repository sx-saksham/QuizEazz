"""
PROBE (read-only): which ambiguous baseline-evidence files does the CURRENT
verify_baseline_evidence() accept? Anything accepted here is a bug.

Runs the helper as a subprocess against temp workspaces. Nothing in deploy/ is
touched and no SQL runs.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "zzsmoke_archive_manifest.py"
BASE = json.loads((ROOT / "deploy" / "ZZSMOKE_BASELINE.json").read_text(encoding="utf-8"))
ACTIVE = {
    "_status": "ACTIVE_FIXTURE", "_rule": "r",
    "teacher_id": "dde0c3e8-61ee-4d36-a874-3b1ef8a82413",
    "student_id": "1a79277a-f727-4f55-adc3-107aa00045f5",
    "exam_id": "1615124b-3645-443b-b23f-7321610bfc54",
    "question_id": "3806253d-cfae-40c5-a85c-a7b181ae3f97",
    "attempt_id": "55ff839f-39ca-44de-ae80-449a11e47b8e",
}
PASS = "PASS: production is byte-identical to the pre-fixture baseline."


def rows_text(dup=0, extra=(), counts=None, verdict="identical"):
    out = ["=== baseline comparison ==="]
    for t, m in BASE.items():
        n = (counts or {}).get(t, m["rows"])
        out.append(f"  {t:34} {n} -> {n}    {verdict}")
        for _ in range(dup):
            out.append(f"  {t:34} {n} -> {n}    {verdict}")
    out.extend(extra)
    out += ["", PASS]
    return "\n".join(out)


def attempt(label, baseline_text, expect_reject=True):
    d = Path(tempfile.mkdtemp(prefix="probe_"))
    (d / "archive").mkdir()
    c = d / "ZZSMOKE_MANIFEST.json"
    c.write_bytes(json.dumps(ACTIVE, indent=2).encode())
    (d / "b.txt").write_text(baseline_text, encoding="utf-8")
    (d / "c.txt").write_text("cleanup ran cleanly\n", encoding="utf-8")
    p = subprocess.run(
        [sys.executable, str(HELPER), "--baseline-evidence", str(d / "b.txt"),
         "--cleanup-evidence", str(d / "c.txt"), "--run-id", "2026-10-13",
         "--canonical", str(c), "--archive-dir", str(d / "archive")],
        capture_output=True, text=True, timeout=120)
    accepted = p.returncode == 0
    verdict = "ACCEPTED" if accepted else "rejected"
    bug = "  <-- BUG: should reject" if accepted and expect_reject else ""
    print(f"  [{'BUG' if bug else 'ok  '}] {label:46} {verdict}{bug}")
    return accepted


print("=== PROBE: ambiguous evidence the parser must reject ===\n")
found = 0

found += attempt("duplicate table rows (matching counts)", rows_text(dup=1))

found += attempt("unexpected extra table name",
                 rows_text(extra=["  public.made_up_table             1 -> 1    identical"]))

found += attempt("PASS line + a plain FAIL statement",
                 rows_text(extra=["", "FAIL: something went wrong"]))

found += attempt("PASS line + 'unavailable:' query error",
                 rows_text(extra=["", "=== some table ===", "  unavailable: Initialising login role..."]))

found += attempt("PASS line + 'ERROR:'",
                 rows_text(extra=["", "ERROR: connection reset"]))

found += attempt("PASS line + contradictory second summary",
                 rows_text(extra=["", "FAIL: production differs from baseline (3 tables drift)"]))

found += attempt("truncated body but PASS appended",
                 rows_text(extra=["", "...output truncated..."]) if False else
                 "\n".join(rows_text().splitlines()[:6] + [PASS]))

found += attempt("PASS line appears twice with differing tables",
                 rows_text() + "\n" + rows_text(counts={"public.profiles": 8}))

print(f"\n{'=' * 64}\n  cases accepted that should have been rejected: {found}")
sys.exit(1 if found else 0)