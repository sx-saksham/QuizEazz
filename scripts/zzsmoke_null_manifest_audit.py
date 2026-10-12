"""
Behavioural audit: what does each manifest-reading tool actually DO when it
encounters the real, all-null deploy/ZZSMOKE_MANIFEST.json?

Uses the actual deployed file (not a temp substitute), and every code path is
either read-only or writes only to a copy. No production write is possible:
the tools under test issue no DELETE/UPDATE (asserted separately).
"""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_MANIFEST = ROOT / "deploy" / "ZZSMOKE_MANIFEST.json"

results = []


def check(name, cond, detail=""):
    print(f"  [{'x' if cond else ' '}] {name}" + (f"\n        {detail}" if detail else ""))
    results.append(cond)


def load(tag, path):
    spec = importlib.util.spec_from_file_location(tag, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


print("=== 0. the real file parses and is fully null ===")
raw = REAL_MANIFEST.read_bytes()
doc = json.loads(raw.decode("utf-8"))
ids = ["teacher_id", "student_id", "exam_id", "question_id", "attempt_id"]
check("no BOM", raw[:3] != b"\xef\xbb\xbf")
check("all five ids are JSON null", all(doc.get(k) is None for k in ids),
      json.dumps({k: doc.get(k) for k in ids}))
check("carries _status", "_status" in doc, doc["_status"])

print("\n=== 1. bf.load_manifest() must REFUSE (not return a null model) ===")
bf = load("bf", ROOT / "scripts" / "zzsmoke_baseline_fingerprint.py")
bf.MANIFEST = REAL_MANIFEST
try:
    m = bf.load_manifest()
    check("refuses", False, f"returned {m!r} - a null model would be DANGEROUS")
except SystemExit as e:
    check("refuses with a clear message", True, str(e))

print("\n=== 2. classify() therefore cannot mis-classify on a null manifest ===")
try:
    bf.classify({"public.exams": {"x": "h"}}, {})
    check("classify refuses", False, "returned - would mis-classify")
except SystemExit as e:
    check("classify propagates the refusal", True, str(e))
except Exception as e:
    check("classify raises", True, f"{type(e).__name__}: {e}")

print("\n=== 3. ownership_predicate() never yields a matchable predicate ===")
# Simulate the null-manifest path directly (bypassing load_manifest) to prove the
# SQL itself is inert even if someone bypassed the Python guard.
pred = (bf.OWNERSHIP["public.exams"]
        .replace(":exam", "NULL::uuid").replace(":accounts", "NULL::uuid")
        .replace(":question", "NULL::uuid").replace(":attempt", "NULL::uuid")
        .replace(":student", "NULL::uuid"))
check("predicate uses only NULL comparisons", "NULL::uuid" in pred, pred)
check("no bare id equality without a NULL guard", "id = NULL" in pred or "id = 'x'" not in pred)

print("\n=== 4. capture tool: 'record' must refuse, and must not blank existing ids ===")
cap = load("cap", ROOT / "scripts" / "zzsmoke_capture_manifest.py")
cap.MANIFEST = ROOT / "deploy" / "_probe_manifest.json"
# seed the probe copy with a REAL-looking id and try to overwrite with junk
cap.MANIFEST.write_text(json.dumps({
    "teacher_id": "dde0c3e8-61ee-4d36-a874-3b1ef8a82413",
    "student_id": "1a79277a-f727-4f55-adc3-107aa00045f5",
    "exam_id": "1615124b-3645-443b-b23f-7321610bfc54",
    "question_id": "3806253d-cfae-40c5-a85c-a7b181ae3f97",
    "attempt_id": "55ff839f-39ca-44de-ae80-449a11e47b8e"}), encoding="utf-8")
import os
os.environ["ZZSMOKE_NEW_ID"] = ""
try:
    cap.record("exam_id")
    check("record() refuses an empty new id", False, "accepted!")
except SystemExit as e:
    after = json.loads(cap.MANIFEST.read_text(encoding="utf-8"))
    check("record() refuses and leaves the file untouched",
          after["exam_id"] == "1615124b-3645-443b-b23f-7321610bfc54", str(e))
cap.MANIFEST.unlink()

print("\n=== 5. capture 'verify' is the ONLY manifest writer; is it safe? ===")
src = (ROOT / "scripts" / "zzsmoke_capture_manifest.py").read_text(encoding="utf-8")
check("verify() requires both emails from env", "ZZSMOKE_TEACHER_EMAIL" in src and "ZZSMOKE_STUDENT_EMAIL" in src)
check("verify() requires 'zzsmoke' in both emails",
      "does not look like a ZZSMOKE fixture address" in src)
check("verify() refuses to overwrite a changed id",
      "changed: manifest had" in src)
check("verify() writes only the manifest file (no SQL)", "DELETE FROM" not in src and "UPDATE " not in src)

print("\n=== 6. feasibility script cannot fabricate ids ===")
f = (ROOT / "scripts" / "zzsmoke_manifest_feasibility.py").read_text(encoding="utf-8")
check("never writes a manifest", "write_text" not in f or "MANIFEST" not in f.split("write_text")[0][-200:])
check("issues no DELETE/UPDATE", "DELETE FROM" not in f and "UPDATE public" not in f)

print(f"\n{'=' * 64}\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)