"""
Byte-exact baseline fingerprint of production + FIXTURE-OWNERSHIP DRIFT
classification. Entirely READ-ONLY.

  capture   - snapshot every row (id + md5) of every fixture-reachable table
  compare   - diff against the snapshot and CLASSIFY every difference:
                FIXTURE  - the row belongs to the ZZSMOKE manifest
                FOREIGN - it does NOT; i.e. concurrent real platform activity
              FOREIGN drift is never permission to delete anything. It means
              the baseline is stale and the operator must re-establish it.

Why classification matters: if a real student submits while the fixture runs,
the global fingerprint moves. That is NOT a reason to treat the fixture's own
rows as unsafe to remove, and it is NOT a reason to widen a delete predicate.
The two categories must be separated explicitly.

  classify  - (requires the manifest) prove ownership of every fixture id and
              every row that differs from the baseline.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "deploy" / "ZZSMOKE_BASELINE.json"
MANIFEST = ROOT / "deploy" / "ZZSMOKE_MANIFEST.json"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

TABLES = [
    "public.profiles", "public.exams", "public.questions", "public.exam_questions",
    "public.programming_test_cases", "public.exam_assignments", "public.exam_attempts",
    "public.answers", "public.security_events", "auth.users", "auth.identities",
]

# How a row in each table can belong to the fixture. This is the authoritative
# ownership model, mirrored by the gates in deploy/ZZSMOKE_CLEANUP.sql.
OWNERSHIP = {
    "public.profiles":            "id IN (:accounts)",
    "public.exams":               "id = :exam OR created_by IN (:accounts)",
    "public.questions":           "id = :question OR created_by IN (:accounts)",
    "public.exam_questions":      "exam_id = :exam OR question_id = :question",
    "public.programming_test_cases": "question_id = :question",
    "public.exam_assignments":    "exam_id = :exam OR student_id = :student",
    "public.exam_attempts":       "id = :attempt OR exam_id = :exam OR student_id = :student",
    "public.answers":             "attempt_id = :attempt OR question_id = :question",
    "public.security_events":     "attempt_id = :attempt OR exam_id = :exam "
                                  "OR question_id = :question OR student_id = :student",
    "auth.users":                 "id IN (:accounts)",
    "auth.identities":            "user_id IN (:accounts)",
}


def run(sql):
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    fd, path = tempfile.mkstemp(suffix=".sql")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sql)
        proc = subprocess.run(
            [npx, "supabase", "db", "query", "--linked", "--file", path],
            capture_output=True, text=True, timeout=600,
            cwd=str(ROOT), shell=os.name == "nt")
    finally:
        os.unlink(path)
    # The CLI writes a benign status line ("Initialising login role...") to
    # stderr, which is NOT an error. Judge on the response payload, not on
    # whether stderr happened to be non-empty.
    payload = proc.stdout[proc.stdout.find("{"):] if "{" in proc.stdout else proc.stdout
    try:
        data = json.loads(payload)
    except ValueError:
        raise RuntimeError(f"unparseable response: {(proc.stderr or proc.stdout or '')[:300]}")
    if "error" in data or "errors" in data:
        raise RuntimeError(f"SQL rejected: {data.get('error') or data.get('errors')}")
    if "rows" not in data:
        raise RuntimeError(f"no rows key in response: {str(data)[:200]}")
    return data["rows"]


def snapshot():
    out = {}
    for tbl in TABLES:
        rows = run(f"SELECT t.id::text AS id, md5(COALESCE(t::text,'')) AS h FROM {tbl} t ORDER BY 1")
        out[tbl] = rows
    return out


def chain(hashes):
    c = "0" * 32
    for h in hashes:
        c = hashlib.md5((c + h).encode()).hexdigest()
    return c


def capture():
    snap = snapshot()
    doc = {t: {"rows": len(snap[t]),
               "chain": chain([r["h"] for r in snap[t]]),
               "records": {r["id"]: r["h"] for r in snap[t]}} for t in TABLES}
    BASELINE.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"baseline captured -> {BASELINE}")
    for t in TABLES:
        print(f"  {t:34} rows={doc[t]['rows']:<5} chain={doc[t]['chain'][:16]}")


def load_manifest():
    if not MANIFEST.exists():
        return None
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for k in ("teacher_id", "student_id"):
        if not m.get(k) or not UUID_RE.match(str(m[k])):
            raise SystemExit(f"FAIL: manifest {k} is missing or not a UUID")
    for k in ("exam_id", "question_id", "attempt_id"):
        v = m.get(k)
        if v not in (None, "") and not UUID_RE.match(str(v)):
            raise SystemExit(f"FAIL: manifest {k}={v!r} is not a UUID")
    return m


def ownership_predicate(tbl, m):
    expr = OWNERSHIP[tbl]
    # OWNERSHIP templates already write "IN (:accounts)", so the substituted
    # value must NOT carry its own parentheses (that yields IN ((...)) and a
    # uuid = record type error).
    accounts = f"'{m['teacher_id']}'::uuid,'{m['student_id']}'::uuid"
    def lit(key):
        v = m.get(key)
        return f"'{v}'::uuid" if v else "NULL::uuid"
    return (expr.replace(":accounts", accounts)
                 .replace(":exam", lit("exam_id"))
                 .replace(":question", lit("question_id"))
                 .replace(":attempt", lit("attempt_id"))
                 .replace(":student", f"'{m['student_id']}'::uuid"))


def diff():
    """Returns (added, removed) as {table: {id: hash}} plus the classification."""
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    now = snapshot()
    added, removed = {}, {}
    for t in TABLES:
        b = base[t]["records"]
        n = {r["id"]: r["h"] for r in now[t]}
        a = {k: v for k, v in n.items() if k not in b}
        d = {k: v for k, v in b.items() if k not in n}
        if a:
            added[t] = a
        if d:
            removed[t] = d
    return added, removed


def classify(added, removed):
    """Split every difference into FIXTURE vs FOREIGN using the ownership model."""
    m = load_manifest()
    if not m:
        return None
    fixture, foreign = {}, {}
    for label, bucket in (("added", added), ("removed", removed)):
        for tbl, rows in bucket.items():
            if not rows:
                continue
            ids = list(rows)
            q = ("SELECT id::text AS id FROM " + tbl + " t WHERE " + ownership_predicate(tbl, m)
                 + " AND t.id::text = ANY(ARRAY["
                 + ",".join(f"'{i}'" for i in ids) + "])")
            # Never silently treat a failed lookup as "not owned": that would
            # manufacture false 'unattributed' alarms and, worse, could mask a
            # genuine ownership question. Fail loudly instead.
            owned = {r["id"] for r in run(q)}
            fixture.setdefault(label, {}).setdefault(tbl, {}).update(
                {i: h for i, h in rows.items() if i in owned})
            foreign.setdefault(label, {}).setdefault(tbl, {}).update(
                {i: h for i, h in rows.items() if i not in owned})
    return fixture, foreign


def report():
    added, removed = diff()
    print("=== baseline comparison ===")
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    drift = False
    for t in TABLES:
        b_rows = base[t]["rows"]
        n_rows = b_rows + len(added.get(t, {})) - len(removed.get(t, {}))
        changed = bool(added.get(t)) or bool(removed.get(t))
        drift = drift or changed
        print(f"  {t:34} {b_rows:>4} -> {n_rows:<4} {'DRIFT' if changed else 'identical'}")
    if not drift:
        print("\nPASS: production is byte-identical to the pre-fixture baseline.")
        return 0

    cl = classify(added, removed)
    print("\n=== drift classification ===")
    if cl is None:
        print("  No manifest yet - differences CANNOT be attributed to the fixture.")
        return 3
    fixture, foreign = cl
    def tally(bucket):
        # bucket is {change_kind: {table: {id: hash}}}; count actual ROWS.
        return {kind: {tbl: len(rows) for tbl, rows in tables.items() if rows}
                for kind, tables in bucket.items()}

    ft, fr = tally(fixture), tally(foreign)
    fx = sum(sum(v.values()) for v in ft.values())
    fo = sum(sum(v.values()) for v in fr.values())
    print(f"  FIXTURE-owned differences : {fx}")
    print(f"  FOREIGN  (real activity)  : {fo}")
    for label, counted in (("FIXTURE", ft), ("FOREIGN", fr)):
        for kind, tables in counted.items():
            for tbl, n in tables.items():
                print(f"    {label:8} {kind:8} {tbl:32} {n} row(s)")

    if fo:
        print("\n  STOP. Rows outside the fixture manifest changed.")
        print("  This is concurrent real platform activity, NOT fixture fallout.")
        print("  Do NOT widen any delete predicate. Re-capture the baseline only")
        print("  once the operator has confirmed the platform is idle, or run")
        print("  cleanup restricted to the exact fixture UUIDs and re-verify.")
        return 3
    print("\n  Only fixture-owned rows differ. Safe to run ZZSMOKE_CLEANUP.sql")
    print("  restricted to the manifest UUIDs, then re-run compare.")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "compare"
    if mode == "capture":
        capture()
        sys.exit(0)
    sys.exit(report())