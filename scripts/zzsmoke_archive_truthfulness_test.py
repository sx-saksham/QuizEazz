"""
Truthfulness tests for scripts/zzsmoke_archive_manifest.py.

These cases model the 2026-10-12 ZZSMOKE run as it actually ended:

  * every public.* table restored byte-identically (content-hash proved),
  * auth.identities restored byte-identically,
  * auth.users carrying ONE known content difference on an ORIGINAL account,
  * the cleanup transaction's output never captured,
  * grading succeeded but frontend attribution was inferred by exclusion,
  * per-test-case results not persisted,
  * the Auth deletion performed by the operator and only verified by end state.

The point of every case below is that MISSING evidence must never be promoted
into a SUCCESSFUL check, and that no archive may assert a byte-identical
restoration the content audit does not prove.

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
    "teacher_id": "4f9d602d-954d-495a-b0ab-659abd8dc24b",
    "student_id": "8c96455e-c343-49ec-b853-2435b8167309",
    "exam_id": "b92b326a-67af-455e-99ab-37566e58b6c0",
    "question_id": "0eae6958-292d-4bd8-8db2-a7f32e13c42e",
    "attempt_id": "db6c790e-8d6d-4637-a163-ffb46c8a22e2",
}
# The real original account whose auth.users content changed during the window.
DRIFT_UUID = "a5cc4116-8cc9-4d4e-8b71-fe0a64944f98"
PASS = "PASS: production is byte-identical to the pre-fixture baseline."
results = []


def check(name, cond, detail=""):
    print(f"  [{'x' if cond else ' '}] {name}" + (f"\n        {detail}" if detail else ""))
    results.append(bool(cond))


def baseline_text():
    out = ["=== baseline comparison ==="]
    for t, m in BASE.items():
        out.append(f"  {t:34} {m['rows']} -> {m['rows']}    identical")
    out += ["", PASS]
    return "\n".join(out)


def audit_text(drift=None):
    """drift: {table: [uuid, ...]} - models the real auth.users difference."""
    drift = drift or {}
    out = ["=== content-hash audit ==="]
    details = []
    for t, m in BASE.items():
        ids = drift.get(t, [])
        changed = len(ids)
        identical = m["rows"] - changed
        out.append(f"  {t:34} base={m['rows']} present={m['rows']} identical={identical} "
                   f"changed={changed} missing=0 extra=0")
        for u in ids:
            details.append(f"CONTENT-CHANGED: {t} {u}")
    return "\n".join(out + details)


def facts(**over):
    f = {
        "run_summary": "Disposable grading smoke test; grading succeeded.",
        "grading": {"outcome": "evaluated", "verdict": "Correct", "score": "1.00",
                    "max_score": "1.00", "submission_to_verdict_seconds": 6.46},
        "grading_attribution": {"trigger_path": "frontend", "method": "exclusion",
                                 "browser_request_captured": False,
                                 "edge_source_frontend_log_available": False,
                                 "basis": "The live cron sweep eligibility gate could not have "
                                          "selected the attempt before grading completed."},
        "per_test_case_results_persisted": False,
        "cleanup_execution_output_captured": False,
        "auth_fixture_deletion": {"observed_directly": False,
                                  "verified_by": "post-cleanup end-state inspection"},
        "restoration": {"all_tables_byte_identical": False},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(f.get(k), dict):
            f[k] = {**f[k], **v}
        else:
            f[k] = v
    return f


def go(audit=None, f=None, cleanup=False):
    d = Path(tempfile.mkdtemp(prefix="zztruth_"))
    (d / "archive").mkdir()
    c = d / "ZZSMOKE_MANIFEST.json"
    c.write_bytes(json.dumps(ACTIVE, indent=2).encode())
    (d / "b.txt").write_text(baseline_text(), encoding="utf-8")
    (d / "a.txt").write_text(audit if audit is not None else audit_text(), encoding="utf-8")
    # A string is written RAW so a malformed-facts case is not silently repaired
    # by json.dumps into valid JSON.
    (d / "f.json").write_text(
        f if isinstance(f, str) else json.dumps(f if f is not None else facts(), indent=2),
        encoding="utf-8")
    argv = [sys.executable, str(HELPER),
            "--baseline-evidence", str(d / "b.txt"),
            "--content-audit", str(d / "a.txt"),
            "--run-facts", str(d / "f.json"),
            "--run-id", "2026-10-12",
            "--canonical", str(c), "--archive-dir", str(d / "archive")]
    if cleanup:
        (d / "c.txt").write_text("ZZSMOKE cleanup: APPLICATION RECORDS COMPLETE (6 row(s) deleted).\n",
                                encoding="utf-8")
        argv += ["--cleanup-evidence", str(d / "c.txt")]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    arch = d / "archive" / "ZZSMOKE_RUN_2026-10-12.manifest.json"
    a = json.loads(arch.read_text(encoding="utf-8")) if arch.exists() else None
    return p.returncode == 0, p.stdout, a, c


def refused(label, out, expect_in=None):
    acc, o, _, _ = out
    ok = not acc and (expect_in is None or expect_in in o)
    check(label, ok, ("ACCEPTED - should reject" if acc else o.strip().splitlines()[-1][:110]))


print("=== 1. the real run: one known auth.users difference is recorded truthfully ===")
acc, out, a, c = go(audit=audit_text({"auth.users": [DRIFT_UUID]}), f=facts())
check("accepted", acc, out.strip().splitlines()[-1][:110] if not acc else "")
if a:
    ra = a["_restoration_audit"]
    check("all_tables_byte_identical is FALSE", ra["all_tables_byte_identical"] is False)
    check("exactly one changed row recorded", ra["total_content_changed_rows"] == 1)
    check("the changed row is recorded by uuid",
          ra["known_content_differences"] == [{"table": "auth.users", "id": DRIFT_UUID,
                                                "kind": "content-hash differs from baseline"}])
    check("auth.users listed as drifted", ra["tables_with_content_change"] == ["auth.users"])
    check("every other table listed byte-identical",
          "auth.users" not in ra["tables_byte_identical"] and len(ra["tables_byte_identical"]) == 10)
    check("restoration proof refuses whole-DB byte-identity",
          "NOT a byte-identical restoration" in a["_restoration_proof"])
    check("public.* all claimed byte-identical",
          all(t in ra["tables_byte_identical"] for t in BASE if t.startswith("public.")))
    check("auth.identities claimed byte-identical", "auth.identities" in ra["tables_byte_identical"])
    check("canonical reset to null template",
          all(json.loads(c.read_text())[k] is None for k in
              ("teacher_id", "student_id", "exam_id", "question_id", "attempt_id")))

print("\n=== 2. unavailable evidence is recorded, never synthesised ===")
if a:
    ue = a["_evidence_categories"]["unavailable_evidence"]
    check("cleanup output listed as unavailable",
          any("Cleanup execution output" in u for u in ue))
    check("cleanup output_captured is False", a["_cleanup_execution"]["output_captured"] is False)
    check("no NOTICE text invented anywhere in the archive",
          "APPLICATION RECORDS COMPLETE" not in json.dumps(a))
    check("browser submission capture listed as unavailable",
          any("browser Network capture" in u for u in ue))
    check("source:'frontend' log listed as unavailable",
          any("source:'frontend'" in u for u in ue))
    check("per-test-case results listed as unavailable",
          any("Per-test-case" in u for u in ue))
    check("grading note says per-test-case not persisted",
          a["_grading"]["_per_test_case_note"].startswith("Per-test-case outcomes were not persisted"))

print("\n=== 3. categories stay distinct: verified / discrepancy / unavailable / inference ===")
if a:
    cat = a["_evidence_categories"]
    check("discrepancy is in known_discrepancies, NOT in verified_facts",
          not any("a5cc4116" in v for v in cat["verified_facts"])
          and any(d["id"] == DRIFT_UUID for d in cat["known_discrepancies"]))
    check("attribution is in inferences, NOT in verified_facts",
          any("inferred by exclusion" in i.lower() for i in cat["inferences"])
          and not any("trigger" in v.lower() and "frontend" in v.lower() for v in cat["verified_facts"]))
    check("grading OUTCOME is in verified_facts",
          any("Disposable grading smoke test" in v for v in cat["verified_facts"]))
    check("auth end-state fact is in verified_facts",
          any("Auth identities are absent" in v for v in cat["verified_facts"]))
    check("auth deletion disclaimer present",
          "did not observe the Dashboard action" in a["_auth_fixture_deletion"]["_note"])
    check("attribution note labels the trigger path an inference",
          "is an inference" in a["_grading"]["_attribution_note"])
    check("inferences entry says it is NOT an observed log line",
          any("NOT an observed log line" in i for i in cat["inferences"]))

print("\n=== 4. a FULLY clean audit permits a byte-identity claim ===")
acc, out, a2, _ = go(audit=audit_text(), f=facts(restoration={"all_tables_byte_identical": True}))
check("accepted", acc, out.strip().splitlines()[-1][:110] if not acc else "")
if a2:
    check("all_tables_byte_identical TRUE", a2["_restoration_audit"]["all_tables_byte_identical"] is True)
    check("no discrepancies recorded", a2["_restoration_audit"]["known_content_differences"] == [])
    check("proof states byte-identical", "byte-identical" in a2["_restoration_proof"]
          and "NOT a byte-identical" not in a2["_restoration_proof"])

print("\n=== 5. fabricated / contradictory claims are REJECTED ===")
refused("claims byte-identity while the audit found a changed row",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(restoration={"all_tables_byte_identical": True})),
        "contradicts")
refused("understates a fully-proven restoration as imperfect",
        go(audit=audit_text(),
           f=facts(restoration={"all_tables_byte_identical": False})),
        "understates")
refused("prose says 'zero drift' while the audit found a change",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(run_summary="Cleanup finished with zero drift across the database.")),
        "zero drift")
refused("prose says 'all tables identical' while the audit found a change",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(run_summary="Restoration is complete: all tables identical to baseline.")),
        "all tables identical")
refused("claims cleanup output captured but supplies no evidence file",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(cleanup_execution_output_captured=True)),
        "Missing evidence must never be recorded")
refused("denies cleanup output while an evidence file IS supplied",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(cleanup_execution_output_captured=False), cleanup=True),
        "contradicts reality")
refused("claims direct-log attribution but records the log as unavailable",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(grading_attribution={"method": "direct_log"})),
        "contradicts its own evidence")
refused("claims direct-log attribution but records the browser request as missing",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(grading_attribution={"method": "direct_log",
                                       "browser_request_captured": False,
                                       "edge_source_frontend_log_available": True})),
        "contradicts its own evidence")
refused("says 'by exclusion' while recording BOTH direct signals available",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(grading_attribution={"method": "exclusion",
                                       "browser_request_captured": True,
                                       "edge_source_frontend_log_available": True})),
        "contradictory evidence")
refused("unknown attribution method",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(grading_attribution={"method": "vibes"})),
        "must be 'direct_log' or 'exclusion'")
refused("not observed directly AND no verification method given",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(auth_fixture_deletion={"observed_directly": False, "verified_by": "   "})),
        "must say how it was verified")
refused("missing a required run-facts field",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f={k: v for k, v in facts().items() if k != "grading_attribution"}),
        "missing required field")
refused("wrong type for a boolean field",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(per_test_case_results_persisted="yes")),
        "must be a boolean")
refused("run-facts is not valid JSON",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}), f="not json"),
        "not valid JSON")

print("\n=== 6. the audit cannot be made to under-report its own changes ===")
refused("changed=1 with no CONTENT-CHANGED id line",
        go(audit="\n".join(["=== content-hash audit ==="] +
                           [f"  {t:34} base={m['rows']} present={m['rows']} "
                            f"identical={m['rows'] - (1 if t == 'auth.users' else 0)} "
                            f"changed={1 if t == 'auth.users' else 0} missing=0 extra=0"
                            for t, m in BASE.items()]),
           f=facts()),
        "CONTENT-CHANGED detail line")
def _extra_row_audit():
    """present/missing do not reconcile with base - an unaccounted live row."""
    out = ["=== content-hash audit ==="]
    for t, m in BASE.items():
        if t == "public.answers":
            out.append(f"  {t:34} base={m['rows']} present={m['rows'] + 1} "
                       f"identical={m['rows']} changed=0 missing=0 extra=1")
        else:
            out.append(f"  {t:34} base={m['rows']} present={m['rows']} identical={m['rows']} "
                       f"changed=0 missing=0 extra=0")
    return "\n".join(out)


refused("an extra row the audit does not account for",
        go(audit=_extra_row_audit(), f=facts()),
        "internally inconsistent")

print("\n=== 7. audit reflecting a missing row is refused as incomplete ===")
refused("a baseline row is missing after cleanup",
        go(audit="\n".join(["=== content-hash audit ==="] +
                           [f"  {t:34} base={m['rows']} present={m['rows'] - (1 if t == 'public.answers' else 0)} "
                            f"identical={m['rows'] - (1 if t == 'public.answers' else 0)} changed=0 "
                            f"missing={1 if t == 'public.answers' else 0} extra=0"
                            for t, m in BASE.items()]),
           f=facts()),
        "restoration is incomplete")

print("\n=== 8. discrepancy annotations may only describe what the audit found ===")
ann = facts(known_discrepancies=[{
    "table": "auth.users", "id": DRIFT_UUID, "status": "unresolved",
    "note": "updated_at moved during the test window on an ORIGINAL account; it is not a "
            "fixture account.",
    "do_not_modify": True}])
acc, out, a3, _ = go(audit=audit_text({"auth.users": [DRIFT_UUID]}), f=ann)
check("a faithful annotation is accepted", acc, out.strip().splitlines()[-1][:110] if not acc else "")
if a3:
    kd = a3["_evidence_categories"]["known_discrepancies"]
    check("exactly one discrepancy entry (merged, not duplicated)", len(kd) == 1, str(kd))
    check("uuid preserved", kd[0]["id"] == DRIFT_UUID)
    check("status 'unresolved' recorded", kd[0].get("status") == "unresolved")
    check("do_not_modify recorded True", kd[0].get("do_not_modify") is True)
    check("still recorded as a discrepancy, not a verified fact",
          not any("a5cc4116" in v for v in a3["_evidence_categories"]["verified_facts"]))

refused("annotates a discrepancy the audit did NOT find",
        go(audit=audit_text(),
           f=facts(restoration={"all_tables_byte_identical": True},
                   known_discrepancies=[{"table": "auth.users", "id": DRIFT_UUID,
                                         "status": "unresolved", "note": "invented"}])),
        "does not support")
refused("annotates a table the audit reported as identical",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(known_discrepancies=[{"table": "public.profiles", "id": DRIFT_UUID,
                                         "status": "unresolved", "note": "wrong table"}])),
        "does not support")
refused("annotation id is not a UUID",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(known_discrepancies=[{"table": "auth.users", "id": "not-a-uuid",
                                         "status": "unresolved", "note": "x"}])),
        "not a UUID")
refused("annotation missing a required key",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(known_discrepancies=[{"table": "auth.users", "id": DRIFT_UUID}])),
        "is missing")
refused("known_discrepancies is not a list",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(known_discrepancies={"table": "auth.users"})),
        "must be a list")
refused("do_not_modify is not a boolean",
        go(audit=audit_text({"auth.users": [DRIFT_UUID]}),
           f=facts(known_discrepancies=[{"table": "auth.users", "id": DRIFT_UUID,
                                         "status": "unresolved", "note": "x",
                                         "do_not_modify": "yes"}])),
        "must be a boolean")

print(f"\n{'=' * 64}\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)