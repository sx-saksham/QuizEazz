"""
READ-ONLY resolution of the two ZZSMOKE accounts by EXACT email.

Reports the UUID, confirmation status, application role, and the user_metadata
KEYS with only display_name surfaced. It never dumps an entire metadata blob,
because arbitrary keys could contain anything the user stored there.

No writes. No credentials required - this uses the linked-project SQL channel.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    ("zzsmoke-student-20261012@gmail.com", "ZZSMOKE Student"),
    ("zzsmoke-teacher-20261012@gmail.com", "ZZSMOKE Teacher"),
]


def query(sql):
    npx = shutil.which("npx") or shutil.which("npx.cmd")
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
        raise SystemExit(f"unparseable response: {(proc.stderr or proc.stdout or '')[:300]}")
    if "error" in payload or "errors" in payload:
        raise SystemExit(f"SQL rejected: {payload.get('error') or payload.get('errors')}")
    if "rows" not in payload:
        raise SystemExit(f"no rows in response: {str(payload)[:200]}")
    return payload["rows"]


def esc(v):
    if not isinstance(v, str) or "@" not in v:
        raise SystemExit(f"refusing to interpolate unsafe email: {v!r}")
    return "'" + v + "'"


emails = ", ".join(f"lower({esc(e)})" for e, _ in TARGETS)

rows = query(f"""
SELECT u.id::text                       AS id,
       u.email                          AS email,
       u.email_confirmed_at IS NOT NULL AS confirmed,
       (u.raw_user_meta_data->>'role')  AS raw_role,
       (u.raw_user_meta_data ? 'display_name') AS has_display_name,
       (u.raw_user_meta_data->>'display_name') AS display_name,
       jsonb_object_keys(COALESCE(u.raw_user_meta_data,'{{}}'::jsonb)) AS meta_key
  FROM auth.users u
 WHERE lower(u.email) IN ({emails})
 ORDER BY u.email, 2;
""")

# group rows back into accounts
accounts = {}
for r in rows:
    a = accounts.setdefault(r["email"], {
        "id": r["id"], "confirmed": r["confirmed"], "raw_role": r["raw_role"],
        "has_display_name": r["has_display_name"],
        "display_name": r["display_name"], "keys": set(),
    })
    a["keys"].add(r["meta_key"])

print("=== resolved accounts (exact email match) ===")
found = set(accounts)
for email, want in TARGETS:
    a = accounts.get(email)
    if not a:
        print(f"\n  {email}")
        print("    NOT FOUND - no auth.users row with this exact address")
        continue
    print(f"\n  {email}")
    print(f"    uuid                 : {a['id']}")
    print(f"    email_confirmed      : {a['confirmed']}")
    print(f"    raw_app_meta role    : {a['raw_role']}")
    print(f"    user_metadata keys   : {sorted(a['keys']) or '(none)'}")
    print(f"    display_name present : {a['has_display_name']}")
    print(f"    display_name current : {a['display_name']!r}")
    print(f"    display_name target  : {want!r}")
    same = a["display_name"] == want
    print(f"    already correct      : {same}")

# link to application profile (read-only context)
ids = [a["id"] for a in accounts.values()]
if ids:
    prof = query("SELECT p.id::text AS id, p.full_name, p.role FROM public.profiles p "
                 "WHERE p.id::text = ANY(ARRAY["
                 + ",".join(f"'{i}'" for i in ids) + "]) ORDER BY p.id;")
    print("\n=== linked application profiles (read-only; NOT modified) ===")
    for p in prof:
        print(f"  {p['id']}  full_name={p['full_name']!r}  role={p['role']!r}")

missing = [e for e, _ in TARGETS if e not in found]
print("\n" + "=" * 60)
print(f"resolved {len(found)}/{len(TARGETS)}")
if missing:
    print("MISSING: " + ", ".join(missing))
sys.exit(0)