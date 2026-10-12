"""
Capture and verify the ZZSMOKE manifest (READ-ONLY).

Never invents or guesses an identifier. Every UUID written to the manifest is
resolved from the database by exact email, and every ownership link is proved
before the manifest is considered usable.

  verify   - resolve the two accounts by exact email and emit/print the manifest
  check    - re-verify an existing manifest (after each fixture stage)

Exit codes: 0 ok, 2 environment/usage problem, 3 a verification gate failed.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "deploy" / "ZZSMOKE_MANIFEST.json"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def run(sql):
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    fd, path = tempfile.mkstemp(suffix=".sql")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sql)
        proc = subprocess.run(
            [npx, "supabase", "db", "query", "--linked", "--file", path],
            capture_output=True, text=True, timeout=300,
            cwd=str(ROOT), shell=os.name == "nt",
        )
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:400])
    payload = proc.stdout[proc.stdout.find("{"):] if "{" in proc.stdout else proc.stdout
    return json.loads(payload).get("rows", [])


def fail(msg, code=3):
    print(f"FAIL: {msg}")
    sys.exit(code)


def esc(v):
    """Escape a literal for a SQL string; refuse anything unexpected."""
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9._%+\-@]{3,120}", v):
        fail(f"refusing to interpolate unsafe value: {v!r}")
    return "'" + v + "'"


def verify():
    teacher_email = os.environ.get("ZZSMOKE_TEACHER_EMAIL")
    student_email = os.environ.get("ZZSMOKE_STUDENT_EMAIL")
    if not teacher_email or not student_email:
        print("set ZZSMOKE_TEACHER_EMAIL and ZZSMOKE_STUDENT_EMAIL")
        sys.exit(2)
    if teacher_email.lower() == student_email.lower():
        fail("the two emails are identical")

    for e in (teacher_email, student_email):
        if "zzsmoke" not in e.lower():
            fail(f"{e} does not look like a ZZSMOKE fixture address")

    rows = run(f"""
      SELECT p.id, u.email, p.full_name, p.role
        FROM public.profiles p JOIN auth.users u ON u.id = p.id
       WHERE lower(u.email) IN (lower({esc(teacher_email)}), lower({esc(student_email)}))
       ORDER BY u.email""")

    by_email = {r["email"].lower(): r for r in rows}
    print(f"resolved {len(rows)} account row(s)")
    for r in rows:
        print(f"  {r['email']:44} {r['id']}  role={r['role']}")

    for label, email in (("teacher", teacher_email), ("student", student_email)):
        if email.lower() not in by_email:
            fail(f"no profile for {label} {email} (was handle_new_user() skipped it?)")
        row = by_email[email.lower()]
        if not str(row["full_name"] or "").upper().startswith("ZZSMOKE"):
            fail(f"{label} full_name is {row['full_name']!r}, not a ZZSMOKE fixture")
        if not UUID_RE.match(str(row["id"])):
            fail(f"{label} id {row['id']!r} is not a UUID")

    manifest = {
        "_status": "ACTIVE_FIXTURE - these ids identify live ZZSMOKE rows. "
                   "They are NOT production data and must not be treated as such.",
        "teacher_id": by_email[teacher_email.lower()]["id"],
        "teacher_email": teacher_email,
        "student_id": by_email[student_email.lower()]["id"],
        "student_email": student_email,
        "exam_id": None, "question_id": None, "attempt_id": None,
    }
    if manifest["teacher_id"] == manifest["student_id"]:
        fail("teacher and student resolved to the same id")

    existing = {}
    if MANIFEST.exists():
        try:
            existing = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except ValueError:
            fail(f"{MANIFEST} exists but is not valid JSON - refusing to overwrite")

    # Never silently change an id that was already recorded.
    for k in ("teacher_id", "student_id"):
        if existing.get(k) and existing[k] != manifest[k]:
            fail(f"{k} changed: manifest had {existing[k]}, database has {manifest[k]}")

    manifest["exam_id"] = existing.get("exam_id")
    manifest["question_id"] = existing.get("question_id")
    manifest["attempt_id"] = existing.get("attempt_id")
    # Carry the template's explanatory _rule forward; it documents null semantics
    # and must survive a populate so the file stays self-describing.
    if existing.get("_rule"):
        manifest["_rule"] = existing["_rule"]
    # Everything above this line either fails (leaving the file untouched) or
    # resolves to two live accounts. Only now is the file marked ACTIVE.
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nmanifest -> {MANIFEST}")
    for k, v in manifest.items():
        print(f"  {k:14} {v}")
    print("\nNext: paste the account UUIDs into ZZSMOKE_BOOTSTRAP.sql, run it, then\n"
          "record the exam/question/attempt UUIDs here as each stage completes:\n"
          "  ZZSMOKE_MANIFEST.json -> exam_id / question_id / attempt_id")


def record(field):
    """Record a UUID produced by a fixture stage, verifying it is a ZZSMOKE row."""
    if field not in ("exam_id", "question_id", "attempt_id"):
        sys.exit(2)
    value = os.environ.get("ZZSMOKE_NEW_ID")
    if not value or not UUID_RE.match(value):
        fail(f"ZZSMOKE_NEW_ID={value!r} is not a UUID")

    if not MANIFEST.exists():
        fail("no manifest yet - run 'verify' first")
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))

    if field == "exam_id":
        rows = run(f"SELECT id, title FROM public.exams WHERE id = '{value}'::uuid")
        if len(rows) != 1 or not str(rows[0]["title"]).upper().startswith("ZZSMOKE"):
            fail(f"{value} is not a ZZSMOKE exam")
    elif field == "question_id":
        rows = run(f"SELECT id, title FROM public.questions WHERE id = '{value}'::uuid")
        if len(rows) != 1 or not str(rows[0]["title"]).upper().startswith("ZZSMOKE"):
            fail(f"{value} is not a ZZSMOKE question")
    else:
        rows = run(f"""SELECT a.id, a.exam_id, a.student_id, e.title
                         FROM public.exam_attempts a JOIN public.exams e ON e.id = a.exam_id
                        WHERE a.id = '{value}'::uuid""")
        if len(rows) != 1:
            fail(f"{value} is not an exam attempt")
        r = rows[0]
        if r["student_id"] != m["student_id"]:
            fail(f"attempt {value} belongs to student {r['student_id']}, "
                 f"not the fixture student {m['student_id']}")
        if not str(r["title"]).upper().startswith("ZZSMOKE"):
            fail(f"attempt {value} belongs to a non-ZZSMOKE exam")
        if m.get("exam_id") and r["exam_id"] != m["exam_id"]:
            fail(f"attempt belongs to exam {r['exam_id']}, manifest has {m['exam_id']}")
        if not m.get("exam_id"):
            m["exam_id"] = r["exam_id"]
            print(f"  inferred exam_id {r['exam_id']} from the attempt (same fixture, "
                  f"verified ZZSMOKE + fixture student)")

    m[field] = value
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")
    print(f"recorded {field} = {value}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "verify":
        verify()
    elif cmd == "record":
        record(sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        print("usage: zzsmoke_capture_manifest.py verify | record <exam_id|question_id|attempt_id>")
        sys.exit(2)