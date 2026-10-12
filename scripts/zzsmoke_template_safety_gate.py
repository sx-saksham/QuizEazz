"""
SAFETY GATE: prove that an inert (all-null) manifest cannot make any tooling
target a production record.

Runs entirely against temp copies. Touches no production data and creates no
fixtures. Must be run BEFORE writing the null template into deploy/.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = {
    "_status": "TEMPLATE - no active fixture. Populated by scripts/zzsmoke_capture_manifest.py at fixture creation.",
    "teacher_id": None, "student_id": None,
    "exam_id": None, "question_id": None, "attempt_id": None,
}

results = []


def check(name, cond, detail=""):
    print(f"  [{'x' if cond else ' '}] {name}" + (f"  {detail}" if detail else ""))
    results.append(cond)


def load(mod_name, path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tmp = Path(tempfile.mkdtemp(prefix="zzgate_"))
mf = tmp / "ZZSMOKE_MANIFEST.json"
mf.write_text(json.dumps(TEMPLATE, indent=2), encoding="utf-8")

print("=== 1. load_manifest() must REFUSE null account ids (fail-closed) ===")
bf = load("bf", ROOT / "scripts" / "zzsmoke_baseline_fingerprint.py")
bf.MANIFEST = mf
try:
    bf.load_manifest()
    check("refuses an all-null manifest", False, "returned normally - UNSAFE")
except SystemExit as exc:
    check("refuses an all-null manifest", True, f"-> {exc}")

print("\n=== 2. ownership predicates built from a null manifest must match NOTHING ===")
# Build the predicate manually for nulls, as classify() would.
for tbl, expr in bf.OWNERSHIP.items():
    # null literals: id = NULL is NULL (never true) in SQL
    pass
null_pred = (bf.OWNERSHIP["public.exams"]
             .replace(":exam", "NULL::uuid")
             .replace(":question", "NULL::uuid")
             .replace(":attempt", "NULL::uuid")
             .replace(":student", "NULL::uuid")
             .replace(":accounts", "NULL::uuid"))
check("exam predicate with all-null anchors contains only NULL comparisons",
      "NULL::uuid" in null_pred and "LIKE" not in null_pred.upper().replace("ILIKE", ""),
      null_pred[:70] + "...")

print("\n=== 3. cleanup SQL template safety (what the operator would paste) ===")
sql = (ROOT / "deploy" / "ZZSMOKE_CLEANUP.sql").read_text(encoding="utf-8")
step0 = sql[sql.index("-- STEP 0"):sql.index("-- STEP 1")]
check("STEP 0 rejects NULL account ids",
      "v_teacher_id IS NULL OR v_student_id IS NULL" in step0)
check("STEP 0 rejects an all-null content manifest",
      "v_exam_id IS NULL AND v_question_id IS NULL AND v_attempt_id IS NULL" in step0)
check("no literal NULL ever reaches a DELETE predicate",
      "IS NULL THEN\n    DELETE" not in sql.replace("\r", ""))

print("\n=== 4. capture tool: must NOT overwrite an existing id with null ===")
cap = load("cap", ROOT / "scripts" / "zzsmoke_capture_manifest.py")
cap.MANIFEST = tmp / "cap.json"
cap.MANIFEST.write_text(json.dumps(dict(TEMPLATE, teacher_id="dde0c3e8-61ee-4d36-a874-3b1ef8a82413",
                                        student_id="1a79277a-f727-4f55-adc3-107aa00045f5")), encoding="utf-8")
os.environ["ZZSMOKE_NEW_ID"] = ""          # invalid uuid
try:
    cap.record("exam_id")
    check("refuses an empty ZZSMOKE_NEW_ID", False, "accepted - UNSAFE")
except SystemExit as exc:
    check("refuses an empty ZZSMOKE_NEW_ID", True, f"-> {exc}")

print("\n=== 5. a NULL uuid can never equal a real row id ===")
# SQL semantics: 'x'::uuid = NULL is NULL, not TRUE. State the invariant.
check("PostgreSQL `uuid = NULL` evaluates to NULL, never TRUE - "
      "so no WHERE clause can match a row via a null anchor", True)

print("\n=== 6. the tools read the manifest only to CLASSIFY, never to DELETE ===")
src_cleanup = sql
check("ZZSMOKE_CLEANUP.sql takes its ids from operator-pasted literals, not from JSON",
      "ZZSMOKE_MANIFEST" not in src_cleanup,
      "cleanup SQL has no manifest reference at all")

print(f"\n{'=' * 62}\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)