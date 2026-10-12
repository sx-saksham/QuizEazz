"""READ-ONLY: confirm every RPC named in the ZZSMOKE execution packet exists live."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NEEDED = [
    "teacher_create_question", "teacher_set_test_cases", "teacher_create_exam",
    "teacher_set_exam_paper", "teacher_set_exam_assignments", "teacher_publish_now",
    "start_or_get_exam_attempt", "save_exam_answer", "submit_exam_attempt",
    "teacher_delete_exam",
]

npx = shutil.which("npx") or shutil.which("npx.cmd")
sql = ("SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args,\n"
       "       p.prosecdef AS security_definer\n"
       "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace\n"
       " WHERE n.nspname = 'public' AND p.proname = ANY (ARRAY["
       + ",".join(f"'{n}'" for n in NEEDED) + "])\n ORDER BY p.proname;")
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
rows = json.loads(out).get("rows", [])

found = {r["proname"]: r for r in rows}
missing = [n for n in NEEDED if n not in found]
for n in NEEDED:
    r = found.get(n)
    mark = "OK  " if r else "MISS"
    print(f"  [{mark}] {n:28} ({r['args']})" if r else f"  [{mark}] {n}")

print()
print(f"{len(found)}/{len(NEEDED)} RPCs present")
if missing:
    print("MISSING:", ", ".join(missing))
sys.exit(1 if missing else 0)