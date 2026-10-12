"""
Regression tests for baseline-evidence parser ambiguity in
scripts/zzsmoke_archive_manifest.py.

Each case is a whole helper invocation against a temp workspace: temp manifest,
temp archive dir, temp evidence files. Nothing in deploy/ is touched, no SQL
runs, no fixture or account exists.
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
results = []


def check(name, cond, detail=""):
    print(f"  [{'x' if cond else ' '}] {name}" + (f"\n        {detail}" if detail else ""))
    results.append(bool(cond))


def rows(counts=None, verdict="identical", dup=0, drop=(), rename=None, raw=None):
    out = ["=== baseline comparison ==="]
    for t, m in BASE.items():
        if t in drop:
            continue
        name = (rename or {}).get(t, t)
        n = (counts or {}).get(t, m["rows"])
        if raw is not None and t == next(iter(raw)):
            out.append(raw[t])
            continue
        out.append(f"  {name:34} {n} -> {n}    {verdict}")
        for _ in range(dup):
            out.append(f"  {name:34} {n} -> {n}    {verdict}")
    out += ["", PASS]
    return "\n".join(out)


def run(baseline_text):
    d = Path(tempfile.mkdtemp(prefix="zzp_"))
    (d / "archive").mkdir()
    c = d / "ZZSMOKE_MANIFEST.json"
    c.write_bytes(json.dumps(ACTIVE, indent=2).encode())
    (d / "b.txt").write_text(baseline_text, encoding="utf-8")
    audit = ["=== content-hash audit ==="]
    for t, m in BASE.items():
        audit.append(f"  {t:34} base={m['rows']} present={m['rows']} identical={m['rows']} "
                     f"changed=0 missing=0 extra=0")
    (d / "a.txt").write_text("\n".join(audit), encoding="utf-8")
    (d / "f.json").write_text(json.dumps({
        "run_summary": "Parser regression fixture.",
        "grading": {"outcome": "evaluated", "verdict": "Correct", "score": "1.00",
                    "max_score": "1.00", "submission_to_verdict_seconds": 6.46},
        "grading_attribution": {"trigger_path": "frontend", "method": "exclusion",
                                 "browser_request_captured": False,
                                 "edge_source_frontend_log_available": False,
                                 "basis": "Excluded by the live sweep eligibility gate."},
        "per_test_case_results_persisted": False,
        "cleanup_execution_output_captured": True,
        "auth_fixture_deletion": {"observed_directly": False,
                                  "verified_by": "post-cleanup end-state inspection"},
        "restoration": {"all_tables_byte_identical": True},
    }, indent=2), encoding="utf-8")
    (d / "c.txt").write_text("cleanup ran cleanly\n", encoding="utf-8")
    p = subprocess.run(
        [sys.executable, str(HELPER), "--baseline-evidence", str(d / "b.txt"),
         "--content-audit", str(d / "a.txt"), "--run-facts", str(d / "f.json"),
         "--cleanup-evidence", str(d / "c.txt"), "--run-id", "2026-10-13",
         "--canonical", str(c), "--archive-dir", str(d / "archive")],
        capture_output=True, text=True, timeout=120)
    return p.returncode == 0, p.stdout


def rejected(label, text, expect_in=None):
    acc, out = run(text)
    ok = not acc and (expect_in is None or expect_in in out)
    check(label, ok, ("ACCEPTED - should reject" if acc else out.strip().splitlines()[-1][:110]))


print("=== positive control (the fix must not over-reject) ===")
acc, out = run(rows())
check("well-formed evidence is still accepted", acc, out.strip().splitlines()[-1][:100])

print("\n=== 1. duplicate table names ===")
rejected("duplicate rows with matching counts", rows(dup=1), "more than once")

print("\n=== 2. missing / unexpected table names ===")
rejected("a table is missing", rows(drop=("public.answers",)), "no row for")
rejected("an unexpected table is present",
        rows() + "\n  public.made_up_table   1 -> 1    identical\n", "unexpected table")

print("\n=== 3. malformed / negative / ambiguous row counts ===")
bad = rows(raw={"auth.users": f"  {'auth.users':34} six -> 6    identical"})
rejected("non-numeric count", bad, "no row for")
bad2 = rows(raw={"auth.users": "  auth.users                       -6 -> -6    identical"})
rejected("negative count", bad2, "negative row count")
bad3 = rows(raw={"auth.users": "  auth.users                       6 -> 6    identical extra"})
rejected("trailing junk on a row", bad3)

print("\n=== 4. multiple / contradictory PASS summaries ===")
rejected("two PASS sentences", rows() + "\n" + rows(counts={"public.profiles": 8}),
         "success sentences")
rejected("PASS plus a plain FAIL line", rows() + "\nFAIL: production differs from baseline")
rejected("PASS plus a query 'unavailable:'",
         rows() + "\n=== t ===\n  unavailable: Initialising login role...")
rejected("PASS plus 'ERROR:'", rows() + "\nERROR: connection reset")

print("\n=== 5. truncation ===")
truncated = "\n".join(rows().splitlines()[:6] + [PASS])
rejected("truncated body, PASS appended", truncated)
rejected("rows present but no PASS line at all",
         "\n".join(l for l in rows().splitlines() if not l.startswith("PASS")))

print("\n=== 6. PASS combined with drift / failure markers ===")
rejected("a table marked DRIFT", rows(verdict="DRIFT"), "failure marker")
rejected("POST-CHECK FAILED present", rows() + "\nPOST-CHECK FAILED: answers remain.")
rejected("CLEANUP ABORTED present", rows() + "\nCLEANUP ABORTED: exam mismatch.")
rejected("Traceback present", rows() + "\nTraceback (most recent call last):")

print("\n=== 7. count disagreement still caught (pre-existing behaviour) ===")
rejected("row count disagrees with the recorded baseline",
         rows(counts={"public.profiles": 8}), "disagrees with the recorded baseline")

print(f"\n{'=' * 64}\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)