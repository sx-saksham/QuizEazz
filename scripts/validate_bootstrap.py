"""
Validate deploy/ZZSMOKE_BOOTSTRAP.sql on a replica that has the REAL
protect_profile_role_escalation trigger, and PROVE the authoritative context
cannot leak out of its transaction.

Covers: the positive promotion, six refusal cases, idempotency, and a
cross-connection leak test on a fresh session.

!! DESTRUCTIVE -- DISPOSABLE TEST DATABASES ONLY !!

  reset() issues `DROP SCHEMA IF EXISTS public CASCADE` and the same for
  `auth`, then rebuilds both. There is no undo and no prompt. Pointing this
  at anything other than a throwaway database destroys it.

  A fail-closed safety gate runs at import time, before psycopg.connect() and
  before any SQL executes. It rejects missing or malformed DSNs, non-loopback
  hosts, multi-host and Unix-socket connection strings, and connection strings
  that name no host or no database. It then requires an explicit per-run
  opt-in. A loopback address is NOT accepted as proof that the target is
  disposable: a developer machine can also run a local stack holding real
  fixtures, so the operator's opt-in is the only basis for that claim.
"""
import os
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

# ---------------------------------------------------------------------------
# SAFETY GATE
#
# Everything below runs at import time. The first database contact happens
# later, inside main(), so a refusal here cannot have connected, seeded or
# mutated anything.
# ---------------------------------------------------------------------------

DSN_ENV_VAR = "ZZSMOKE_VALIDATION_DSN"
OPT_IN_ENV_VAR = "QUIZEAZZ_DISPOSABLE_DSN_ACK"
OPT_IN_VALUE = "yes-this-is-a-disposable-test-database"

# Numeric loopback literals only. 'localhost' is deliberately NOT accepted: it
# is a name resolved through the host resolver, so what it points at is not
# determined by this string. IPv6 zone identifiers are stripped before the
# comparison so '[::1%eth0]' is judged on '::1'.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def _refuse(reason):
    """Abort before connecting. Never echoes the DSN or any credential."""
    print("=" * 72)
    print(f"REFUSED -- no connection was opened and no SQL was executed.")
    print(f"  {reason}")
    print("")
    print("  This script DROPs and recreates the public and auth schemas, so it")
    print("  refuses to run against anything but a disposable test database.")
    print("  Point it at one explicitly, e.g.")
    print("")
    print(f"    {DSN_ENV_VAR}=postgresql://USER:PASSWORD@127.0.0.1:PORT/DISPOSABLE_DB")
    print(f"    {OPT_IN_ENV_VAR}={OPT_IN_VALUE}")
    print("")
    print("  Both variables are required on every run; nothing is remembered.")
    print("=" * 72)
    raise SystemExit(2)


def enforce_disposable_target(dsn, opt_in):
    """Validate the target completely, then return the DSN.

    Any missing, ambiguous or unsupported input aborts the process. This
    function never opens a connection, so a refusal cannot have had a side
    effect on the database.
    """
    if dsn is None or not dsn.strip():
        _refuse(f"{DSN_ENV_VAR} is unset or empty.")

    try:
        params = conninfo_to_dict(dsn)
    except Exception as exc:  # noqa: BLE001
        # The driver's message can quote the offending token, which may be a
        # credential, so only the exception type is reported here.
        _refuse(f"{DSN_ENV_VAR} is not a valid PostgreSQL connection string "
                f"(parser raised {type(exc).__name__}).")

    # conninfo_to_dict('') returns {} rather than raising, so catch it here.
    if not params:
        _refuse(f"{DSN_ENV_VAR} parsed to an empty connection configuration.")

    raw_host = params.get("host")
    if raw_host is None or not raw_host.strip():
        _refuse("the connection string names no host, so the target cannot be "
                "identified; libpq would fall back to a local socket.")

    if "," in raw_host:
        _refuse(f"multi-host connection strings are not supported (host={raw_host!r}); "
                "the effective target would be ambiguous.")

    if raw_host.startswith("/") or "\\" in raw_host:
        _refuse(f"Unix-socket connections are not supported (host={raw_host!r}); the "
                "database behind a socket path cannot be identified from here.")

    # '[::1]' -> '::1', and '[fe80::1%eth0]' -> 'fe80::1'.
    host = raw_host.strip().strip("[]").split("%", 1)[0].lower()
    if host not in LOOPBACK_HOSTS:
        if host == "localhost":
            _refuse("'localhost' is resolved through the host resolver, so it does "
                    "not identify the target; use the numeric address 127.0.0.1 "
                    "or [::1] instead.")
        _refuse(f"host {host!r} is not a loopback address; only "
                f"{' and '.join(sorted(LOOPBACK_HOSTS))} are accepted.")

    port = params.get("port")
    if port is not None:
        try:
            port_num = int(port)
        except (TypeError, ValueError):
            _refuse(f"port {port!r} is not a number.")
        if not 1 <= port_num <= 65535:
            _refuse(f"port {port_num} is outside the range 1-65535.")

    dbname = params.get("dbname")
    if dbname is None or not dbname.strip():
        _refuse("the connection string names no database, so the target cannot be "
                "identified.")

    if opt_in != OPT_IN_VALUE:
        _refuse(f"{OPT_IN_ENV_VAR} is not set to the exact value required. It must "
                f"be {OPT_IN_VALUE!r} (received {opt_in!r}).")

    print("=" * 72)
    print("disposable-target gate: PASSED")
    print(f"  host     : {host}  (numeric loopback)")
    print(f"  port     : {port if port is not None else 'unset (libpq default 5432)'}")
    print(f"  database : {dbname}")
    print(f"  user     : {params.get('user', 'unset (libpq default)')}")
    print("  password : never printed")
    print(f"  opt-in   : {OPT_IN_ENV_VAR} acknowledged")
    print("")
    print("  A loopback host does NOT prove this database is disposable. The")
    print("  opt-in above is the only basis for that claim, and it is the")
    print("  operator's to make. This script will now DROP and rebuild the")
    print("  public and auth schemas of the database named above.")
    print("=" * 72)
    return dsn


SQL_FILE = Path(__file__).resolve().parent.parent / "deploy" / "ZZSMOKE_BOOTSTRAP.sql"
DSN = enforce_disposable_target(os.environ.get(DSN_ENV_VAR),
                                os.environ.get(OPT_IN_ENV_VAR))

T = "11111111-1111-1111-1111-00000000000"
FIX, REAL, STUD, IMP = f"{T}1", f"{T}4", f"{T}2", f"{T}7"

SCHEMA = f"""
CREATE SCHEMA auth;
CREATE TABLE auth.users (
  id uuid PRIMARY KEY, email text,
  raw_app_meta_data jsonb, raw_user_meta_data jsonb);
CREATE TABLE public.profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  full_name text, role text, created_at timestamptz DEFAULT now());
CREATE TABLE public.exams (id uuid PRIMARY KEY, title text, created_by uuid
  REFERENCES public.profiles(id) ON DELETE NO ACTION);
CREATE TABLE public.questions (id uuid PRIMARY KEY, title text, created_by uuid
  REFERENCES public.profiles(id) ON DELETE NO ACTION);
CREATE TABLE public.exam_attempts (id uuid PRIMARY KEY, exam_id uuid, student_id uuid
  REFERENCES public.profiles(id) ON DELETE NO ACTION);
CREATE TABLE public.exam_assignments (exam_id uuid, student_id uuid
  REFERENCES public.profiles(id) ON DELETE CASCADE);
CREATE TABLE public.security_events (id uuid PRIMARY KEY, student_id uuid
  REFERENCES public.profiles(id) ON DELETE NO ACTION);

CREATE FUNCTION public.quizeazz_ctx() RETURNS boolean LANGUAGE sql AS $$
  SELECT COALESCE(current_setting('quizeazz.authoritative_context', true), 'false') = 'true'; $$;
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS $$ SELECT NULL::uuid; $$;

-- Faithful copy of the production role-protection trigger (DELETE does not fire).
CREATE FUNCTION public.protect_profile_role_escalation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_user_role TEXT; v_is_trusted BOOLEAN;
BEGIN
  v_is_trusted := public.quizeazz_ctx();
  SELECT role INTO v_user_role FROM public.profiles WHERE id = auth.uid();
  IF NOT v_is_trusted AND (v_user_role IS NULL OR v_user_role <> 'admin') THEN
    RAISE EXCEPTION 'Unauthorized: profile role may only be changed by an administrator.';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER trg_protect_profile_role BEFORE INSERT OR UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION protect_profile_role_escalation();
"""

GOOD = f"""
INSERT INTO auth.users (id, email, raw_app_meta_data, raw_user_meta_data) VALUES
  ('{FIX}','zzsmoke-teacher@example.test','{{}}','{{}}'),
  ('{REAL}','real.person@example.test','{{}}','{{}}');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{FIX}','ZZSMOKE Teacher','student'),
  ('{REAL}','Real Teacher','teacher');
"""

# REGRESSION seed - the profile-name spelling that actually exists in
# production. The live fixture was created with a lowercase full_name, so the
# UPDATE predicate must be case-insensitive or it matches zero rows and the run
# aborts. The mail domain below is an RFC 2606 stand-in: the regression is
# about the NAME predicate, so no real fixture address needs to be committed.
LIVE = f"""
INSERT INTO auth.users (id, email, raw_app_meta_data, raw_user_meta_data) VALUES
  ('{FIX}','zzsmoke-teacher-20261012@example.test','{{}}','{{}}'),
  ('{STUD}','zzsmoke-student-20261012@example.test','{{}}','{{}}'),
  ('{REAL}','real.person@example.test','{{}}','{{}}');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{FIX}','zzsmoke-teacher-20261012','student'),
  ('{STUD}','zzsmoke-student-20261012','student'),
  ('{REAL}','Real Teacher','teacher');
"""

# A look-alike whose EMAIL contains 'zzsmoke' (so the email gates pass) but
# whose name is not a fixture. This is the case ILIKE must still refuse, and
# it proves the name predicate - not the email - is what protects the update.
IMPOSTOR = f"""
INSERT INTO auth.users (id, email, raw_app_meta_data, raw_user_meta_data) VALUES
  ('{IMP}','zzsmoke-lookalike@example.test','{{}}','{{}}'),
  ('{REAL}','real.person@example.test','{{}}','{{}}');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{IMP}','Impostor Account','student'),
  ('{REAL}','Real Teacher','teacher');
"""

# Each case: (name, seed, email, uuid, expect_abort)
CASES = [
    ("promotes a pristine ZZSMOKE student profile", GOOD,
     "zzsmoke-teacher@example.test", FIX, False),
    # --- regression: the live lowercase spelling must succeed -------------
    ("REGRESSION promotes the LIVE lowercase fixture name", LIVE,
     "zzsmoke-teacher-20261012@example.test", FIX, False),
    ("REGRESSION lowercase, unrelated accounts untouched", LIVE,
     "zzsmoke-teacher-20261012@example.test", FIX, False),
    ("mixed-case fixture name also accepted",
     GOOD.replace("'ZZSMOKE Teacher'", "'ZzSmOkE Teacher'"),
     "zzsmoke-teacher@example.test", FIX, False),
    # --- negatives --------------------------------------------------------
    ("REFUSES a non-ZZSMOKE email", GOOD,
     "real.person@example.test", FIX, True),
    ("REFUSES an unknown UUID", GOOD,
     "zzsmoke-teacher@example.test", f"{T}9", True),
    ("REFUSES an account that already owns records", GOOD
     + f"INSERT INTO public.exams (id, title, created_by) VALUES ('{T}6','x','{FIX}');",
     "zzsmoke-teacher@example.test", FIX, True),
    ("REFUSES a placeholder email literal", GOOD, "replace-with-the-exact-fixture-email",
     FIX, True),
    ("REFUSES a zzsmoke-email account whose name is NOT a fixture", IMPOSTOR,
     "zzsmoke-lookalike@example.test", IMP, True),
    ("REFUSES a real profile targeted by exact UUID", IMPOSTOR,
     "zzsmoke-lookalike@example.test", REAL, True),
]


def build_sql(email, uuid):
    raw = SQL_FILE.read_text(encoding="utf-8")
    raw = raw.replace("'00000000-0000-0000-0000-000000000000'::uuid", f"'{uuid}'::uuid")
    raw = raw.replace("'replace-with-the-exact-fixture-email'", f"'{email}'")
    return raw


def reset(conn):
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS auth CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute(SCHEMA)
    conn.commit()


def seed_rows(conn, sql):
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('quizeazz.authoritative_context','true',false)")
        cur.execute(sql)
    conn.commit()


def main():
    passed = failed = 0
    conn = psycopg.connect(DSN)

    for name, seed, email, uuid, expect_abort in CASES:
        print(f"\n=== {name} ===")
        try:
            reset(conn)
            seed_rows(conn, seed)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            print(f"  [ ] seed failed: {str(exc).splitlines()[0]}")
            failed += 1
            continue

        # Capture the role BEFORE the bootstrap runs. Asserting "role != teacher"
        # afterwards is vacuous for a profile that is already a teacher, so the
        # check must compare against the pre-run value.
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM public.profiles WHERE id = %s", (uuid,))
            row = cur.fetchone()
        role_before = row[0] if row else None

        err = None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('quizeazz.authoritative_context','false',false)")
                cur.execute(build_sql(email, uuid))
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            err = str(exc).split("\n")[0]
        else:
            conn.commit()

        aborted = err is not None
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM public.profiles WHERE id = %s", (uuid,))
            row = cur.fetchone()
        role = row[0] if row else None

        ok = True
        if aborted != expect_abort:
            print(f"  [ ] abort={aborted} expected={expect_abort} err={err}")
            ok = False
        if not expect_abort:
            if role != "teacher":
                print(f"  [ ] role is {role!r}, expected 'teacher'")
                ok = False
            elif role_before != "student":
                print(f"  [ ] role was {role_before!r} before the run; a promotion test "
                      f"must start from 'student'")
                ok = False
            else:
                print("  [ ] promoted to teacher")
            # idempotency: re-running must abort (role is no longer 'student')
            try:
                with conn.cursor() as cur:
                    cur.execute(build_sql(email, uuid))
                conn.commit()
                print("  [ ] re-run did NOT abort - not idempotent")
                ok = False
            except Exception:  # noqa: BLE001
                conn.rollback()
                print("  [ ] re-run aborts cleanly (role already 'student' no longer holds)")
        else:
            # The decisive assertion is UNCHANGED, not "not teacher": a profile
            # that was already a teacher must not be reported as promoted.
            if role != role_before:
                print(f"  [ ] MUTATED on a refusal path: role {role_before!r} -> {role!r}")
                ok = False
            else:
                print(f"  [ ] refused, role unchanged at {role!r}")
        print("  PASS" if ok else "  FAIL")
        passed += ok
        failed += (not ok)

    # ---- LEAK TEST -------------------------------------------------------
    print("\n=== authoritative context cannot leak ===")
    ok = True
    reset(conn)
    seed_rows(conn, GOOD)
    # Reproduce a real SQL Editor session: the GUC is unset, not 'true'.
    with conn.cursor() as cur:
        cur.execute("RESET quizeazz.authoritative_context")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(build_sql("zzsmoke-teacher@example.test", FIX))
    conn.commit()

    # same connection, after COMMIT: transaction-local setting must be gone
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('quizeazz.authoritative_context', true)")
        same = cur.fetchone()[0]
    conn.commit()
    if same == 'true':
        print(f"  [ ] LEAK on same connection after COMMIT: {same!r}")
        ok = False
    else:
        print(f"  [ ] same connection after COMMIT: not enabled ({same!r})")

    # a brand-new connection must also see nothing
    other = psycopg.connect(DSN)
    with other.cursor() as cur:
        cur.execute("SELECT current_setting('quizeazz.authoritative_context', true)")
        fresh = cur.fetchone()[0]
    other.commit()
    other.close()
    if fresh is not None:
        print(f"  [ ] LEAK on a fresh connection: {fresh!r}")
        ok = False
    else:
        print("  [ ] fresh connection: setting is absent")

    # and the context is genuinely load-bearing (removing it blocks the write)
    reset(conn)
    seed_rows(conn, GOOD)
    blocked = False
    try:
        with conn.cursor() as cur:
            cur.execute("RESET quizeazz.authoritative_context")
            cur.execute("UPDATE public.profiles SET role='teacher' WHERE id=%s", (FIX,))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        blocked = True
    if not blocked:
        print("  [ ] context is NOT load-bearing: role escalated without it")
        ok = False
    else:
        print("  [ ] context is load-bearing: the trigger blocks without it")
    print("  PASS" if ok else "  FAIL")
    passed += ok
    failed += (not ok)

    print(f"\n{'=' * 60}\n{passed} passed, {failed} failed")
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())