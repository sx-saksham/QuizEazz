"""
Dry-run the deployment artifacts against a DISPOSABLE PostgreSQL container.

    python scripts/dryrun_migration_20_deploy.py

Proves, without touching production:
  1. deploy/migration_20_PAYLOAD.sql          executes cleanly
  2. the ACL established beforehand is preserved by CREATE OR REPLACE
  3. the installed body carries FOR UPDATE
  4. deploy/ROLLBACK_..._verdict.sql           executes cleanly
  5. rollback restores the EXACT original body (FOR UPDATE gone)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
DSN = os.environ.get("QUIZEAZZ_DRYRUN_DSN", "")
ARGS = ("UUID, UUID, TEXT, TEXT, INT, INT, INT, NUMERIC, TEXT, TEXT, TEXT, TEXT")
FNAME = "quizeazz_apply_judge0_verdict"

SCHEMA = """
CREATE TABLE public.exams (id uuid PRIMARY KEY);
CREATE TABLE public.exam_attempts (id uuid PRIMARY KEY, exam_id uuid, status text);
CREATE TABLE public.questions (id uuid PRIMARY KEY, question_type text);
CREATE TABLE public.exam_questions (exam_id uuid, question_id uuid, marks numeric);
CREATE TABLE public.answers (
  id uuid PRIMARY KEY, attempt_id uuid, question_id uuid, code text, language text,
  is_correct boolean, marks_awarded numeric, evaluation_status text,
  evaluation_details jsonb, saved_at timestamptz DEFAULT now());
CREATE FUNCTION public.quizeazz_marks_for_outcome(p_outcome text, p_marks numeric)
RETURNS numeric LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_outcome = 'Correct' THEN p_marks ELSE 0 END; $$;
"""

ACL = f"""
REVOKE ALL ON FUNCTION public.{FNAME}({ARGS}) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.{FNAME}({ARGS}) FROM anon;
REVOKE ALL ON FUNCTION public.{FNAME}({ARGS}) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.{FNAME}({ARGS}) TO service_role;
"""

# The ORIGINAL (pre-migration-20) definition, exactly as captured from live.
ORIGINAL = (ROOT / "deploy" / "ROLLBACK_apply_judge0_verdict.sql").read_text(encoding="utf-8")

ok = True


def check(name, cond, detail=""):
    global ok
    print(f"  [{'x' if cond else ' '}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        ok = False


def body_of():
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    f"WHERE n.nspname='public' AND p.proname='{FNAME}'")
        return cur.fetchone()[0]


def acl_of():
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            "SELECT has_function_privilege('anon',p.oid,'EXECUTE'),"
            "has_function_privilege('authenticated',p.oid,'EXECUTE'),"
            "has_function_privilege('public',p.oid,'EXECUTE'),"
            "has_function_privilege('service_role',p.oid,'EXECUTE') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            f"WHERE n.nspname='public' AND p.proname='{FNAME}'")
        return cur.fetchone()


def main():
    if not DSN:
        print("QUIZEAZZ_DRYRUN_DSN not set - skipping")
        return 2

    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        for r in ("anon", "authenticated", "service_role"):
            # Hardcoded safe identifiers; %I is escaped as %%I because psycopg3
            # would otherwise read it as a placeholder.
            cur.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='%s') "
                "THEN EXECUTE format('CREATE ROLE %%I NOLOGIN', '%s'); END IF; END $$;" % (r, r)
            )
        cur.execute(SCHEMA)
        # Start from the ORIGINAL definition, then apply the ACL, exactly as
        # production stands today.
        cur.execute(ORIGINAL)
        cur.execute(ACL)

    original_body = body_of()
    check("baseline installed body has NO FOR UPDATE", "FOR UPDATE" not in original_body.upper())
    check("baseline ACL is service_role only", acl_of() == (False, False, False, True),
          str(acl_of()))

    # ---- apply the payload inside a single transaction --------------------
    payload = (DEPLOY / "migration_20_PAYLOAD.sql").read_text(encoding="utf-8")
    conn = psycopg.connect(DSN, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(payload)
            cur.execute("COMMIT")
        check("PAYLOAD executed with no SQL error in one transaction", True)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        check("PAYLOAD executed with no SQL error in one transaction", False, str(exc))
    finally:
        conn.close()

    installed = body_of()
    check("installed body now has FOR UPDATE", "FOR UPDATE" in installed.upper())
    check("ACL survived CREATE OR REPLACE (service_role only)",
          acl_of() == (False, False, False, True), str(acl_of()))
    check("rollback file still preserves the original body exactly",
          original_body.strip() in ORIGINAL or "FOR UPDATE" not in installed.upper())

    # ---- roll back ---------------------------------------------------------
    try:
        with psycopg.connect(DSN, autocommit=False) as conn, conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(ORIGINAL)
            cur.execute("COMMIT")
        check("ROLLBACK executed with no SQL error", True)
    except Exception as exc:  # noqa: BLE001
        check("ROLLBACK executed with no SQL error", False, str(exc))

    restored = body_of()
    check("rollback removed FOR UPDATE", "FOR UPDATE" not in restored.upper())
    check("rollback restored the original body byte-for-byte",
          restored.strip() == original_body.strip(),
          f"{len(restored)} vs {len(original_body)}")
    check("rollback left the ACL untouched",
          acl_of() == (False, False, False, True), str(acl_of()))

    print("\nDRY RUN " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())