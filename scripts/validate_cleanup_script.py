"""
Validate deploy/ZZSMOKE_CLEANUP.sql against a DISPOSABLE PostgreSQL that
replicates the live FK topology exactly (same parents, same ON DELETE rules).

Runs three scenarios:
  POSITIVE  - a well-formed ZZSMOKE fixture is fully removed, unrelated data untouched
  NEGATIVE1 - a non-ZZSMOKE exam id is supplied -> must ABORT, delete nothing
  NEGATIVE2 - a question used by a second paper is supplied -> must ABORT

No production connection. Disposable container only.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get("ZZSMOKE_VALIDATION_DSN", "")
SQL_FILE = ROOT / "deploy" / "ZZSMOKE_CLEANUP.sql"

# Live topology, measured in production:
#   profiles -> auth.users CASCADE
#   answers  -> exam_attempts CASCADE ; -> questions NO ACTION
#   security_events -> exam_attempts CASCADE ; -> exams/questions/profiles NO ACTION
#   exam_assignments -> exams CASCADE ; -> profiles CASCADE
#   exam_questions -> exams CASCADE ; -> questions NO ACTION
#   programming_test_cases -> questions CASCADE
#   exam_attempts -> exams NO ACTION ; -> profiles NO ACTION
#   exams -> profiles NO ACTION ; questions -> profiles NO ACTION
SCHEMA = """
CREATE SCHEMA public;
CREATE SCHEMA auth;
CREATE TABLE auth.users (id uuid PRIMARY KEY);
CREATE TABLE auth.scim_users (user_id uuid REFERENCES auth.users(id));

CREATE TABLE public.profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  full_name text NOT NULL, role text NOT NULL DEFAULT 'student');

CREATE TABLE public.exams (
  id uuid PRIMARY KEY, title text NOT NULL,
  created_by uuid REFERENCES public.profiles(id));

CREATE TABLE public.questions (
  id uuid PRIMARY KEY, title text NOT NULL,
  created_by uuid REFERENCES public.profiles(id));

CREATE TABLE public.programming_test_cases (
  id uuid PRIMARY KEY, question_id uuid NOT NULL
    REFERENCES public.questions(id) ON DELETE CASCADE, expected_stdout text);

CREATE TABLE public.exam_questions (
  id uuid PRIMARY KEY, exam_id uuid NOT NULL
    REFERENCES public.exams(id) ON DELETE CASCADE,
  question_id uuid NOT NULL REFERENCES public.questions(id));

CREATE TABLE public.exam_assignments (
  exam_id uuid NOT NULL REFERENCES public.exams(id) ON DELETE CASCADE,
  student_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  PRIMARY KEY (exam_id, student_id));

CREATE TABLE public.exam_attempts (
  id uuid PRIMARY KEY, exam_id uuid NOT NULL REFERENCES public.exams(id),
  student_id uuid NOT NULL REFERENCES public.profiles(id),
  status text NOT NULL DEFAULT 'in_progress');

CREATE TABLE public.answers (
  id uuid PRIMARY KEY, attempt_id uuid NOT NULL
    REFERENCES public.exam_attempts(id) ON DELETE CASCADE,
  question_id uuid NOT NULL REFERENCES public.questions(id),
  evaluation_status text);

CREATE TABLE public.security_events (
  id uuid PRIMARY KEY,
  attempt_id uuid REFERENCES public.exam_attempts(id) ON DELETE CASCADE,
  exam_id uuid REFERENCES public.exams(id),
  question_id uuid REFERENCES public.questions(id),
  student_id uuid REFERENCES public.profiles(id));

-- Faithful copies of the production protection triggers (verified against
-- pg_proc.prosrc), so the replica BLOCKS deletes exactly as production does.
CREATE FUNCTION public.quizeazz_ctx() RETURNS boolean LANGUAGE sql AS $$
  SELECT COALESCE(current_setting('quizeazz.authoritative_context', true), 'false') = 'true'; $$;

CREATE FUNCTION public.protect_attempt_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_user_role TEXT; v_is_trusted BOOLEAN;
BEGIN
  v_is_trusted := public.quizeazz_ctx();
  SELECT role INTO v_user_role FROM public.profiles WHERE id = auth.uid();
  IF TG_OP = 'DELETE' THEN
    IF NOT v_is_trusted THEN
      IF v_user_role NOT IN ('teacher','admin') OR v_user_role IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Candidates cannot delete examination attempts.';
      END IF;
    END IF;
    RETURN OLD;
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER trg_protect_attempt_mutation BEFORE INSERT OR DELETE OR UPDATE
  ON public.exam_attempts FOR EACH ROW EXECUTE FUNCTION protect_attempt_mutation();

CREATE FUNCTION public.protect_exam_composition_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF public.quizeazz_ctx() THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'Unauthorized: exam assignments and exam papers are maintained exclusively through the faculty RPCs.';
END; $$;
CREATE TRIGGER trg_protect_exam_assignments BEFORE INSERT OR DELETE OR UPDATE
  ON public.exam_assignments FOR EACH ROW EXECUTE FUNCTION protect_exam_composition_mutation();
CREATE TRIGGER trg_protect_exam_questions BEFORE INSERT OR DELETE OR UPDATE
  ON public.exam_questions FOR EACH ROW EXECUTE FUNCTION protect_exam_composition_mutation();

CREATE FUNCTION public.check_answer_write_eligibility() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_user_role TEXT; v_is_trusted BOOLEAN;
BEGIN
  v_is_trusted := public.quizeazz_ctx();
  SELECT role INTO v_user_role FROM public.profiles WHERE id = auth.uid();
  IF TG_OP = 'DELETE' THEN
    IF NOT v_is_trusted THEN
      IF v_user_role = 'student' OR v_user_role IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Candidates cannot delete examination answers.';
      END IF;
    END IF;
    RETURN OLD;
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER trg_check_answer_eligibility BEFORE INSERT OR DELETE OR UPDATE
  ON public.answers FOR EACH ROW EXECUTE FUNCTION check_answer_write_eligibility();

-- auth.uid() is NULL for a raw SQL-editor session, matching production.
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS $$ SELECT NULL::uuid; $$;
"""

# Fixture + unrelated data. The ZZSMOKE ids are fixed so the manifest in the
# cleanup script (which the driver rewrites) matches.
T = "11111111-1111-1111-1111-00000000000"  # last group is 11 chars; +1 = 12
SEED = f"""
-- Replica-only: the protection triggers require the authoritative context, so
-- seeding needs it too. This mirrors what the real RPCs do.
SELECT set_config('quizeazz.authoritative_context', 'true', false);
INSERT INTO auth.users VALUES
  ('{T}1'), ('{T}2'), ('{T}3'), ('{T}4'), ('{T}5');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{T}1','ZZSMOKE Teacher','teacher'),
  ('{T}2','ZZSMOKE Student','student'),
  ('{T}3','Real Student','student'),
  ('{T}4','Real Teacher','teacher'),
  ('{T}5','Another Real','student');
INSERT INTO public.exams (id, title, created_by) VALUES
  ('{T}6','ZZSMOKE Exam','{T}1'),
  ('{T}7','Real Exam','{T}4');
INSERT INTO public.questions (id, title, created_by) VALUES
  ('{T}8','ZZSMOKE Question','{T}1'),
  ('{T}9','Real Question','{T}4');
INSERT INTO public.programming_test_cases (id, question_id, expected_stdout) VALUES
  ('aa000000-0000-0000-0000-000000000001','{T}8','42');
INSERT INTO public.exam_questions (id, exam_id, question_id) VALUES
  ('bb000000-0000-0000-0000-000000000001','{T}6','{T}8'),
  ('bb000000-0000-0000-0000-000000000002','{T}7','{T}9');
INSERT INTO public.exam_assignments (exam_id, student_id) VALUES
  ('{T}6','{T}2');
INSERT INTO public.exam_attempts (id, exam_id, student_id, status) VALUES
  ('{T}a','{T}6','{T}2','evaluated'),
  ('{T}b','{T}7','{T}3','evaluated');
INSERT INTO public.answers (id, attempt_id, question_id, evaluation_status) VALUES
  ('cc000000-0000-0000-0000-000000000001','{T}a','{T}8','Correct'),
  ('cc000000-0000-0000-0000-000000000002','{T}b','{T}9','Correct');
INSERT INTO public.security_events (id, attempt_id, exam_id, question_id, student_id) VALUES
  ('dd000000-0000-0000-0000-000000000001','{T}a','{T}6','{T}8','{T}2');
"""



TABLES = ["auth.users", "public.profiles", "public.exams", "public.questions",
          "public.programming_test_cases", "public.exam_questions",
          "public.exam_assignments", "public.exam_attempts",
          "public.answers", "public.security_events"]


def counts(conn):
    out = {}
    with conn.cursor() as cur:
        for t in TABLES:
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
    return out


def manifest(**over):
    ids = {"teacher": f"{T}1", "student": f"{T}2", "exam": f"{T}6",
           "question": f"{T}8", "attempt": f"{T}a"}
    ids.update(over)
    return ids


def build_sql(ids):
    raw = SQL_FILE.read_text(encoding="utf-8")
    raw = raw.replace("'00000000-0000-0000-0000-000000000001'::uuid", f"'{ids['teacher']}'::uuid")
    raw = raw.replace("'00000000-0000-0000-0000-000000000002'::uuid", f"'{ids['student']}'::uuid")
    raw = raw.replace("'00000000-0000-0000-0000-000000000003'::uuid", f"'{ids['exam']}'::uuid")
    raw = raw.replace("'00000000-0000-0000-0000-000000000004'::uuid", f"'{ids['question']}'::uuid")
    raw = raw.replace("'00000000-0000-0000-0000-000000000005'::uuid", f"'{ids['attempt']}'::uuid")
    return raw


def reset(conn):
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE; DROP SCHEMA IF EXISTS auth CASCADE;")
        cur.execute(SCHEMA)
        cur.execute(SEED)


def run_case(conn, ids, expect_abort, label):
    err = None
    try:
        with conn.cursor() as cur:
            # Simulate a FRESH SQL Editor session: no inherited context, exactly
            # as the real cleanup would run.
            cur.execute("SELECT set_config('quizeazz.authoritative_context','false',false)")
            cur.execute(build_sql(ids))
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        err = str(exc)
    else:
        conn.commit()
    aborted = err is not None
    ok = aborted == expect_abort
    print(f"  [{'x' if ok else ' '}] {label}")
    print(f"        aborted={aborted} (expected {expect_abort})")
    if err:
        print(f"        reason: {err.splitlines()[0][:100]}")
    return ok, counts(conn)


def main():
    if not DSN:
        print("ZZSMOKE_VALIDATION_DSN not set - skipping")
        return 2

    conn = psycopg.connect(DSN, autocommit=True)
    passed, failed = [], []

    print("=== POSITIVE: well-formed fixture ===")
    reset(conn)
    before = counts(conn)
    ok, after = run_case(conn, manifest(), False, "fixture fully removed")
    passed.append(ok)
    if not ok:
        failed.append("positive case")
    # unrelated data must survive untouched
    survivors = {t: after[t] for t in TABLES}
    print(f"        before : {before}")
    print(f"        after  : {after}")
    fixture_gone = (after["public.exam_attempts"] == before["public.exam_attempts"] - 1
                    and after["public.answers"] == before["public.answers"] - 1
                    and after["public.exams"] == before["public.exams"] - 1
                    and after["public.questions"] == before["public.questions"] - 1
                    and after["public.profiles"] == before["public.profiles"] - 2
                    and after["auth.users"] == before["auth.users"] - 2
                    and after["public.programming_test_cases"] == 0
                    and after["public.exam_questions"] == before["public.exam_questions"] - 1
                    and after["public.exam_assignments"] == 0
                    and after["public.security_events"] == 0)
    print(f"  [{'x' if fixture_gone else ' '}] unrelated rows preserved (only fixture removed)")
    passed.append(fixture_gone)
    if not fixture_gone:
        failed.append("unrelated data preserved")

    print("\n=== NEGATIVE 1: a non-ZZSMOKE exam id ===")
    reset(conn)
    before = counts(conn)
    ok, after = run_case(conn, manifest(exam=f"{T}7"), True,
                         "aborts when the exam is real, not ZZSMOKE")
    unchanged = after == before
    print(f"  [{'x' if unchanged else ' '}] nothing deleted on abort")
    passed += [ok, unchanged]
    if not ok:
        failed.append("neg1 abort")
    if not unchanged:
        failed.append("neg1 no-mutation")

    print("\n=== NEGATIVE 2: question shared with a second paper ===")
    reset(conn)
    before = counts(conn)
    ok, after = run_case(conn, manifest(question=f"{T}9"), True,
                         "aborts when the question is used by another paper")
    unchanged = after == before
    print(f"  [{'x' if unchanged else ' '}] nothing deleted on abort")
    passed += [ok, unchanged]
    if not ok:
        failed.append("neg2 abort")
    if not unchanged:
        failed.append("neg2 no-mutation")

    print("\n=== NEGATIVE 3: attempt belonging to another student ===")
    reset(conn)
    before = counts(conn)
    ok, after = run_case(conn, manifest(attempt=f"{T}b"), True,
                         "aborts when the attempt is not the student's")
    unchanged = after == before
    print(f"  [{'x' if unchanged else ' '}] nothing deleted on abort")
    passed += [ok, unchanged]
    if not ok:
        failed.append("neg3 abort")
    if not unchanged:
        failed.append("neg3 no-mutation")

    conn.close()
    print("\n" + "=" * 72)
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  FAIL {f}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())