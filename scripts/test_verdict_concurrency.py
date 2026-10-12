"""
BEHAVIOURAL concurrency test for migration 20 (P1) — runs against a DISPOSABLE
local PostgreSQL only.

    python scripts/test_verdict_concurrency.py

WHAT THIS PROVES
    quizeazz_apply_judge0_verdict documents "One verdict per answer. Never
    overwrite a stored verdict." Without `FOR UPDATE` on the guard SELECT that
    is a TOCTOU check, not a guarantee: under READ COMMITTED two concurrent
    graders can both read 'Pending Evaluation', and the second UPDATE then
    blocks, re-checks a WHERE clause that still matches (id and attempt_id are
    immutable), and OVERWRITES the committed verdict.

    This test is DIFFERENTIAL. It installs the real function body twice —
    once from migration_09 (no lock, = what production runs today) and once
    from migration_20 (FOR UPDATE) — and runs the identical concurrent workload
    against each. The migration_09 run must exhibit the double-apply; the
    migration_20 run must never do so. A test that only ran against the fixed
    body could not tell a real fix from a test that passes either way.

WHY IT SKIPS BY DEFAULT
    There is no local PostgreSQL in the current environment: no docker, no
    podman, no psql, no postgres service, and no PostgreSQL driver installed.
    So this suite reports SKIP, not PASS. Nothing about migration 20 has been
    executed by Postgres yet. Do not read a green run of the other suites as
    evidence that this file's assertions hold.

SAFETY
    Refuses to run against anything that is not a loopback host, and works
    inside a throwaway database that it creates and drops. It never touches the
    linked Supabase project, and it never writes to production.

    Set QUIZEAZZ_REHEARSAL_DSN to e.g.
        postgresql://postgres:postgres@127.0.0.1:5432/postgres
"""

import os
import secrets
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SQL = ROOT / "supabase"

MARKER = "CREATE OR REPLACE FUNCTION public.quizeazz_apply_judge0_verdict("
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
ROUNDS = 30

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

-- Faithful stand-in for the production BEFORE-UPDATE trigger
-- check_answer_write_eligibility. The parts that matter for migration 20 are:
--   * a caller may never set or change evaluation fields directly, and
--   * the verdict writer must raise the authoritative flag itself.
-- Migration 20 performs exactly this set_config before its UPDATE, so this
-- trigger is what proves the fix still satisfies the anti-tamper contract.
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

SEED_SQL = """
INSERT INTO public.exams VALUES ('11111111-1111-1111-1111-111111111111');
INSERT INTO public.exam_attempts
  VALUES ('22222222-2222-2222-2222-222222222222',
          '11111111-1111-1111-1111-111111111111', 'evaluating', now());
INSERT INTO public.questions VALUES
  ('33333333-3333-3333-3333-333333333333', 'coding');
INSERT INTO public.exam_questions VALUES
  ('11111111-1111-1111-1111-111111111111',
   '33333333-3333-3333-3333-333333333333', 10);
"""


VERSION_P = "UUID, UUID, TEXT, TEXT, INT, INT, INT, NUMERIC, TEXT, TEXT, TEXT, TEXT"


def ensure_roles(psyc, dsn, created=None):
    """
    Create the Supabase roles this function's ACL refers to.

    A stock postgres:17 image has none of them, so has_function_privilege
    would fail. Roles are cluster-scoped and exist only inside this
    disposable container.

    `created` is an optional set receiving the names of the roles THIS call
    created. A role that already existed is never recorded, and a role not in
    this set must never be dropped. The parameter is optional so the existing
    two-argument caller in burst_75_harness.leg_b keeps working.
    """
    conn = psyc.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            for role in ("anon", "authenticated", "service_role"):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                if cur.fetchone() is not None:
                    continue          # pre-existing: not ours, never ours to drop
                # role name comes from the literal tuple above, never from input
                cur.execute(f"CREATE ROLE {role} NOLOGIN")
                if created is not None:
                    created.add(role)
    finally:
        conn.close()


def drop_roles(psyc, dsn, created):
    """
    Drop ONLY roles this run created, and only once every disposable database
    is gone. A role absent from `created` is never touched. A DROP that fails
    (role still owns objects or holds grants) is reported and left in place -
    nothing is forced.
    """
    for role in sorted(created):
        try:
            conn = psyc.connect(dsn, autocommit=True)
            try:
                with conn.cursor() as cur:
                    cur.execute(f'DROP ROLE IF EXISTS "{role}"')
            finally:
                conn.close()
            print(f"    dropped role {role} (created by this run)")
        except Exception as exc:  # noqa: BLE001
            print(f"    LEFT IN PLACE role {role}: {type(exc).__name__}")


# Migration 07 established this ACL. Migration 20 adds no GRANT/REVOKE and
# relies on CREATE OR REPLACE preserving it, so the rehearsal must establish
# it FIRST for that preservation to be observable.
ACL_SQL = f"""
REVOKE ALL ON FUNCTION public.quizeazz_apply_judge0_verdict({VERSION_P}) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_apply_judge0_verdict({VERSION_P}) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_apply_judge0_verdict({VERSION_P}) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.quizeazz_apply_judge0_verdict({VERSION_P}) TO service_role;
"""


def verdict_body(fname):
    """The real CREATE OR REPLACE FUNCTION statement from a migration file."""
    text = (SQL / fname).read_text(encoding="utf-8")
    i = text.index(MARKER)
    return text[i : text.index("$$;", i) + 3]


def skip(reason):
    """Report that the rehearsal did NOT run, and fail loudly.

    This deliberately returns the same non-zero code as refuse() (2). A skip is
    a CONFIGURATION problem, not a pass: the concurrency property is unproven
    until this suite actually executes against a disposable database. Returning
    0 here let a skip be counted as success by any CI that reads exit codes,
    which would silently green-light a regression this suite exists to catch.
    """
    print("=" * 74)
    print("QuizEazz  ·  verdict concurrency rehearsal (P1)")
    print("=" * 74)
    print(f"  SKIP  {reason}")
    print()
    print("  RESULT: NOT EXECUTED. Nothing was run against any database.")
    print("  Migration 20's concurrent-verdict property remains UNVERIFIED against a")
    print("  real PostgreSQL. This suite exits NON-ZERO on purpose so a skipped")
    print("  rehearsal can never be mistaken for a passing one.")
    print("  Re-run with QUIZEAZZ_REHEARSAL_DSN pointed at a disposable LOOPBACK")
    print("  PostgreSQL instance, and 'psycopg[binary]' installed.")
    print("=" * 74)
    return 2


def refuse(reason):
    print("=" * 74)
    print("QuizEazz  ·  verdict concurrency rehearsal (P1)")
    print("=" * 74)
    print(f"  REFUSE  {reason}")
    print("=" * 74)
    return 2


def race(conn_factory, answer_id, barrier):
    """Fire two concurrent, deliberately conflicting verdicts at one answer."""
    outcomes = []

    def worker(outcome):
        conn = conn_factory()
        try:
            barrier.wait(timeout=10)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT public.quizeazz_apply_judge0_verdict(
                  '22222222-2222-2222-2222-222222222222'::uuid, %s::uuid,
                  NULL, %s, 3, 0, 1000, 0.01, 'out', '', '', 'race');
                """,
                (answer_id, outcome),
            )
            outcomes.append(("ok", cur.fetchone()[0]))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
            outcomes.append(("raised", type(exc).__name__))
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()

    # Opposing outcomes: if both ever commit, one has overwritten the other.
    threads = [
        threading.Thread(target=worker, args=("Correct",)),
        threading.Thread(target=worker, args=("Wrong Answer",)),
    ]
    for t in threads:
        t.start()
    # Only the two workers meet at the barrier. If main() also waited it would
    # be a third party on a Barrier(2), releasing the workers early and then
    # timing out itself - corrupting the measurement with a BrokenBarrierError.
    for t in threads:
        t.join(timeout=60)
    return outcomes


def run_variant(psyc, dsn, dbname, body_sql, label):
    """Install `body_sql` into a throwaway DB and run the concurrent workload."""
    created_db = False
    admin = psyc.connect(dsn, autocommit=True)
    try:
        with admin.cursor() as cur:
            # Fail closed. This rehearsal must never destroy a database it did
            # not create, so a name collision aborts instead of dropping.
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if cur.fetchone() is not None:
                raise RuntimeError(
                    f"refusing to create over an existing database {dbname!r}: "
                    "this rehearsal never drops a database it did not create")
            cur.execute(f'CREATE DATABASE "{dbname}"')
            created_db = True
    finally:
        admin.close()

    target = dsn.rsplit("/", 1)[0] + "/" + dbname
    contract = {}
    try:
        conn = psyc.connect(target, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                cur.execute(SEED_SQL)
                cur.execute(body_sql)
                # Establish the production ACL (migration 07) so that migration
                # 20's "CREATE OR REPLACE preserves the grant set" is observable.
                cur.execute(ACL_SQL)
        finally:
            conn.close()

        factory = lambda: psyc.connect(target)  # noqa: E731

        double_applies = 0
        rounds = ROUNDS
        for r in range(rounds):
            answer_id = f"44444444-4444-4444-4444-{r:012d}"
            conn = factory()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO public.answers (id, attempt_id, question_id, code,"
                        " language, evaluation_status) VALUES (%s::uuid,"
                        " '22222222-2222-2222-2222-222222222222',"
                        " '33333333-3333-3333-3333-333333333333', 'int main(){}',"
                        " 'cpp', 'Pending Evaluation')",
                        (answer_id,),
                    )
                conn.commit()
            finally:
                conn.close()

            barrier = threading.Barrier(2)
            outcomes = race(factory, answer_id, barrier)
            successes = [o for o in outcomes if o[0] == "ok" and o[1] is True]
            if len(successes) > 1:
                double_applies += 1

        # ---- inspect the INSTALLED function before dropping the database ---
        contract = inspect_contract(psyc, target)
    finally:
        # Drop the disposable database even if setup or the race raised, so a
        # failure cannot leak one. Guarded on created_db: on a collision we
        # raised BEFORE creating, and must not drop what we did not make.
        if created_db:
            admin = psyc.connect(dsn, autocommit=True)
            try:
                with admin.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
            finally:
                admin.close()

    return label, rounds, double_applies, contract


CONTRACT_SQL = """
SELECT p.prosrc,
       pg_get_function_identity_arguments(p.oid) AS identity_args,
       pg_get_function_result(p.oid)            AS result,
       p.prosecdef                              AS security_definer,
       p.proconfig                              AS config,
       has_function_privilege('anon', p.oid, 'EXECUTE')        AS anon,
       has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth,
       has_function_privilege('service_role', p.oid, 'EXECUTE')  AS svc,
       has_function_privilege('public', p.oid, 'EXECUTE')        AS public
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND p.proname = 'quizeazz_apply_judge0_verdict';
"""


def inspect_contract(psyc, target):
    """Read back the installed definition, signature and privileges."""
    conn = psyc.connect(target, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(CONTRACT_SQL)
            row = cur.fetchone()
            cols = [d.name for d in cur.description]
            info = dict(zip(cols, row))
            # Anti-tamper contract: an unprivileged write of evaluation fields
            # must still be refused after migration 20. Use a FRESH probe row
            # still at 'Pending Evaluation' - reusing a raced row is unreliable
            # because the no-op UPDATE would never fire the BEFORE trigger.
            cur.execute(
                "INSERT INTO public.answers (id, attempt_id, question_id, code,"
                " language, evaluation_status) VALUES"
                " ('55555555-5555-5555-5555-000000000000',"
                " '22222222-2222-2222-2222-222222222222',"
                " '33333333-3333-3333-3333-333333333333', 'int main(){}',"
                " 'cpp', 'Pending Evaluation')"
            )
            cur.execute("SELECT set_config('quizeazz.authoritative_context', 'false', false)")
            tampered = False
            try:
                cur.execute(
                    "UPDATE public.answers SET evaluation_status = 'Correct' "
                    "WHERE id = '55555555-5555-5555-5555-000000000000'"
                )
            except Exception:  # noqa: BLE001
                tampered = True
            info["tamper_blocked"] = tampered
    finally:
        conn.close()
    return info


def main():
    dsn = os.environ.get("QUIZEAZZ_REHEARSAL_DSN", "").strip()
    if not dsn:
        return skip(
            "QUIZEAZZ_REHEARSAL_DSN is not set. No disposable local PostgreSQL "
            "is configured in this environment."
        )

    host = (urlparse(dsn).hostname or "").lower()
    if host not in LOCAL_HOSTS:
        return refuse(
            f"refusing to run against host {host!r}: this test creates and drops "
            "databases and must only ever target a loopback disposable instance."
        )

    try:
        import psycopg as psyc  # noqa: PLC0415
    except ImportError:
        return skip(
            "no PostgreSQL driver installed (pip install 'psycopg[binary]') "
            f"for DSN {host}"
        )

    # 64 bits from the OS CSPRNG, so this name cannot collide with
    # anything; a collision now aborts the rehearsal rather than dropping.
    tag = secrets.token_hex(8)
    print("=" * 74)
    print("QuizEazz  ·  verdict concurrency rehearsal (P1)")
    print("=" * 74)
    print(f"  target host : {host}  (disposable databases only)")
    print(f"  rounds      : {ROUNDS} per variant")
    created_roles = set()
    try:
        ensure_roles(psyc, dsn, created_roles)
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR  could not create the Supabase roles: {exc}")
        drop_roles(psyc, dsn, created_roles)
        return 1
    print()

    results = []
    try:
        for fname, label in (
            ("migration_09_cpp_only_and_verdict_fix.sql", "migration 09  (NO row lock - current production)"),
            ("migration_20_verdict_row_lock.sql", "migration 20  (FOR UPDATE - the fix)"),
        ):
            try:
                results.append(
                    run_variant(psyc, dsn, f"quizeazz_rehearsal_{tag}_{fname[10:12]}", verdict_body(fname), label)
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR  {label}: {type(exc).__name__}: {exc}")
                return 1
    finally:
        # Every disposable database is gone (run_variant drops each in its own
        # finally), so these roles have no dependents left. Only roles this run
        # created are attempted; anything pre-existing is untouched.
        if created_roles:
            print("  cleaning up roles this run created:")
            drop_roles(psyc, dsn, created_roles)

    failed = 0
    for label, rounds, double_applies, contract in results:
        print(f"  {label}")
        print(f"    concurrent double-apply observed : {double_applies} / {rounds} rounds")
        print(f"    installed guard SELECT locked    : "
              f"{bool(re.search(r'FOR UPDATE', contract['prosrc'] or '', re.I))}")
        print(f"    returns                          : {contract['result']}")
        print(f"    SECURITY DEFINER                 : {contract['security_definer']}")
        print(f"    search_path                      : {contract['config']}")
        print(f"    privilege anon/auth/public/svc   : "
              f"{contract['anon']}/{contract['auth']}/{contract['public']}/{contract['svc']}")
        print(f"    anti-tamper trigger still blocks : {contract['tamper_blocked']}")
        print()

    unlocked = results[0][2]
    locked = results[1][2]
    if unlocked == 0:
        print("  FAIL  migration 09 reproduced NO double-apply.")
        print("        The race did not manifest, so this harness cannot currently")
        print("        demonstrate the bug and its fix. Do not trust the result.")
        failed += 1
    if locked != 0:
        print(f"  FAIL  migration 20 still double-applied a verdict in {locked} round(s).")
        print("        FOR UPDATE did not prevent the overwrite.")
        failed += 1

    # --- installed-function contract (requirement: definition/signature/ACL)
    fixed = results[1][3]
    if not re.search(r"FOR UPDATE", fixed["prosrc"] or "", re.I):
        print("  FAIL  installed function body does not contain FOR UPDATE")
        failed += 1
    if fixed["result"] != "boolean":
        print(f"  FAIL  return type changed: {fixed['result']}")
        failed += 1
    if not fixed["security_definer"]:
        print("  FAIL  function is no longer SECURITY DEFINER")
        failed += 1
    if (fixed["anon"] or fixed["auth"] or fixed["public"]
            or not fixed["svc"]):
        print(f"  FAIL  privileges changed: anon={fixed['anon']} "
              f"auth={fixed['auth']} public={fixed['public']} svc={fixed['svc']}")
        failed += 1
    expected_args = ("p_attempt_id uuid, p_answer_id uuid, p_submission_token text, "
                     "p_outcome text, p_status_id integer, p_exit_code integer, "
                     "p_memory integer, p_time numeric, p_stdout text, "
                     "p_stderr text, p_compile_output text, p_message text")
    if (fixed["identity_args"] or "").lower() != expected_args.lower():
        print(f"  FAIL  signature changed: {fixed['identity_args']}")
        failed += 1
    if not fixed["tamper_blocked"]:
        print("  FAIL  the anti-tamper trigger no longer blocks an unprivileged")
        print("        evaluation-field write; migration 20 would have broken it")
        failed += 1

    if not failed:
        print("  ok    migration 09 races, migration 20 does not (FOR UPDATE is effective)")
        print("  ok    at most one grader may commit a verdict for any answer")
        print("  ok    installed signature, return type, SECURITY DEFINER and ACL intact")
        print("  ok    the anti-tamper trigger still refuses unprivileged writes")

    print("-" * 74)
    print("RESULT: " + ("all behavioural concurrency assertions hold" if not failed
                       else f"{failed} behavioural failure(s)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())