"""
Read-only manifest feasibility check + exact drift attribution.

Answers two questions without writing anything:

  1. Can the existing manifest-generation process resolve ALL FIVE required
     fixture ids right now? If not, the manifest must not be written.

  2. Is the current baseline drift fully explained by the two ZZSMOKE auth
     accounts, or is there unrelated drift that has to be reported?

Attribution lists the exact row ids added/removed per table rather than only
counts, so "unrelated drift" can be identified rather than assumed absent.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "bf", ROOT / "scripts" / "zzsmoke_baseline_fingerprint.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

STUDENT = "1a79277a-f727-4f55-adc3-107aa00045f5"
TEACHER = "dde0c3e8-61ee-4d36-a874-3b1ef8a82413"
ACCOUNTS = {TEACHER, STUDENT}

npx = shutil.which("npx") or shutil.which("npx.cmd")


def query(sql):
    fd, path = tempfile.mkstemp(suffix=".sql")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sql)
        proc = subprocess.run([npx, "supabase", "db", "query", "--linked", "--file", path],
                              capture_output=True, text=True, timeout=300,
                              cwd=str(ROOT), shell=os.name == "nt")
    finally:
        os.unlink(path)
    out = proc.stdout[proc.stdout.find("{"):] if "{" in proc.stdout else proc.stdout
    try:
        payload = json.loads(out)
    except ValueError:
        raise SystemExit(f"unparseable: {(proc.stderr or proc.stdout or '')[:300]}")
    if "error" in payload or "errors" in payload:
        raise SystemExit(f"SQL rejected: {payload.get('error') or payload.get('errors')}")
    if "rows" not in payload:
        raise SystemExit(f"no rows key: {str(payload)[:200]}")
    return payload["rows"]


acct = ",".join(f"'{u}'" for u in sorted(ACCOUNTS))

print("=== Q1: can all five fixture ids be resolved? ===")
resolvable = {
    "teacher_id": len(query(f"SELECT 1 FROM auth.users WHERE id='{TEACHER}'::uuid")) == 1,
    "student_id": len(query(f"SELECT 1 FROM auth.users WHERE id='{STUDENT}'::uuid")) == 1,
    "exam_id":     len(query(f"SELECT 1 FROM public.exams WHERE id IN "
                             f"(SELECT id FROM public.exams WHERE created_by IN ({acct}) "
                             f"OR title ILIKE 'zzsmoke%')")) > 0,
    "question_id": len(query(f"SELECT 1 FROM public.questions WHERE id IN "
                             f"(SELECT id FROM public.questions WHERE created_by IN ({acct}) "
                             f"OR title ILIKE 'zzsmoke%')")) > 0,
    "attempt_id":  len(query(f"SELECT 1 FROM public.exam_attempts WHERE id IN "
                             f"(SELECT id FROM public.exam_attempts WHERE student_id IN ({acct}))")) > 0,
}
for k, ok in resolvable.items():
    print(f"  {k:12} {'resolvable  ' if ok else 'NOT RESOLVABLE'}")
missing = [k for k, ok in resolvable.items() if not ok]
print(f"\n  {len(resolvable) - len(missing)}/5 resolvable; missing: {missing or 'none'}")
if missing:
    print("  -> The existing manifest-generation process CANNOT safely resolve all five.")
    print("     deploy/ZZSMOKE_MANIFEST.json will NOT be written (would be fabricated).")

print("\n=== Q2: exact drift attribution ===")
base = json.loads(bf.BASELINE.read_text(encoding="utf-8"))
now = bf.snapshot()
manifest = bf.load_manifest()
print(f"\n  manifest loaded: teacher={bool(manifest and manifest.get('teacher_id'))} "
      f"exam={bool(manifest and manifest.get('exam_id'))} "
      f"question={bool(manifest and manifest.get('question_id'))} "
      f"attempt={bool(manifest and manifest.get('attempt_id'))}")


def owned_ids(table, ids):
    """Rows of `table` owned by the fixture, per bf.OWNERSHIP."""
    if not manifest or not ids:
        return set()
    q = ("SELECT id::text AS id FROM " + table + " t WHERE "
         + bf.ownership_predicate(table, manifest) + " AND t.id::text = ANY(ARRAY["
         + ",".join(f"'{i}'" for i in ids) + "])")
    return {r["id"] for r in bf.run(q)}


unattributed = []
for t in bf.TABLES:
    b = base[t]["records"]
    n = {r["id"]: r["h"] for r in now[t]}
    added = sorted(set(n) - set(b))
    removed = sorted(set(b) - set(n))
    changed = sorted(k for k in (set(b) & set(n)) if b[k] != n[k])
    if not (added or removed or changed):
        continue
    print(f"\n  {t}")
    owned = owned_ids(t, added + changed)
    for i in added:
        ok = i in owned
        if not ok:
            unattributed.append((t, "added", i))
        print(f"    + {i}  [{'FIXTURE' if ok else 'UNATTRIBUTED'}]")
    for i in removed:
        unattributed.append((t, "removed", i))
        print(f"    - {i}  [UNATTRIBUTED - a pre-existing row disappeared]")
    for i in changed:
        ok = i in owned
        if not ok:
            unattributed.append((t, "modified", i))
        print(f"    ~ {i}  [{'FIXTURE' if ok else 'UNATTRIBUTED - pre-existing row modified'}]")

print("\n" + "=" * 62)
if unattributed:
    print(f"UNATTRIBUTED DRIFT PRESENT: {len(unattributed)} row(s)")
    for t, kind, i in unattributed:
        print(f"  {t}: {kind} {i}")
    print("\nThis must be reported, not guessed at. No manifest written.")
    sys.exit(3)
missing_now = [k for k in ("exam_id", "question_id", "attempt_id")
               if not (manifest or {}).get(k)]
print("All drift is attributable to the ZZSMOKE fixture. No pre-existing row")
print("was removed or modified.")
if missing_now:
    print(f"Manifest still incomplete: {missing_now} not yet recorded.")
else:
    print("Manifest carries all five fixture ids.")