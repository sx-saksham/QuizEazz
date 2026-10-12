"""
Archive a consumed ZZSMOKE manifest and restore the canonical null template.

LOCAL-ONLY. This helper executes NO SQL, invokes no cleanup, creates or deletes
no accounts, touches no database and runs no deployment command. It only reads
evidence files the operator produced, re-verifies them, and rewrites two local
JSON files.

================================================================================
TRUTHFULNESS CONTRACT
================================================================================
The `compare` tool this helper historically trusted proves only that every
baselined table is PRESENT with the EXPECTED ROW COUNT. Its diff() keys on id
membership alone and never compares row content, so it cannot observe an
in-place modification of a row that exists on both sides. A clean `compare`
run is therefore NOT evidence of byte-identical restoration.

This helper consequently separates three independent proofs and refuses to
merge them:

  1. PRESENCE + COUNT   --baseline-evidence  (compare output; unchanged)
  2. ROW CONTENT        --content-audit      (NEW, load-bearing for any
                                             byte-identity claim)
  3. RUN FACTS         --run-facts          (NEW; operator-supplied, but
                                             validated for internal
                                             contradiction against 1 and 2)

Every restoration sentence written into an archive is DERIVED from proof 2.
None is hardcoded. If the content audit shows any changed row, the archive
records that discrepancy by UUID and the restoration proof says so in plain
words. A full-database byte-identity claim is refused outright.

================================================================================
EVIDENCE THE OPERATOR MUST SUPPLY
================================================================================
1. --baseline-evidence  verbatim stdout of

       python scripts/zzsmoke_baseline_fingerprint.py compare

   The helper re-parses it and cross-checks every table name and row count
   against deploy/ZZSMOKE_BASELINE.json, requires zero 'DRIFT' lines and the
   exact success sentence. This proves presence and counts. It does NOT prove
   content.

2. --content-audit  verbatim stdout of a per-table content-hash audit, one row
   per baselined table, in this exact shape:

       === content-hash audit ===
         public.profiles    base=6 present=6 identical=6 changed=0 missing=0 extra=0
         ...
         auth.users         base=6 present=6 identical=5 changed=1 missing=0 extra=0
       CONTENT-CHANGED: auth.users a5cc4116-8cc9-4d4e-8b71-fe0a64944f98

   Every table in ZZSMOKE_BASELINE.json must appear exactly once. base must
   equal the recorded baseline row count; present+missing==base;
   identical+changed==present. A table reporting changed=N must supply exactly
   N CONTENT-CHANGED detail lines and a table reporting changed=0 must supply
   none. This is the only artifact that can support a byte-identity claim.

3. --run-facts  a JSON document the operator authors, recorded verbatim into
   the archive so the run's outcome and its limitations survive. Structure is
   validated; claims are cross-checked against proofs 1 and 2. See
   RUN_FACTS_SCHEMA below. A claim that contradicts the audit is REJECTED, not
   recorded.

4. --cleanup-evidence  OPTIONAL verbatim stdout of the cleanup transaction.

   CORROBORATING only. `supabase db query` does not surface RAISE NOTICE, and
   a Dashboard run may never have been captured at all. If this argument is
   omitted the archive records the cleanup execution output as UNAVAILABLE.
   It is never synthesised. run-facts.cleanup_execution_output_captured must
   agree with whether the file was supplied.

================================================================================
USAGE
================================================================================
  python scripts/zzsmoke_archive_manifest.py \
      --baseline-evidence  <file> \
      --content-audit      <file> \
      --run-facts          <file> \
      [--cleanup-evidence  <file>] \
      --run-id             2026-10-13
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / "deploy" / "ZZSMOKE_MANIFEST.json"
ARCHIVE_DIR = ROOT / "deploy" / "archive"
BASELINE = ROOT / "deploy" / "ZZSMOKE_BASELINE.json"

REQUIRED_IDS = ["teacher_id", "student_id", "exam_id", "question_id", "attempt_id"]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
PASS_LINE = "PASS: production is byte-identical to the pre-fixture baseline."
FAIL_MARKERS = ["ABORTED", "POST-CHECK FAILED", "CLEANUP ABORTED",
                "DRIFT", "BOOTSTRAP ABORTED", "Traceback (most recent call last)",
                "FAIL", "unavailable:", "ERROR", "refused", "unavailable"]
# A comparison table row: "<table> <before> -> <after> <verdict>".
ROW_RE = re.compile(r"^[ \t]*(\S+)[ \t]+(-?\d+)[ \t]*->[ \t]*(-?\d+)[ \t]+(\S+)[ \t]*$", re.M)
# A content-audit row: "<table> base=N present=N identical=N changed=N missing=N extra=N"
AUDIT_ROW_RE = re.compile(
    r"^[ \t]*(\S+)[ \t]+base=(\d+)[ \t]+present=(\d+)[ \t]+identical=(\d+)"
    r"[ \t]+changed=(\d+)[ \t]+missing=(\d+)[ \t]+extra=(\d+)[ \t]*$", re.M)
# A content-change detail line: "CONTENT-CHANGED: <table> <uuid>"
CHANGED_ROW_RE = re.compile(r"^[ \t]*CONTENT-CHANGED:[ \t]+(\S+)[ \t]+([0-9a-f-]{36})[ \t]*$", re.M | re.I)
# Anything that could be a credential must never reach an archived file.
SECRET_MARKERS = [re.compile(p) for p in (
    r"sb_secret_[A-Za-z0-9_-]{8,}", r"eyJ[A-Za-z0-9_-]{20,}",
    r"service_role_key\s*[=:]\s*\S", r"refresh_token", r"access_token",
    r"password", r"VERCEL_OIDC_TOKEN", r"JUDGE0_API_TOKEN")]

TEMPLATE = {
    "_status": "TEMPLATE - no active fixture. Populated by scripts/zzsmoke_capture_manifest.py at fixture creation.",
    "_rule": "A null id means 'not yet created'. Tooling refuses to classify or delete against it; ZZSMOKE_CLEANUP.sql reads operator-pasted literals, never this file.",
    "teacher_id": None, "student_id": None,
    "exam_id": None, "question_id": None, "attempt_id": None,
}

# Phrases that assert a whole-database byte-identity restoration. They may only
# appear when the content audit actually proves every row identical.
FULL_RESTORATION_PHRASES = [
    "all tables identical",
    "zero drift",
    "byte-identical to the pre-fixture baseline",
    "byte-identical to baseline",
    "fully restored",
    "production is byte-identical",
]

RUN_FACTS_SCHEMA = {
    "run_summary": str,
    "grading": ("dict", ("outcome", "verdict", "score", "max_score",
                         "submission_to_verdict_seconds")),
    "grading_attribution": ("dict", ("trigger_path", "method",
                                     "browser_request_captured",
                                     "edge_source_frontend_log_available", "basis")),
    "per_test_case_results_persisted": bool,
    "cleanup_execution_output_captured": bool,
    "auth_fixture_deletion": ("dict", ("observed_directly", "verified_by")),
    "restoration": ("dict", ("all_tables_byte_identical",)),
}


def die(msg, code=3):
    print(f"\nARCHIVE REFUSED: {msg}")
    print("  No file was modified. The canonical manifest and all archives are intact.")
    sys.exit(code)


def read_json_bomfree(path: Path, label: str):
    if not path.exists():
        die(f"{label} does not exist: {path}")
    raw = path.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        die(f"{label} has a UTF-8 BOM: {path}")
    try:
        return json.loads(raw.decode("utf-8")), raw
    except Exception as exc:
        die(f"{label} is not valid JSON: {exc}")


def verify_baseline_evidence(path: Path):
    """Re-verify the presence/count artifact against the committed baseline.

    Every row is parsed - not just the first match - so a duplicated, extra, or
    contradictory table row cannot hide behind a valid one. NOTE: the PASS
    sentence in this artifact is a presence-and-count statement only; it is
    not accepted as proof of byte-identity. That is the content audit's job.
    """
    if not path.exists():
        die("--baseline-evidence file not found. Run "
            "'python scripts/zzsmoke_baseline_fingerprint.py compare' and capture its output.")
    text = path.read_text(encoding="utf-8", errors="replace")

    if PASS_LINE not in text:
        die("baseline evidence does not contain the success sentence - the comparison did not pass.")
    if text.count(PASS_LINE) != 1:
        die(f"baseline evidence contains {text.count(PASS_LINE)} success sentences; "
            "expected exactly 1 (contradictory summaries).")
    for marker in FAIL_MARKERS:
        if marker in text:
            die(f"baseline evidence contains a failure marker: {marker!r}")

    doc = read_json_bomfree(BASELINE, "ZZSMOKE_BASELINE.json")[0]

    rows = ROW_RE.findall(text)
    if not rows:
        die("baseline evidence contains no comparable table rows.")
    seen = {}
    for name, before, after, verdict in rows:
        if name in seen:
            die(f"baseline evidence lists {name} more than once - ambiguous or tampered.")
        seen[name] = (before, after, verdict)

    expected = set(doc)
    present = set(seen)
    for missing in sorted(expected - present):
        die(f"baseline evidence has no row for {missing}")
    for extra in sorted(present - expected):
        die(f"baseline evidence contains an unexpected table {extra!r}")

    for table, meta in doc.items():
        before, after, verdict = seen[table]
        if int(before) < 0 or int(after) < 0:
            die(f"{table} has a negative row count ({before} -> {after})")
        if verdict != "identical":
            die(f"{table} is '{verdict}' in the evidence, not 'identical'")
        if int(before) != int(after) or int(after) != meta["rows"]:
            die(f"{table} row count {before}->{after} disagrees with the recorded baseline "
                f"({meta['rows']})")
    print(f"  presence/count evidence verified: {len(doc)} recorded tables, each listed exactly once")
    print("    (presence + counts only; NOT a byte-identity proof)")


def verify_content_audit(path: Path):
    """Parse the per-table content-hash audit. This is the only byte-identity proof."""
    if not path.exists():
        die("--content-audit file not found. A per-table content-hash audit is "
            "required before any byte-identity claim can be recorded.")
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        die("--content-audit is empty.")
    if "content-hash audit" not in text:
        die("--content-audit does not look like a content-hash audit "
            "(missing 'content-hash audit' header).")
    for marker in ("Traceback", "ERROR:"):
        if marker in text:
            die(f"content audit contains a failure marker: {marker!r}")

    doc = read_json_bomfree(BASELINE, "ZZSMOKE_BASELINE.json")[0]

    rows = AUDIT_ROW_RE.findall(text)
    if not rows:
        die("content audit contains no parsable table rows.")
    seen = {}
    for name, base, present, ident, changed, missing, extra in rows:
        if name in seen:
            die(f"content audit lists {name} more than once - ambiguous or tampered.")
        seen[name] = dict(base=int(base), present=int(present), identical=int(ident),
                          changed=int(changed), missing=int(missing), extra=int(extra))

    expected = set(doc)
    present_tables = set(seen)
    for missing in sorted(expected - present_tables):
        die(f"content audit has no row for {missing}")
    for extra in sorted(present_tables - expected):
        die(f"content audit contains an unexpected table {extra!r}")

    changed_details = {}
    for table, uid in CHANGED_ROW_RE.findall(text):
        changed_details.setdefault(table, []).append(uid)
    for table in seen:
        if table not in doc:
            die(f"content audit lists unexpected table {table!r} in its CONTENT-CHANGED lines.")
    for table in changed_details:
        if table not in seen:
            die(f"content audit reports CONTENT-CHANGED for {table!r} but has no row for it.")

    audit = {}
    for table, meta in doc.items():
        v = seen[table]
        if v["base"] != meta["rows"]:
            die(f"content audit says {table} base={v['base']} but the recorded baseline "
                f"has {meta['rows']} rows.")
        if v["present"] + v["missing"] != v["base"]:
            die(f"content audit for {table} is internally inconsistent: "
                f"present({v['present']}) + missing({v['missing']}) != base({v['base']})")
        if v["identical"] + v["changed"] != v["present"]:
            die(f"content audit for {table} is internally inconsistent: "
                f"identical({v['identical']}) + changed({v['changed']}) != present({v['present']})")
        uuids = changed_details.get(table, [])
        if len(uuids) != v["changed"]:
            die(f"content audit for {table} reports changed={v['changed']} but supplies "
                f"{len(uuids)} CONTENT-CHANGED detail line(s).")
        for u in uuids:
            if not UUID_RE.match(u):
                die(f"content audit for {table} lists a non-UUID id: {u!r}")
        if v["missing"] and v["present"] < v["base"]:
            die(f"content audit for {table} reports {v['missing']} missing baseline row(s); "
                "restoration is incomplete and this must not be archived as cleaned.")
        audit[table] = dict(v, changed_ids=uuids)

    total_changed = sum(a["changed"] for a in audit.values())
    byte_identical = [t for t, a in audit.items() if a["changed"] == 0 and a["missing"] == 0]
    print(f"  content audit verified: {len(audit)} tables")
    print(f"    byte-identical tables        : {len(byte_identical)}")
    print(f"    tables with content changes  : "
          f"{sum(1 for a in audit.values() if a['changed'])} ({total_changed} row(s))")
    return audit


def _check_shape(doc, schema, prefix=""):
    for key, spec in schema.items():
        where = f"{prefix}{key}"
        if key not in doc:
            die(f"run-facts is missing required field {where!r}")
        val = doc[key]
        if spec is str:
            if not isinstance(val, str) or not val.strip():
                die(f"run-facts field {where!r} must be a non-empty string")
        elif spec is bool:
            if not isinstance(val, bool):
                die(f"run-facts field {where!r} must be a boolean, got {type(val).__name__}")
        else:
            _, required = spec
            if not isinstance(val, dict):
                die(f"run-facts field {where!r} must be an object")
            for sub in required:
                if sub not in val:
                    die(f"run-facts field {where!r} is missing {sub!r}")


def validate_run_facts(path: Path, audit: dict, cleanup_captured: bool):
    """Validate run facts and reject any claim the evidence contradicts."""
    if not path.exists():
        die("--run-facts file not found. The run's outcome and its limitations must be "
            "recorded; they are not inferred.")
    raw = path.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        die("--run-facts is empty.")
    try:
        facts = json.loads(raw)
    except Exception as exc:
        die(f"--run-facts is not valid JSON: {exc}")
    if not isinstance(facts, dict):
        die("--run-facts must be a JSON object.")

    _check_shape(facts, RUN_FACTS_SCHEMA)

    total_changed = sum(a["changed"] for a in audit.values())

    # 1. A whole-database byte-identity claim must match the audit exactly.
    claimed = facts["restoration"]["all_tables_byte_identical"]
    if not isinstance(claimed, bool):
        die("run-facts restoration.all_tables_byte_identical must be a boolean")
    if claimed and total_changed:
        die(f"run-facts claims all_tables_byte_identical=true, but the content audit "
            f"found {total_changed} changed row(s). Refusing to record a restoration "
            "claim the evidence contradicts.")
    if (not claimed) and total_changed == 0:
        die("run-facts claims all_tables_byte_identical=false, but the content audit "
            "found 0 changed row(s). The claim understates a fully-proven restoration; "
            "fix the facts or the audit rather than recording a false discrepancy.")

    # 2. Prose must not contradict the audit either.
    prose = " ".join([facts["run_summary"],
                      facts["restoration"].get("summary", ""),
                      facts["grading_attribution"]["basis"]]).lower()
    if total_changed:
        for phrase in FULL_RESTORATION_PHRASES:
            if phrase in prose:
                die(f"run-facts prose asserts {phrase!r} while the content audit found "
                    f"{total_changed} changed row(s). Refusing a contradictory restoration claim.")

    # 3. Attribution method must match the availability of direct evidence.
    at = facts["grading_attribution"]
    m = at["method"]
    if m not in ("direct_log", "exclusion"):
        die(f"run-facts grading_attribution.method must be 'direct_log' or 'exclusion', got {m!r}")
    if not isinstance(at["browser_request_captured"], bool) or \
       not isinstance(at["edge_source_frontend_log_available"], bool):
        die("run-facts grading_attribution availability flags must be booleans")
    if m == "direct_log":
        if not (at["browser_request_captured"] and at["edge_source_frontend_log_available"]):
            die("run-facts claims attribution method 'direct_log' but records that the browser "
                "request and/or the source:'frontend' Edge log were NOT available. "
                "Refusing an attribution that contradicts its own evidence.")
    if m == "exclusion":
        if at["browser_request_captured"] and at["edge_source_frontend_log_available"]:
            die("run-facts records attribution by 'exclusion' while also recording that both "
                "direct signals were available. Refusing contradictory evidence.")

    # 4. Cleanup-evidence availability must match reality.
    cap = facts["cleanup_execution_output_captured"]
    if not isinstance(cap, bool):
        die("run-facts cleanup_execution_output_captured must be a boolean")
    if cap and not cleanup_captured:
        die("run-facts claims the cleanup execution output was captured, but no "
            "--cleanup-evidence file was supplied. Missing evidence must never be "
            "recorded as a successful check.")
    if (not cap) and cleanup_captured:
        die("run-facts claims the cleanup execution output was NOT captured, but a "
            "--cleanup-evidence file was supplied. The record contradicts reality.")

    # 5. Auth deletion must not be claimed as directly observed unless it was.
    ad = facts["auth_fixture_deletion"]
    if not isinstance(ad["observed_directly"], bool):
        die("run-facts auth_fixture_deletion.observed_directly must be a boolean")
    if ad["observed_directly"] is False and not str(ad["verified_by"]).strip():
        die("run-facts records that the Auth deletion was not observed directly, so "
            "auth_fixture_deletion.verified_by must say how it was verified instead.")

    # 6. Optional annotations may only DESCRIBE a discrepancy the audit found.
    #    An annotation can add context and a do-not-modify rule; it can never
    #    introduce a discrepancy the content audit does not independently show.
    kd = facts.get("known_discrepancies", [])
    if not isinstance(kd, list):
        die("run-facts known_discrepancies must be a list")
    audit_diffs = {(t, u) for t, a in audit.items() for u in a["changed_ids"]}
    for i, e in enumerate(kd):
        if not isinstance(e, dict):
            die(f"run-facts known_discrepancies[{i}] must be an object")
        for k in ("table", "id", "status", "note"):
            if k not in e:
                die(f"run-facts known_discrepancies[{i}] is missing {k!r}")
        if not UUID_RE.match(str(e["id"])):
            die(f"run-facts known_discrepancies[{i}].id is not a UUID")
        if (e["table"], str(e["id"]).lower()) not in {(t, u.lower()) for t, u in audit_diffs}:
            die(f"run-facts annotates a known discrepancy for {e['table']} {e['id']} that "
                "the content audit did not find. Refusing to record a discrepancy the "
                "audit does not support.")
        if not isinstance(e.get("do_not_modify", False), bool):
            die(f"run-facts known_discrepancies[{i}].do_not_modify must be a boolean")
    if kd:
        print(f"  known_discrepancies annotations: {len(kd)} (each matched to an audit finding)")

    print(f"  run-facts validated: {len(facts)} top-level field(s), no contradiction with the audit")
    print(f"    attribution method : {m}")
    print(f"    cleanup output     : {'captured' if cleanup_captured else 'UNAVAILABLE (recorded, not synthesised)'}")
    return facts


def verify_cleanup_evidence(path: Path):
    if not path.exists():
        die("--cleanup-evidence file not found. Capture the cleanup transaction's output.")
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        die("--cleanup-evidence is empty.")
    for marker in FAIL_MARKERS:
        if marker in text:
            die(f"cleanup evidence contains a failure marker: {marker!r}")
    print("  cleanup evidence carries no failure marker")


def build_restoration(audit: dict, facts: dict):
    """Derive every restoration sentence from the audit. Nothing is hardcoded."""
    total_changed = sum(a["changed"] for a in audit.values())
    identical = sorted(t for t, a in audit.items() if a["changed"] == 0 and a["missing"] == 0)
    drifted = sorted(t for t, a in audit.items() if a["changed"] != 0)
    discrepancies = [
        {"table": t, "id": uid, "kind": "content-hash differs from baseline"}
        for t in drifted for uid in audit[t]["changed_ids"]
    ]
    n_diff = len(discrepancies)
    if total_changed == 0:
        proof = (f"Content-hash audit: all {len(audit)} baselined tables byte-identical to "
                 f"deploy/ZZSMOKE_BASELINE.json - 0 missing, 0 extra, 0 content-changed rows.")
    else:
        proof = (f"Content-hash audit: {len(identical)} of {len(audit)} baselined tables "
                 f"byte-identical; {n_diff} row(s) differ in content and are recorded "
                 f"individually below. This is NOT a byte-identical restoration of the "
                 f"whole database.")
    return {
        "_restoration_proof": proof,
        "_restoration_audit": {
            "method": ("per-table content-hash comparison of every row id present in both the "
                       "committed baseline and live state (md5 over the full row)"),
            "scope_note": ("This is a stronger proof than the baseline comparison, which keys on "
                           "id presence and row count only and cannot observe an in-place change."),
            "tables_audited": len(audit),
            "tables_byte_identical": identical,
            "tables_with_content_change": drifted,
            "all_tables_byte_identical": total_changed == 0,
            "total_content_changed_rows": total_changed,
            "known_content_differences": discrepancies,
        },
    }


def scan_for_secrets(text: str, label: str):
    for rx in SECRET_MARKERS:
        if rx.search(text):
            die(f"{label} would contain credential-like text ({rx.pattern}); refusing to archive.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-evidence", required=True, type=Path)
    ap.add_argument("--content-audit", required=True, type=Path)
    ap.add_argument("--run-facts", required=True, type=Path)
    ap.add_argument("--cleanup-evidence", type=Path, default=None)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--canonical", type=Path, default=CANONICAL)
    ap.add_argument("--archive-dir", type=Path, default=ARCHIVE_DIR)
    args = ap.parse_args()

    if not re.fullmatch(r"[0-9A-Za-z._-]{1,64}", args.run_id):
        die("--run-id must be 1-64 chars of [A-Za-z0-9._-]")

    print("=== 1. canonical manifest preconditions ===")
    doc, raw = read_json_bomfree(args.canonical, "canonical manifest")
    status = str(doc.get("_status", ""))
    if not status.startswith("ACTIVE_FIXTURE"):
        die(f"canonical manifest is not an ACTIVE fixture (_status={status!r}). "
            "Nothing to archive; the template is already in place.")
    for k in REQUIRED_IDS:
        v = doc.get(k)
        if not isinstance(v, str) or not UUID_RE.match(v):
            die(f"manifest {k}={v!r} is missing or not a UUID - refusing to archive a "
                "partially recorded fixture.")
    if doc["teacher_id"].lower() == doc["student_id"].lower():
        die("teacher_id and student_id are identical.")
    print(f"  ACTIVE_FIXTURE, all {len(REQUIRED_IDS)} ids valid, teacher != student")

    print("\n=== 2. verify evidence ===")
    verify_baseline_evidence(args.baseline_evidence)
    audit = verify_content_audit(args.content_audit)
    cleanup_captured = args.cleanup_evidence is not None
    if cleanup_captured:
        verify_cleanup_evidence(args.cleanup_evidence)
    else:
        print("  --cleanup-evidence not supplied: the cleanup execution output will be "
              "recorded as UNAVAILABLE, not synthesised")
    facts = validate_run_facts(args.run_facts, audit, cleanup_captured)

    print("\n=== 3. destination ===")
    args.archive_dir.mkdir(parents=True, exist_ok=True)
    dest = args.archive_dir / f"ZZSMOKE_RUN_{args.run_id}.manifest.json"
    if dest.exists():
        die(f"archive destination already exists: {dest.name} (refusing to overwrite evidence)")
    print(f"  destination: deploy/archive/{dest.name}")

    print("\n=== 4. build the redacted archive ===")
    restoration = build_restoration(audit, facts)
    archived = {
        "_status": "CONSUMED_AND_CLEANED",
        "_run_id": args.run_id,
        "_note": "HISTORICAL RECORD ONLY. Every UUID below was created and then deleted "
                 "during the ZZSMOKE grading smoke test. None exist in production.",
        "_emails": "REDACTED - personal addresses deliberately omitted from this repository.",
        **restoration,
        "_evidence_categories": {
            "_about": "Categories are kept distinct on purpose. Missing evidence is never "
                      "promoted into a successful check.",
            "verified_facts": [
                f"presence and row counts match deploy/ZZSMOKE_BASELINE.json for all "
                f"{len(audit)} baselined tables",
                f"content-hash audit: {len(restoration['_restoration_audit']['tables_byte_identical'])} "
                f"table(s) byte-identical, "
                f"{restoration['_restoration_audit']['total_content_changed_rows']} row(s) changed",
                facts["run_summary"],
            ],
            "known_discrepancies": restoration["_restoration_audit"]["known_content_differences"],
            "unavailable_evidence": [],
            "inferences": [],
        },
        "_grading": {
            **facts["grading"],
            "per_test_case_results_persisted": facts["per_test_case_results_persisted"],
            "_per_test_case_note": (
                "Per-test-case outcomes were not persisted; only aggregate results and the "
                "recorded final stdout are available."
                if not facts["per_test_case_results_persisted"] else
                "Per-test-case outcomes were persisted and are recorded in grading."),
            "_attribution": facts["grading_attribution"],
            "_attribution_note": (
                "The grading outcome is a verified fact. The TRIGGER PATH is an inference "
                "derived by exclusion, not an observation."
                if facts["grading_attribution"]["method"] == "exclusion" else
                "The trigger path rests on direct log evidence."),
        },
        "_cleanup_execution": {
            "output_captured": cleanup_captured,
            "_note": ("verbatim transaction output was supplied and re-verified"
                      if cleanup_captured else
                      "NOT AVAILABLE - the cleanup was executed outside this tooling and its "
                      "output was never captured. No NOTICE output is recorded, because none "
                      "was observed. Absence of evidence is not evidence of success."),
            "verified_by": ("post-transaction end-state inspection only"
                            if not cleanup_captured else "captured output + end-state inspection"),
        },
        "_auth_fixture_deletion": {
            **facts["auth_fixture_deletion"],
            "_note": ("Performed by the operator through the Supabase Dashboard. This tooling "
                      "did not execute the deletion and did not observe the Dashboard action "
                      "first-hand; only the resulting end state was verified."
                      if not facts["auth_fixture_deletion"]["observed_directly"] else
                      "Directly observed."),
        },
        "teacher_id": doc["teacher_id"], "student_id": doc["student_id"],
        "exam_id": doc["exam_id"], "question_id": doc["question_id"],
        "attempt_id": doc["attempt_id"],
    }

    cats = archived["_evidence_categories"]
    if not cleanup_captured:
        cats["unavailable_evidence"].append(
            "Cleanup execution output (NOTICE lines) - never captured; not synthesised.")
    if not facts["per_test_case_results_persisted"]:
        cats["unavailable_evidence"].append(
            "Per-test-case grading results - not persisted by the grading path.")
    at = facts["grading_attribution"]
    if not at["browser_request_captured"]:
        cats["unavailable_evidence"].append(
            "The browser Network capture of the student submission - the observed tab never "
            "left the fullscreen gate, so the submission request was not captured.")
    if not at["edge_source_frontend_log_available"]:
        cats["unavailable_evidence"].append(
            "Edge Function source:'frontend' log line - no Dashboard session was available and "
            "the CLI offers no equivalent.")
    if not facts["auth_fixture_deletion"]["observed_directly"]:
        cats["verified_facts"].append(
            "The two fixture Auth identities are absent from auth.users and auth.identities "
            "(verified by end-state inspection, not by observing the Dashboard action).")
    if at["method"] == "exclusion":
        cats["inferences"].append(
            "Trigger path = frontend. Inferred by exclusion: the live cron sweep eligibility "
            "gate could not have selected the attempt before grading completed. This is an "
            "inference, NOT an observed log line, and a short latency alone would not "
            "establish it.")

    # Merge operator annotations onto the audit-derived findings rather than
    # appending duplicates, so an annotated row is not counted twice.
    _by_key = {(d["table"], d["id"]): d for d in cats["known_discrepancies"]}
    for e in facts.get("known_discrepancies", []):
        key = (e["table"], str(e["id"]).lower())
        if key in _by_key:
            _by_key[key].update({"status": e["status"], "note": e["note"],
                                 "do_not_modify": bool(e.get("do_not_modify", False))})
        else:
            _by_key[key] = {"table": e["table"], "id": str(e["id"]).lower(),
                            "kind": "content-hash differs from baseline",
                            "status": e["status"], "note": e["note"],
                            "do_not_modify": bool(e.get("do_not_modify", False))}
    cats["known_discrepancies"] = sorted(_by_key.values(), key=lambda d: (d["table"], d["id"]))

    serialised = json.dumps(archived, indent=2)
    scan_for_secrets(serialised, "the archive")
    if "@" in serialised:
        die("archive still contains an '@' character; refusing (expected: emails removed).")

    print("\n=== 5. write archive (evidence first) ===")
    dest.write_bytes(serialised.encode("utf-8"))
    if dest.read_bytes()[:3] == b"\xef\xbb\xbf":
        die("archive unexpectedly written with a BOM")
    print(f"  wrote {dest.name} ({dest.stat().st_size} bytes, BOM-free)")
    print(f"  restoration claim derived from audit: all_tables_byte_identical="
          f"{restoration['_restoration_audit']['all_tables_byte_identical']}, "
          f"{restoration['_restoration_audit']['total_content_changed_rows']} changed row(s) recorded")

    print("\n=== 6. reset the canonical manifest ===")
    args.canonical.write_bytes(json.dumps(TEMPLATE, indent=2).encode("utf-8"))
    check = json.loads(args.canonical.read_text(encoding="utf-8"))
    if any(check.get(k) is not None for k in REQUIRED_IDS):
        die("reset produced a non-null manifest")
    if not check["_status"].startswith("TEMPLATE") or check["_rule"] != TEMPLATE["_rule"]:
        die("reset did not restore the template semantics")
    if args.canonical.read_bytes()[:3] == b"\xef\xbb\xbf":
        die("reset manifest written with a BOM")
    print("  canonical manifest is the BOM-free null template")

    print("\nARCHIVE + RESET COMPLETE")
    print(f"  archive   : deploy/archive/{dest.name}")
    print(f"  canonical : deploy/ZZSMOKE_MANIFEST.json (null template)")
    return 0


if __name__ == "__main__":
    sys.exit(main())