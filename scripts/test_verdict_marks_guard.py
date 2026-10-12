"""
DIFFERENTIAL experiment on the `v_marks_total IS NULL` guard in
quizeazz_apply_judge0_verdict -- runs against a DISPOSABLE local PostgreSQL only.

    python scripts/test_verdict_marks_guard.py

THE ORIGINAL PREMISE, AND WHAT ACTUALLY HAPPENED
    This suite was written around a static reading of
    supabase/migration_20_verdict_row_lock.sql. The premise was that

        v_marks_total INT := 0;
        ...
        IF v_marks_total IS NULL THEN RAISE EXCEPTION
          'Answer % is not a coding answer of attempt %.' ...

    made the "answer is a coding answer of this attempt" check unreachable: the
    `:= 0` initialiser would hold the variable at zero, and PL/pgSQL would
    leave a SELECT..INTO target UNCHANGED when the query matches no rows.

    THAT PREMISE DID NOT REPRODUCE. In the observed disposable run (run label
    `marksguard-20261012T134827Z`, exit code 1) the CURRENT body exactly as
    written in migration 20 REFUSED all three invalid cases, and both variants
    accepted the three legitimate ones with marks 10 / 0 / 0. The static
    reasoning was wrong: PL/pgSQL assigns NULL to a SELECT..INTO target when
    the query matches no rows, so the `IS NULL` guard does fire.

    The premise is WITHDRAWN, not replaced. See
    deploy/MIGRATION_20_REHEARSAL_RECORD.md. This file is retained as the
    executable record of that negative result, not as evidence of a defect.

WHY THIS TEST IS DIFFERENTIAL
    It installs the real body twice and drives the identical cases against each:
      * CURRENT  -- the body exactly as it exists in migration 20 today.
      * CORRECTED -- the same body with two mechanical edits: the `:= 0`
        initialiser removed and the NULL test replaced by PL/pgSQL's FOUND.

    The two variants share identical text except for those two edits, so any
    behavioural difference is attributable to the guard alone.

WHY IT STILL EXITS NON-ZERO
    The negative control asserts that the CURRENT body fails to refuse the
    invalid cases, so it fails whenever the original premise does not
    reproduce. With the premise withdrawn that is the expected outcome: a
    non-zero exit from this suite is NOT a defect finding and NOT a pass. Read
    it as "the premise did not reproduce".

WHAT IT DOES NOT DO
    It does not fix anything. It does not edit migrations or deploy artefacts.
    It asserts only against a throwaway database it creates and drops.

WHAT IT DOES NOT ESTABLISH
    It does not establish that the deployed function is correct. It exercised
    SQL text against a minimal stand-in schema, on one disposable local
    PostgreSQL, on one occasion. It says nothing about production tables, RLS
    policies, data volumes, the live deployed function, or whether migration 20
    has ever been applied to production.

SAFETY
    Refuses any host outside a loopback allowlist, and additionally requires an
    exact disposable-database acknowledgement string that is never remembered.
    Works inside a throwaway database it creates and drops. Never touches the
    linked Supabase project and never writes to production.

WHY IT SKIPS BY DEFAULT
    With no disposable local PostgreSQL configured, this suite reports SKIP and
    exits NON-ZERO. A skip is a configuration problem, not a pass.

    Set QUIZEAZZ_MARKS_GUARD_DSN and QUIZEAZZ_DISPOSABLE_DSN_ACK to e.g.
        postgresql://postgres:PASSWORD@127.0.0.1:5432/postgres
        yes-this-is-a-disposable-test-database
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SQL = ROOT / "supabase"

MARKER = "CREATE OR REPLACE FUNCTION public.quizeazz_apply_judge0_verdict("
LOCAL_HOSTS = {"127.0.0.1", "::1"}

DSN_ENV = "QUIZEAZZ_MARKS_GUARD_DSN"
ACK_ENV = "QUIZEAZZ_DISPOSABLE_DSN_ACK"
ACK_VALUE = "yes-this-is-a-disposable-test-database"

SOURCE_MIGRATION = "migration_20_verdict_row_lock.sql"

# The two anchors the original premise relied on, and their corrected forms.
DECL_BROKEN = "v_marks_total INT := 0;"
DECL_FIXED = "v_marks_total INT;"
GUARD_BROKEN = "IF v_marks_total IS NULL THEN"
GUARD_FIXED = "IF NOT FOUND THEN"

RAISE_FRAGMENT = "is not a coding answer of attempt"

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'x' if cond else ' '}] {name}" + (f"\n        {detail}" if detail else ""))
    return bool(cond)


# ============================================================== SCHEMA ========
SCHEMA_SQL = """
CREATE TABLE public.exams (id uuid PRIMARY KEY);
CREATE TABLE public.exam_attempts (
  id uuid PRIMARY KEY, exam_id uuid, status text, submitted_at timestamptz);
CREATE TABLE public.questions (id uuid PRIMARY KEY, question_type text);
CREATE TABLE public.exam_questions (
  exam_id uuid, question_id uuid, marks numeric);
CREATE TABLE public.answers (
  id uuid PRIMARY KEY, attempt_id uuid, question_id uuid,
  code text, language text, is_correct boolean, marks_awarded numeric,
  evaluation_status text, evaluation_details jsonb,
  saved_at timestamptz DEFAULT now());

-- Real marks semantics: only 'Correct' earns anything.
CREATE FUNCTION public.quizeazz_marks_for_outcome(p_outcome text, p_marks numeric)
RETURNS numeric LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_outcome = 'Correct' THEN p_marks ELSE 0 END;
$$;

-- Faithful stand-in for the production BEFORE-UPDATE trigger: a caller may never
-- set evaluation fields directly, and the verdict writer must raise the
-- authoritative flag itself. Migration 20 does exactly that before its UPDATE.
CREATE FUNCTION public.check_answer_write_eligibility() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_is_trusted BOOLEAN;
BEGIN
  v_is_trusted := (current_setting('quizeazz.authoritative_context', true) = 'true');
  IF NOT v_is_trusted THEN
    IF TG_OP = 'INSERT' THEN
      IF NEW.is_correct IS NOT NULL OR NEW.marks_awarded IS NOT NULL
         OR NEW.evaluation_status IS NOT NULL THEN
        RAISE EXCEPTION 'Unauthorized: Candidates cannot supply evaluation marks or status.';
      END IF;
    ELSE
      IF NEW.is_correct IS DISTINCT FROM OLD.is_correct
         OR NEW.marks_awarded IS DISTINCT FROM OLD.marks_awarded
         OR NEW.evaluation_status IS DISTINCT FROM OLD.evaluation_status THEN
        RAISE EXCEPTION 'Unauthorized: Candidate clients cannot modify evaluation outcomes.';
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_check_answer_eligibility
  BEFORE INSERT OR UPDATE ON public.answers
  FOR EACH ROW EXECUTE FUNCTION public.check_answer_write_eligibility();
"""

# ---------------------------------------------------------------- fixtures ---
# Deliberate layout. Each case isolates ONE reason the marks lookup can miss:
#   Q_NOEQ    coding, but NO exam_questions row at all
#   Q_MCQ     has an exam_questions row, so ONLY question_type can exclude it
#   Q_FOREIGN coding and fully configured, but its answer hangs off attempt B
#   Q_OK      coding, marks 10
#   Q_NULLM   coding, exam_questions row present but marks IS NULL
E = "11111111-1111-1111-1111-111111111111"
A = "22222222-2222-2222-2222-222222222222"
B = "33333333-3333-3333-3333-333333333333"
Q_NOEQ = "44444444-4444-4444-4444-444444444441"
Q_MCQ = "44444444-4444-4444-4444-444444444442"
Q_FOREIGN = "44444444-4444-4444-4444-444444444443"
Q_OK = "44444444-4444-4444-4444-444444444444"
Q_NULLM = "44444444-4444-4444-4444-444444444445"
AN_NOEQ = "55555555-5555-5555-5555-000000000001"
AN_MCQ = "55555555-5555-5555-5555-000000000002"
AN_FOREIGN = "55555555-5555-5555-5555-000000000003"
AN_OK = "55555555-5555-5555-5555-000000000004"
AN_NULLM = "55555555-5555-5555-5555-000000000005"

# UNRESOLVED: trigger versus fixture seeding. The stand-in trigger above raises
# when an INSERT supplies a non-NULL is_correct, marks_awarded, or
# evaluation_status. Every answer row below supplies
# evaluation_status = 'Pending Evaluation', so on static reading the seed
# INSERT should have been REJECTED. In the observed run it was not, and the
# suite proceeded. quizeazz.authoritative_context is read by the trigger but
# never SET anywhere in this file, so this source cannot explain the seed
# succeeding. THE CAUSE IS UNKNOWN AND IS NOT GUESSED AT HERE. It bears on
# fixture fidelity, not on migration 20, and should be resolved before this
# suite is relied on for anything beyond what it actually measured.
SEED_SQL = f"""
INSERT INTO public.exams VALUES ('{E}');
INSERT INTO public.exam_attempts VALUES ('{A}', '{E}', 'evaluating', now());
INSERT INTO public.exam_attempts VALUES ('{B}', '{E}', 'evaluating', now());
INSERT INTO public.questions VALUES ('{Q_NOEQ}', 'coding');
INSERT INTO public.questions VALUES ('{Q_MCQ}', 'mcq');
INSERT INTO public.questions VALUES ('{Q_FOREIGN}', 'coding');
INSERT INTO public.questions VALUES ('{Q_OK}', 'coding');
INSERT INTO public.questions VALUES ('{Q_NULLM}', 'coding');
-- NOTE: no exam_questions row for {Q_NOEQ}. That absence is case 1.
INSERT INTO public.exam_questions VALUES ('{E}', '{Q_MCQ}', 10);
INSERT INTO public.exam_questions VALUES ('{E}', '{Q_FOREIGN}', 10);
INSERT INTO public.exam_questions VALUES ('{E}', '{Q_OK}', 10);
INSERT INTO public.exam_questions VALUES ('{E}', '{Q_NULLM}', NULL);
INSERT INTO public.answers (id, attempt_id, question_id, code, language, evaluation_status)
  VALUES ('{AN_NOEQ}',    '{A}', '{Q_NOEQ}',    'int main();', 'cpp', 'Pending Evaluation'),
         ('{AN_MCQ}',     '{A}', '{Q_MCQ}',     'int main();', 'cpp', 'Pending Evaluation'),
         ('{AN_FOREIGN}', '{B}', '{Q_FOREIGN}', 'int main();', 'cpp', 'Pending Evaluation'),
         ('{AN_OK}',      '{A}', '{Q_OK}',      'int main();', 'cpp', 'Pending Evaluation'),
         ('{AN_NULLM}',   '{A}', '{Q_NULLM}',   'int main();', 'cpp', 'Pending Evaluation');
"""

CALL_SQL = """
SELECT public.quizeazz_apply_judge0_verdict(
  %s::uuid, %s::uuid, NULL, %s, 3, 0, 1000, 0.01, 'out', '', '', 'marks-guard');
"""


# ============================================================ EXIT HANDLERS ==
def skip(reason):
    """Report that the rehearsal did NOT run, and fail loudly.

    A skip is a CONFIGURATION problem, not a pass: the guard question is
    unobserved until this suite actually executes. Returning 0 would let a skip
    as success by any CI reading exit codes, silently green-lighting exactly
    the regression this suite exists to catch.
    """
    print("=" * 74)
    print("QuizEazz  ·  verdict marks-guard rehearsal (differential probe)")
    print("=" * 74)
    print(f"  SKIP  {reason}")
    print()
    print("  RESULT: NOT EXECUTED. Nothing was run against any database.")
    print("  The v_marks_total IS NULL guard was NOT re-observed here.")
    print("  The last observed disposable run found it firing on all three")
    print("  invalid cases - a stand-in-schema observation, not a claim about")
    print("  production or the live deployed function.")
    print("  This suite exits NON-ZERO on purpose so a skipped rehearsal can")
    print("  never be mistaken for a passing one.")
    print("=" * 74)
    return 2


def refuse(reason):
    print("=" * 74)
    print("QuizEazz  ·  verdict marks-guard rehearsal (differential probe)")
    print("=" * 74)
    print(f"  REFUSE  {reason}")
    print("=" * 74)
    return 2


# ============================================================== VARIANTS =====
def current_body():
    """The function body exactly as it exists in the migration today."""
    text = (SQL / SOURCE_MIGRATION).read_text(encoding="utf-8")
    i = text.index(MARKER)
    return text[i : text.index("$$;", i) + 3]


def corrected_body(current):
    """
    The SAME body with two mechanical edits and nothing else.

    1. drop the `:= 0` initialiser so the unassigned case can stay NULL
    2. replace the NULL test with PL/pgSQL's FOUND, which is true only when the
       SELECT..INTO actually returned at least one row

    Refuses to proceed if either anchor is missing or ambiguous, so the two
    variants can never silently collapse into the same text.
    """
    if current.count(DECL_BROKEN) != 1:
        raise SystemExit(
            f"REFUSING to build a corrected body: expected exactly one "
            f"{DECL_BROKEN!r}, found {current.count(DECL_BROKEN)}. "
            "The source shape changed; re-derive this transformation.")
    if current.count(GUARD_BROKEN) != 1:
        raise SystemExit(
            f"REFUSING to build a corrected body: expected exactly one "
            f"{GUARD_BROKEN!r}, found {current.count(GUARD_BROKEN)}. "
            "The source shape changed; re-derive this transformation.")
    out = current.replace(DECL_BROKEN, DECL_FIXED).replace(GUARD_BROKEN, GUARD_FIXED)
    if out == current:
        raise SystemExit("REFUSING: the corrected body is identical to the current body")
    return out


# ================================================================= PROBE =====
def probe(conn, attempt_id, answer_id, outcome):
    """
    Invoke the real RPC inside its own transaction and report what actually
    happened: whether it refused, and if it did not, what it persisted.

    Reads the persisted row BEFORE rolling back, so the reported marks are the
    marks the function really wrote, not the seeded state.
    """
    conn.rollback()
    cur = conn.cursor()
    raised, msg, returned = False, "", None
    try:
        cur.execute(CALL_SQL, (attempt_id, answer_id, outcome))
        returned = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
        raised = True
        msg = str(exc)
    marks, status = None, None
    if not raised:
        cur.execute(
            "SELECT marks_awarded, evaluation_status FROM public.answers WHERE id = %s",
            (answer_id,))
        row = cur.fetchone()
        if row:
            marks, status = row[0], row[1]
    conn.rollback()
    return {"raised": raised, "msg": msg, "returned": returned,
            "marks": marks, "status": status}


def refused_expected(p):
    return p["raised"] and RAISE_FRAGMENT in p["msg"]


# =================================================================== CASES ====
CASES = [
    ("missing exam_questions row",
     "a coding answer with no exam_questions row must be refused", A, AN_NOEQ, "Correct"),
    ("MCQ answer",
     "an answer whose question_type is not 'coding' must be refused", A, AN_MCQ, "Correct"),
    ("answer from another attempt",
     "an answer belonging to a different attempt must be refused", A, AN_FOREIGN, "Correct"),
    ("valid Correct with 10 marks",
     "a fully configured coding answer graded Correct earns its marks", A, AN_OK, "Correct"),
    ("valid coding row with NULL marks",
     "a present exam_questions row with NULL marks earns zero, and is NOT refused",
     A, AN_NULLM, "Correct"),
    ("valid Wrong Answer",
     "a fully configured coding answer graded Wrong earns zero", A, AN_OK, "Wrong Answer"),
]

MUST_REFUSE = {"missing exam_questions row", "MCQ answer", "answer from another attempt"}
EXPECT_MARKS = {
    "valid Correct with 10 marks": (10, "Correct"),
    "valid coding row with NULL marks": (0, "Correct"),
    "valid Wrong Answer": (0, "Wrong Answer"),
}


def run_variant(psyc, dsn, dbname, body, label):
    """Create a throwaway DB, install `body`, and run every case against it."""
    admin = psyc.connect(dsn, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
            cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()

    target = dsn.rsplit("/", 1)[0] + "/" + dbname
    try:
        conn = psyc.connect(target)
        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                cur.execute(SEED_SQL)
                cur.execute(body)
            conn.commit()
            outcomes = {}
            for name, _why, attempt, answer, outcome in CASES:
                outcomes[name] = probe(conn, attempt, answer, outcome)
        finally:
            conn.close()
    finally:
        admin = psyc.connect(dsn, autocommit=True)
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        finally:
            admin.close()
    return label, outcomes


def main():
    dsn = os.environ.get(DSN_ENV, "").strip()
    ack = os.environ.get(ACK_ENV, "")
    if not dsn:
        return skip(f"{DSN_ENV} is not set. No disposable local PostgreSQL is configured.")
    host = (urlparse(dsn).hostname or "").lower()
    if host not in LOCAL_HOSTS:
        return refuse(
            f"refusing to run against host {host!r}: this test creates and drops "
            "databases and must only ever target a loopback disposable instance.")
    if ack != ACK_VALUE:
        return refuse(
            f"{ACK_ENV} is not set to the exact required value. The opt-in is "
            "deliberately required on every run and is never remembered.")

    try:
        import psycopg as psyc  # noqa: PLC0415
    except ImportError:
        return skip(f"no PostgreSQL driver installed (pip install 'psycopg[binary]') for {host}")

    try:
        current = current_body()
        corrected = corrected_body(current)
    except SystemExit as exc:
        return refuse(str(exc))

    print("=" * 74)
    print("QuizEazz  ·  verdict marks-guard rehearsal (differential probe)")
    print("=" * 74)
    print(f"  target host        : {host}  (disposable databases only)")
    print(f"  source migration   : supabase/{SOURCE_MIGRATION}")
    print(f"  cases per variant  : {len(CASES)}")

    db_base = "quizeazz_marksguard_" + os.urandom(4).hex()
    runs = []
    for suffix, body, label in (("broken", current, "CURRENT body (migration 20 as written)"),
                                ("fixed", corrected, "CORRECTED body (no initialiser + FOUND)")):
        try:
            runs.append(run_variant(psyc, dsn, f"{db_base}_{suffix}", body, label))
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR  {label}: {type(exc).__name__}: {exc}")
            return 1

    cur_out = runs[0][1]
    fix_out = runs[1][1]

    print()
    for label, outcomes in runs:
        print(f"  {label}")
        for name, _why, _a, _ans, _o in CASES:
            p = outcomes[name]
            if p["raised"]:
                shape = f"REFUSED ({p['msg'].splitlines()[0][:44]})"
            else:
                shape = f"accepted -> marks={p['marks']} status={p['status']}"
            print(f"    {name:32} {shape}")
        print()

    print("  --- negative control: the original premise must reproduce ---")
    control_ok = True
    for name in sorted(MUST_REFUSE):
        p = cur_out[name]
        reproduced = not p["raised"]
        if not check(f"CURRENT body fails to refuse: {name}", reproduced,
                     f"returned={p['returned']} marks={p['marks']} status={p['status']}"
                     if reproduced else
                     f"it raised instead: {p['msg'].splitlines()[0][:70]}"):
            control_ok = False

    print()
    print("  --- corrected body must reject the same cases ---")
    for name in sorted(MUST_REFUSE):
        p = fix_out[name]
        check(f"CORRECTED body refuses: {name}", refused_expected(p),
              p["msg"].splitlines()[0][:74] if p["raised"] else f"ACCEPTED, marks={p['marks']}")

    print()
    print("  --- both variants must agree on the legitimate cases ---")
    for name, (want_marks, want_status) in EXPECT_MARKS.items():
        for label, outcomes in runs:
            p = outcomes[name]
            check(f"{label.split()[0]} body: {name}", not p["raised"],
                  p["msg"].splitlines()[0][:74] if p["raised"] else "accepted")
            if p["raised"]:
                continue
            check(f"{label.split()[0]} body: {name} -> marks {want_marks}",
                  str(p["marks"]) == str(want_marks), f"got marks={p['marks']}")
            check(f"{label.split()[0]} body: {name} -> status {want_status}",
                  p["status"] == want_status, f"got status={p['status']}")

    failed = results.count(False)
    if not control_ok:
        print()
        print("  FAIL  the negative control did not reproduce the ORIGINAL PREMISE.")
        print("        The CURRENT body refused the invalid cases, so this suite cannot")
        print("        demonstrate the bug its premise assumed. That premise is WITHDRAWN")
        print("        - see deploy/MIGRATION_20_REHEARSAL_RECORD.md. This non-zero exit")
        print("        is the honest result; it is not a defect finding.")
        print("        Do not read the rest of the results as a fix having landed.")
    print("-" * 74)
    print("RESULT: " + (f"{failed} failure(s)" if failed else
                        "no assertion failed - the ORIGINAL PREMISE reproduced in this run; "
                        "see deploy/MIGRATION_20_REHEARSAL_RECORD.md before reading that as a defect"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())