"""
Offline test of the drift-classification DECISION LOGIC in
zzsmoke_baseline_fingerprint.py.

This must never touch production: the module's `run()` transport and the
snapshot source are replaced with deterministic fakes, and only the
classification / exit-code behaviour is exercised.

The property that matters: a difference that is NOT attributable to the fixture
must never be treated as permission to delete anything.
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "bf", ROOT / "scripts" / "zzsmoke_baseline_fingerprint.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

FIX = {"teacher_id": "aaaa1111-1111-1111-1111-111111111111",
       "student_id": "bbbb2222-2222-2222-2222-222222222222",
       "exam_id": "cccc3333-3333-3333-3333-333333333333",
       "question_id": "dddd4444-4444-4444-4444-444444444444",
       "attempt_id": "eeee5555-5555-5555-5555-555555555555"}

# Row ids that the fake ownership model recognises as the fixture's own.
OWNED = {"aaaa1111-1111-1111-1111-111111111111",
         "bbbb2222-2222-2222-2222-222222222222",
         "cccc3333-3333-3333-3333-333333333333",
         "dddd4444-4444-4444-4444-444444444444",
         "eeee5555-5555-5555-5555-555555555555"}
FOREIGN_ID = "ffff9999-9999-9999-9999-999999999999"


def fake_run(sql):
    """Answer only the ownership query; anything else returns nothing."""
    if "SELECT id::text AS id FROM" in sql:
        ids = []
        for cand in OWNED:
            if cand in sql:
                ids.append({"id": cand})
        return ids
    return []


bf.run = fake_run


def base_doc(added_counts):
    doc = {t: {"rows": 1, "chain": "x", "records": {"seed-" + t: "h0"}}
           for t in bf.TABLES}
    return doc


def install(tmp_manifest, added):
    bf.BASELINE = Path(tmp_manifest).with_suffix(".baseline.json")
    bf.BASELINE.write_text(json.dumps(base_doc(added)), encoding="utf-8")
    bf.MANIFEST = Path(tmp_manifest)
    if FIX is None:
        bf.MANIFEST = Path("/nonexistent/manifest.json")
    else:
        bf.MANIFEST.write_text(json.dumps(FIX), encoding="utf-8")
    # snapshot() returns one seed row per table plus the 'added' rows
    def fake_snapshot():
        out = {}
        for t in bf.TABLES:
            rows = [{"id": f"seed-{t}", "h": "h0"}]
            rows += [{"id": i, "h": f"h-{i[:4]}"} for i in added.get(t, [])]
            out[t] = rows
        return out
    bf.snapshot = fake_snapshot


results = []


def check(name, cond):
    print(f"  [{'x' if cond else ' '}] {name}")
    results.append(cond)


print("=== drift classification ===\n")

print("case 1 - every added row is fixture-owned")
install("m1", {"public.profiles": [FIX["teacher_id"]],
               "public.exams": [FIX["exam_id"]]})
rc = bf.report()
check("exits 0 (safe to proceed)", rc == 0)
fixture_bucket, foreign_bucket = bf.classify(
    {"public.profiles": {FIX["teacher_id"]: "h1"},
     "public.exams": {FIX["exam_id"]: "h2"}}, {})
fx_rows = [i for d in fixture_bucket["added"].values() for i in d]
fo_rows = [i for d in foreign_bucket["added"].values() for i in d]
check(f"both rows classified FIXTURE ({len(fx_rows)}), none FOREIGN",
      len(fx_rows) == 2 and len(fo_rows) == 0)

print("\ncase 2 - one added row belongs to real activity")
install("m2", {"public.profiles": [FIX["teacher_id"]],
               "public.exam_attempts": [FOREIGN_ID]})
rc = bf.report()
check("exits 3 (STOP)", rc == 3)
check("no fixture-owned claim over the foreign row",
      "exam_attempts" in json.dumps(bf.classify(
          {"public.exam_attempts": {FOREIGN_ID: "h"}}, {})[1]))

print("\ncase 3 - no manifest exists")
bf.MANIFEST = Path("/nonexistent/manifest.json")
install("m3", {"public.exams": [FIX["exam_id"]]})
bf.MANIFEST = Path("/nonexistent/manifest.json")
rc = bf.report()
check("exits 3 (cannot attribute drift)", rc == 3)

print("\ncase 4 - manifest with a malformed UUID is rejected outright")
import tempfile, os
p = Path(tempfile.mkstemp(suffix=".json")[1])
p.write_text(json.dumps({**FIX, "exam_id": "not-a-uuid"}), encoding="utf-8")
bf.MANIFEST = p
try:
    bf.load_manifest()
    check("rejects malformed UUID", False)
except SystemExit as exc:
    check(f"rejects malformed UUID ({exc})", True)

print(f"\n{'=' * 60}\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)