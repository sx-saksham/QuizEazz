"""
Scope test for the ZZSMOKE case-sensitivity fix.

Does NOT edit deploy/ZZSMOKE_CLEANUP.sql. It writes temporary variants to a
scratch directory and runs them against a disposable replica seeded with the
REAL current fixture names (lowercase 'zzsmoke-...'), i.e. exactly the
production situation.

Variants:
  A  original, all case-sensitive LIKE
  B  only the two CLASSIFICATION gates made case-insensitive (lines 110/121)
  C  all four profile checks made case-insensitive (110/121/329/337)
  D  all six made case-insensitive (adds exam/question titles)
"""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg  # noqa: E402
from validate_cleanup_script import SCHEMA  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "deploy" / "ZZSMOKE_CLEANUP.sql"
DSN = os.environ.get("ZZSMOKE_VALIDATION_DSN")
if not DSN:
    print("set ZZSMOKE_VALIDATION_DSN")
    sys.exit(2)

T = "11111111-1111-1111-1111-00000000000"
FIX = {k: f"{T}{v}" for k, v in dict(t="1", s="2", ex="6", q="8", a="a").items()}

# The REAL fixture names in production right now: lowercase.
SEED = f"""
SELECT set_config('quizeazz.authoritative_context','true',false);
INSERT INTO auth.users VALUES ('{FIX["t"]}'), ('{FIX["s"]}'), ('{T}3');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{FIX["t"]}','zzsmoke-teacher-20261012','teacher'),
  ('{FIX["s"]}','zzsmoke-student-20261012','student'),
  ('{T}3','Real Student','student');
INSERT INTO public.exams (id, title, created_by) VALUES ('{FIX["ex"]}','ZZSMOKE Exam','{FIX["t"]}');
INSERT INTO public.questions (id, title, created_by) VALUES ('{FIX["q"]}','ZZSMOKE Question','{FIX["t"]}');
INSERT INTO public.programming_test_cases (id, question_id, expected_stdout)
  VALUES ('aa000000-0000-0000-0000-000000000008','{FIX["q"]}','42');
INSERT INTO public.exam_questions (id, exam_id, question_id)
  VALUES ('bb000000-0000-0000-0000-000000000008','{FIX["ex"]}','{FIX["q"]}');
INSERT INTO public.exam_assignments (exam_id, student_id) VALUES ('{FIX["ex"]}','{FIX["s"]}');
INSERT INTO public.exam_attempts (id, exam_id, student_id, status)
  VALUES ('{FIX["a"]}','{FIX["ex"]}','{FIX["s"]}','evaluated');
INSERT INTO public.answers (id, attempt_id, question_id, evaluation_status)
  VALUES ('cc000000-0000-0000-0000-000000000008','{FIX["a"]}','{FIX["q"]}','Correct');
INSERT INTO public.security_events (id, attempt_id, exam_id, question_id, student_id)
  VALUES ('dd000000-0000-0000-0000-000000000008','{FIX["a"]}','{FIX["ex"]}','{FIX["q"]}','{FIX["s"]}');
"""

TABLES = ["public.profiles", "public.exams", "public.questions",
          "public.exam_questions", "public.programming_test_cases",
          "public.exam_assignments", "public.exam_attempts", "public.answers",
          "public.security_events", "auth.users"]

# line numbers (1-based, in the ORIGINAL file) that each variant flips
GATE_LINES = [110, 121]                 # classification COUNTs
DEL_LINES = [329, 337]                  # profile DELETE predicates
TITLE_LINES = [134, 148]                # exam / question title gates


def flip(lines):
    raw = SRC.read_text(encoding="utf-8").splitlines(keepends=True)
    out, changed = [], 0
    for i, line in enumerate(raw, start=1):
        if i in lines and re.search(r"(?<!I)\bLIKE\b", line):
            line = re.sub(r"(?<!I)\bLIKE\b", "ILIKE", line)
            changed += 1
        out.append(line)
    return "".join(out), changed


def build(lines):
    raw, _ = flip(lines)
    # replace the five manifest placeholders with the replica's fixture ids
    for placeholder, key in (("00000000-0000-0000-0000-000000000001", "t"),
                             ("00000000-0000-0000-0000-000000000002", "s"),
                             ("00000000-0000-0000-0000-000000000003", "ex"),
                             ("00000000-0000-0000-0000-000000000004", "q"),
                             ("00000000-0000-0000-0000-000000000005", "a")):
        raw = raw.replace(f"'{placeholder}'::uuid", f"'{FIX[key]}'::uuid")
    return raw


def counts(conn):
    out = {}
    with conn.cursor() as cur:
        for t in TABLES:
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
    return out


def run(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute("RESET quizeazz.authoritative_context")
            cur.execute(sql)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        return str(exc).split("\n")[0]
    conn.commit()
    return None


def main():
    variants = [
        ("A  original (all case-sensitive)", []),
        ("B  only the 2 classification gates", GATE_LINES),
        ("C  all 4 profile checks", GATE_LINES + DEL_LINES),
        ("D  all 6 checks", GATE_LINES + DEL_LINES + TITLE_LINES),
    ]
    conn = psycopg.connect(DSN)
    for name, lines in variants:
        sql = build(lines)
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("DROP SCHEMA IF EXISTS auth CASCADE")
            cur.execute(SCHEMA)
            cur.execute(SEED)
        conn.commit()
        before = counts(conn)
        err = run(conn, sql)
        after = counts(conn)
        status = "OK (cleaned)" if err is None else f"ABORT: {err[:72]}"
        print(f"\n=== variant {name} ===")
        print(f"  result  : {status}")
        print(f"  mutated : {before != after}")
        if err:
            print(f"  profiles left: {after['public.profiles']}   "
                  f"exams left: {after['public.exams']}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())