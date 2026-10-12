"""
Gate 1 read-only check: does the fixture-ownership model account for every
table that could hold fixture-owned rows?

Scope is deliberate. Two disjoint classes of table must both be accounted for:

  APPLICATION tables - rows our SQL cleanup deletes. Every one MUST have an
    ownership rule in zzsmoke_baseline_fingerprint.OWNERSHIP, or both cleanup
    and drift classification could ignore a leftover.

  AUTH child tables - owned entirely by auth.users. These are NOT deleted by
    our SQL (no direct auth writes); admin.deleteUser() removes them. They are
    only a risk if their FK is NO ACTION, which would make deleteUser() fail.
    Every auth child is therefore classified by its ON DELETE rule.
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

APP_ROOTS = ["public.profiles", "public.exams", "public.questions", "public.exam_attempts"]
DEL = {"c": "CASCADE", "a": "NO ACTION", "r": "RESTRICT",
       "n": "SET NULL", "d": "SET DEFAULT"}

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
        # "Initialising login role..." is a status line on stderr, not a failure.
        raise RuntimeError(f"unparseable response: {(proc.stderr or proc.stdout or '')[:300]}")
    if "error" in payload or "errors" in payload:
        raise RuntimeError(f"SQL rejected: {payload.get('error') or payload.get('errors')}")
    if "rows" not in payload:
        raise RuntimeError(f"no rows key in response: {str(payload)[:200]}")
    return payload["rows"]


def norm(name):
    """regclass::text omits the schema for the search_path; restore it."""
    return name if "." in name else f"public.{name}"


failures = []

# ---- 1. application tables -------------------------------------------
print("=== application tables reachable from a fixture root ===")
rows = query(f"""SELECT DISTINCT conrelid::regclass::text AS child
                   FROM pg_constraint
                  WHERE contype = 'f'
                    AND confrelid::regclass::text = ANY (ARRAY[{",".join(chr(39) + r.split(".")[-1] + chr(39) for r in APP_ROOTS)}])
                  ORDER BY 1;""")
reachable = sorted({norm(r["child"]) for r in rows})
for t in reachable:
    if t in bf.OWNERSHIP:
        print(f"  [covered]   {t}")
    else:
        print(f"  [UNCOVERED] {t}")
        failures.append(f"no ownership rule for {t}")

# ---- 2. auth children -------------------------------------------------
print("\n=== auth child tables and their ON DELETE rule ===")
rows = query("""SELECT conrelid::regclass::text AS child, confdeltype AS del
                  FROM pg_constraint
                 WHERE contype = 'f' AND confrelid = 'auth.users'::regclass
                 ORDER BY 1;""")
blocking = []
for r in rows:
    rule = DEL.get(r["del"], r["del"])
    note = ""
    # Only NO ACTION / RESTRICT actually block the delete. CASCADE and
    # SET NULL both let auth.users be removed cleanly.
    if r["del"] in ("a", "r"):
        note = "  <-- BLOCKS admin.deleteUser()"
        blocking.append((r["child"], rule))
    elif r["del"] != "c":
        note = "  (SET NULL - does not block)"
    print(f"  [auth]      {r['child']:32} {rule}{note}")

print("\n=== auth rows tracked by the fingerprint ===")
for t in ("auth.users", "auth.identities"):
    print(f"  {'[tracked]  ' if t in bf.OWNERSHIP else '[missing]  '}{t}")

# ---- 3. verdict -------------------------------------------------------
print("\n" + "=" * 62)
print(f"application tables reachable : {len(reachable)}")
print(f"  covered by ownership model : {sum(1 for t in reachable if t in bf.OWNERSHIP)}")
print(f"  uncovered                  : {sum(1 for f in failures)}")
print(f"auth children                : {len(rows)}")
print(f"  cascading (deleted by deleteUser) : {len(rows) - len(blocking)}")
print(f"  NOT cascading (would block)       : {len(blocking)}")

if failures:
    print("\nFAIL: missing ownership rules:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nPASS: every application table reachable from a fixture root has an")
print("      ownership rule. Auth children are API-managed by admin.deleteUser().")
if blocking:
    print("\nNOTE - auth children that would block admin.deleteUser() if populated:")
    for name, rule in blocking:
        print(f"  - {name} ({rule})")
    print("  The fixture accounts are password-only, so none are expected.")
    print("  Zero rows present is re-verified at cleanup time.")
else:
    print("      All auth children CASCADE; no deleteUser() blocker exists.")