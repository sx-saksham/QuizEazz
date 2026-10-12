"""
Tests for the ACTIVE_FIXTURE manifest marker in zzsmoke_capture_manifest.py.

Fully offline: `run()` is replaced by a fake, every manifest path is a temp
file, and no SQL, production connection or fixture id is involved.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []


def check(name, cond, detail=""):
    print(f"  [{'x' if cond else ' '}] {name}" + (f"\n        {detail}" if detail else ""))
    results.append(cond)


def load(tag, path):
    spec = importlib.util.spec_from_file_location(tag, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TEMPLATE = {
    "_status": "TEMPLATE - no active fixture.",
    "_rule": "A null id means 'not yet created'. Tooling refuses to classify or delete against it.",
    "teacher_id": None, "student_id": None,
    "exam_id": None, "question_id": None, "attempt_id": None,
}
TEACHER_UUID = "dde0c3e8-61ee-4d36-a874-3b1ef8a82413"
STUDENT_UUID = "1a79277a-f727-4f55-adc3-107aa00045f5"
TEACHER_MAIL = "zzsmoke-teacher-20261012@example.test"
STUDENT_MAIL = "zzsmoke-student-20261012@example.test"


def fresh():
    """A capture module pointed at a temp manifest + a fake DB resolver."""
    d = Path(tempfile.mkdtemp(prefix="zzcap_"))
    cap = load(f"cap{id(d)}", ROOT / "scripts" / "zzsmoke_capture_manifest.py")
    cap.MANIFEST = d / "ZZSMOKE_MANIFEST.json"
    cap.MANIFEST.write_text(json.dumps(TEMPLATE, indent=2), encoding="utf-8")
    return cap, cap.MANIFEST


def fake_rows(rows):
    cap, mf = fresh()

    def run(sql):
        if "FROM public.profiles" in sql:
            return rows
        return []
    cap.run = run
    return cap, mf


def expect_exit(fn, *a):
    try:
        fn(*a)
        return None
    except SystemExit as e:
        return e


print("=== T1  successful verify writes ACTIVE status and preserves _rule ===")
cap, mf = fake_rows([
    {"email": TEACHER_MAIL, "id": TEACHER_UUID, "full_name": "zzsmoke-teacher-20261012", "role": "student"},
    {"email": STUDENT_MAIL, "id": STUDENT_UUID, "full_name": "zzsmoke-student-20261012", "role": "student"},
])
os.environ["ZZSMOKE_TEACHER_EMAIL"] = TEACHER_MAIL
os.environ["ZZSMOKE_STUDENT_EMAIL"] = STUDENT_MAIL
cap.verify()
out = json.loads(mf.read_text(encoding="utf-8"))
check("_status marked ACTIVE_FIXTURE", str(out.get("_status", "")).startswith("ACTIVE_FIXTURE"), out.get("_status"))
check("_rule preserved from the template", out.get("_rule") == TEMPLATE["_rule"])
check("teacher_id written", out["teacher_id"] == TEACHER_UUID)
check("student_id written", out["student_id"] == STUDENT_UUID)
check("emails written", out["teacher_email"] == TEACHER_MAIL and out["student_email"] == STUDENT_MAIL)
check("content ids still null", out["exam_id"] is None and out["question_id"] is None and out["attempt_id"] is None)
check("file is BOM-free JSON", mf.read_bytes()[:3] != b"\xef\xbb\xbf")

print("\n=== T1b  record() preserves _status and _rule ===")
os.environ["ZZSMOKE_NEW_ID"] = "1615124b-3645-443b-b23f-7321610bfc54"
cap.run = lambda sql: [{"id": os.environ["ZZSMOKE_NEW_ID"], "title": "ZZSMOKE Exam"}]
cap.record("exam_id")
out2 = json.loads(mf.read_text(encoding="utf-8"))
check("_status survives record()", str(out2.get("_status", "")).startswith("ACTIVE_FIXTURE"))
check("_rule survives record()", out2.get("_rule") == TEMPLATE["_rule"])
check("exam_id recorded", out2["exam_id"] == os.environ["ZZSMOKE_NEW_ID"])

print("\n=== T2  null template is still rejected by load_manifest() ===")
bf = load("bf", ROOT / "scripts" / "zzsmoke_baseline_fingerprint.py")
d = Path(tempfile.mkdtemp(prefix="zzbf_"))
(d / "M.json").write_text(json.dumps(TEMPLATE), encoding="utf-8")
bf.MANIFEST = d / "M.json"
try:
    bf.load_manifest()
    check("load_manifest refuses the null template", False, "returned a model!")
except SystemExit as e:
    check("load_manifest refuses the null template", True, str(e))

print("\n=== T3  invalid / mismatched ids cannot overwrite an existing manifest ===")
# 3a - a DIFFERENT teacher id already recorded
cap, mf = fake_rows([
    {"email": TEACHER_MAIL, "id": "00000000-0000-0000-0000-0000000000ff", "full_name": "zzsmoke-t", "role": "student"},
    {"email": STUDENT_MAIL, "id": STUDENT_UUID, "full_name": "zzsmoke-s", "role": "student"},
])
mf.write_text(json.dumps({**TEMPLATE, "teacher_id": TEACHER_UUID, "_status": "ACTIVE_FIXTURE"}), encoding="utf-8")
before = mf.read_text(encoding="utf-8")
e = expect_exit(cap.verify)          # exactly one invocation
after = mf.read_text(encoding="utf-8")
check("mismatched teacher_id refuses AND leaves the file byte-identical",
      e is not None and before == after,
      f"exit {getattr(e,'code',None)}; file " + ("modified" if before != after else "unchanged"))

# 3b - non-ZZSMOKE email is refused
cap, mf = fake_rows([])
os.environ["ZZSMOKE_TEACHER_EMAIL"] = "real.person@example.test"
before = mf.read_text(encoding="utf-8")
e = expect_exit(cap.verify)
after = mf.read_text(encoding="utf-8")
check("non-zzsmoke email refused", e is not None, f"exit {getattr(e,'code',None)}")
check("  ...and the file is byte-identical to before", before == after,
      "file was modified" if before != after else "unchanged")
os.environ["ZZSMOKE_TEACHER_EMAIL"] = TEACHER_MAIL

print("\n=== T4  a failed verification cannot leave a misleading ACTIVE status ===")
scenarios = [
    ("account missing from DB", [], TEACHER_MAIL, STUDENT_MAIL),
    ("profile name not a fixture",
     [{"email": TEACHER_MAIL, "id": TEACHER_UUID, "full_name": "Real Person", "role": "student"},
      {"email": STUDENT_MAIL, "id": STUDENT_UUID, "full_name": "zzsmoke-s", "role": "student"}],
     TEACHER_MAIL, STUDENT_MAIL),
    ("id is not a UUID",
     [{"email": TEACHER_MAIL, "id": "not-a-uuid", "full_name": "zzsmoke-t", "role": "student"},
      {"email": STUDENT_MAIL, "id": STUDENT_UUID, "full_name": "zzsmoke-s", "role": "student"}],
     TEACHER_MAIL, STUDENT_MAIL),
    ("teacher and student identical",
     [{"email": TEACHER_MAIL, "id": TEACHER_UUID, "full_name": "zzsmoke-t", "role": "student"},
      {"email": STUDENT_MAIL, "id": TEACHER_UUID, "full_name": "zzsmoke-s", "role": "student"}],
     TEACHER_MAIL, STUDENT_MAIL),
]
for label, rows, t, s in scenarios:
    cap, mf = fresh()
    cap.run = lambda sql, r=rows: r
    os.environ["ZZSMOKE_TEACHER_EMAIL"] = t
    os.environ["ZZSMOKE_STUDENT_EMAIL"] = s
    before = mf.read_text(encoding="utf-8")
    e = expect_exit(cap.verify)
    after = mf.read_text(encoding="utf-8")
    check(f"refused + file untouched: {label}", e is not None and before == after)

print("\n=== T5  archival restores the template state ===")
# Simulate the post-cleanup reset that Phase 1 Step 7 performs.
cap, mf = fresh()
cap.run = lambda sql: [
    {"email": TEACHER_MAIL, "id": TEACHER_UUID, "full_name": "zzsmoke-t", "role": "student"},
    {"email": STUDENT_MAIL, "id": STUDENT_UUID, "full_name": "zzsmoke-s", "role": "student"}]
cap.verify()
active = json.loads(mf.read_text(encoding="utf-8"))
check("after verify: ACTIVE and ids populated",
      bool(str(active["_status"]).startswith("ACTIVE_FIXTURE") and active["teacher_id"]))
# archival reset = template written back
mf.write_text(json.dumps(TEMPLATE, indent=2), encoding="utf-8")
reset = json.loads(mf.read_text(encoding="utf-8"))
check("after reset: no ACTIVE marker", not str(reset["_status"]).startswith("ACTIVE_FIXTURE"))
check("after reset: all ids null", all(reset[k] is None for k in ("teacher_id","student_id","exam_id","question_id","attempt_id")))
bf.MANIFEST = mf
try:
    bf.load_manifest()
    check("after reset: load_manifest refuses again", False, "accepted a template!")
except SystemExit as e:
    check("after reset: load_manifest refuses again", True, str(e))

print(f"\n{'=' * 64}\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)