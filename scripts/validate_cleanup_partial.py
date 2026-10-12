"""
Validate deploy/ZZSMOKE_CLEANUP.sql against a replica that has
the REAL production protection triggers, across:

  * a complete fixture
  * every partial-setup stage (setup can stop anywhere)
  * idempotent re-runs
  * eight distinct fail-closed violations

Every negative case must ABORT with ZERO rows changed.

!! DESTRUCTIVE -- DISPOSABLE TEST DATABASES ONLY !!

  seed() issues `DROP SCHEMA IF EXISTS public CASCADE` and the same for
  `auth`, then rebuilds both, once per scenario. There is no undo and no
  prompt. Pointing this at anything other than a throwaway database destroys
  it.

  A fail-closed safety gate runs at import time, before psycopg.connect() and
  before any SQL executes. It rejects missing or malformed DSNs, non-loopback
  hosts, multi-host and Unix-socket connection strings, and connection strings
  that name no host or no database. It then requires an explicit per-run
  opt-in. A loopback address is NOT accepted as proof that the target is
  disposable: a developer machine can also run a local stack holding real
  fixtures, so the operator's opt-in is the only basis for that claim.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg  # noqa: E402
from psycopg.conninfo import conninfo_to_dict  # noqa: E402
from validate_cleanup_script import SCHEMA  # noqa: E402

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


SQL_FILE = Path(__file__).resolve().parent.parent / "deploy" / "ZZSMOKE_CLEANUP.sql"
DSN = enforce_disposable_target(__import__("os").environ.get(DSN_ENV_VAR),
                                __import__("os").environ.get(OPT_IN_ENV_VAR))

T = "11111111-1111-1111-1111-00000000000"
FIX = {k: f"{T}{v}" for k, v in
       dict(t="1", s="2", ex="6", q="8", a="a", x="c").items()}

# Unrelated (must survive every single scenario untouched)
REAL = f"""
INSERT INTO auth.users VALUES ('{T}3'), ('{T}4'), ('{T}5');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{T}3','Real Student','student'), ('{T}4','Real Teacher','teacher'),
  ('{T}5','Another Real','student');
INSERT INTO public.exams (id, title, created_by) VALUES ('{T}7','Real Exam','{T}4');
INSERT INTO public.questions (id, title, created_by) VALUES ('{T}9','Real Question','{T}4');
INSERT INTO public.programming_test_cases (id, question_id, expected_stdout)
  VALUES ('aa000000-0000-0000-0000-000000000009','{T}9','7');
INSERT INTO public.exam_questions (id, exam_id, question_id)
  VALUES ('bb000000-0000-0000-0000-000000000009','{T}7','{T}9');
INSERT INTO public.exam_assignments (exam_id, student_id) VALUES ('{T}7','{T}3');
INSERT INTO public.exam_attempts (id, exam_id, student_id, status)
  VALUES ('{T}b','{T}7','{T}3','evaluated');
INSERT INTO public.answers (id, attempt_id, question_id, evaluation_status)
  VALUES ('cc000000-0000-0000-0000-000000000009','{T}b','{T}9','Correct');
INSERT INTO public.security_events (id, attempt_id, exam_id, question_id, student_id)
  VALUES ('dd000000-0000-0000-0000-000000000009','{T}b','{T}7','{T}9','{T}3');
"""

ACCOUNTS = f"""
INSERT INTO auth.users VALUES ('{FIX["t"]}'), ('{FIX["s"]}');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{FIX["t"]}','ZZSMOKE Teacher','teacher'), ('{FIX["s"]}','ZZSMOKE Student','student');
"""
ACCOUNTS_UNPROMOTED = f"""
INSERT INTO auth.users VALUES ('{FIX["t"]}'), ('{FIX["s"]}');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{FIX["t"]}','ZZSMOKE Teacher','student'), ('{FIX["s"]}','ZZSMOKE Student','student');
"""
ACCOUNTS_LOWERCASE = f"""
INSERT INTO auth.users VALUES ('{FIX["t"]}'), ('{FIX["s"]}');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{FIX["t"]}','zzsmoke-teacher-20261012','teacher'),
  ('{FIX["s"]}','zzsmoke-student-20261012','student');
"""
QUESTION = f"""
INSERT INTO public.questions (id, title, created_by) VALUES ('{FIX["q"]}','ZZSMOKE Q','{FIX["t"]}');
INSERT INTO public.programming_test_cases (id, question_id, expected_stdout)
  VALUES ('aa000000-0000-0000-0000-000000000008','{FIX["q"]}','42');
"""
EXAM = f"""
INSERT INTO public.exams (id, title, created_by) VALUES ('{FIX["ex"]}','ZZSMOKE Exam','{FIX["t"]}');
INSERT INTO public.exam_questions (id, exam_id, question_id)
  VALUES ('bb000000-0000-0000-0000-000000000008','{FIX["ex"]}','{FIX["q"]}');
INSERT INTO public.exam_assignments (exam_id, student_id) VALUES ('{FIX["ex"]}','{FIX["s"]}');
"""
ATTEMPT = f"""
INSERT INTO public.exam_attempts (id, exam_id, student_id, status)
  VALUES ('{FIX["a"]}','{FIX["ex"]}','{FIX["s"]}','evaluated');
INSERT INTO public.answers (id, attempt_id, question_id, evaluation_status)
  VALUES ('cc000000-0000-0000-0000-000000000008','{FIX["a"]}','{FIX["q"]}','Correct');
INSERT INTO public.security_events (id, attempt_id, exam_id, question_id, student_id)
  VALUES ('dd000000-0000-0000-0000-000000000008','{FIX["a"]}','{FIX["ex"]}','{FIX["q"]}','{FIX["s"]}');
"""

SCENARIOS = [
    # name, extra seed, expect_abort
    ("complete fixture",          REAL + ACCOUNTS + QUESTION + EXAM + ATTEMPT, False),
    # REGRESSION: the live fixture accounts were created with lowercase names
    # ('zzsmoke-...'), while the marker check is 'ZZSMOKE'. A case-sensitive
    # LIKE aborted here at classification; a partial fix aborted LATE at the
    # profile DELETE ("expected to delete 1 teacher profile, deleted 0").
    ("REGRESSION lowercase fixture names (live production spelling)",
                                 REAL + ACCOUNTS_LOWERCASE + QUESTION + EXAM + ATTEMPT, False),
    ("REGRESSION lowercase names, accounts only",
                                 REAL + ACCOUNTS_LOWERCASE, False),
    ("mixed-case fixture names also accepted",
                                 REAL + (f"""
INSERT INTO auth.users VALUES ('{FIX["t"]}'), ('{FIX["s"]}');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{FIX["t"]}','ZzSmOkE Teacher','teacher'),
  ('{FIX["s"]}','zZSMOKE Student','student');
""") + QUESTION + EXAM + ATTEMPT, False),
    # NOTE: the existing "NEG teacher profile exists but is not ZZSMOKE" case
    # is the guard that ILIKE could have widened - it names the profile
    # 'Promoted Real' and must still abort. That case covers it.
    ("partial: accounts only (teacher never promoted)",
                                 REAL + ACCOUNTS_UNPROMOTED, False),
    ("partial: question + test cases, no exam",
                                 REAL + ACCOUNTS + QUESTION, False),
    ("partial: exam published, attempt never started",
                                 REAL + ACCOUNTS + QUESTION + EXAM, False),
    ("NEG real exam id in manifest",
                                 REAL + ACCOUNTS + QUESTION + EXAM + ATTEMPT, True),
    ("NEG question shared with a real paper",
                                 REAL + ACCOUNTS + QUESTION + EXAM + ATTEMPT
                                 + f"INSERT INTO public.exam_questions (id, exam_id, question_id)"
                                   f" VALUES ('bb000000-0000-0000-0000-0000000000ee','{T}7','{FIX["q"]}');", True),
    ("NEG attempt belongs to a real student",
                                 REAL + ACCOUNTS + QUESTION + EXAM + ATTEMPT, True),
    ("NEG security_event ties a fixture account to a real exam",
                                 REAL + ACCOUNTS + QUESTION + EXAM + ATTEMPT
                                 + f"INSERT INTO public.security_events (id, attempt_id, exam_id, question_id, student_id)"
                                   f" VALUES ('dd000000-0000-0000-0000-0000000000ee','{T}b','{T}7','{T}9','{FIX["s"]}');", True),
    ("NEG exam's real attempt is not the manifest attempt (no inference)",
                                 REAL + ACCOUNTS + QUESTION + EXAM
                                 + f"INSERT INTO public.exam_attempts (id, exam_id, student_id, status)"
                                   f" VALUES ('{T}e','{FIX["ex"]}','{FIX["s"]}','in_progress');", True),
    ("NEG teacher profile exists but is not ZZSMOKE",
                                 REAL + f"""
INSERT INTO auth.users VALUES ('{FIX["t"]}'), ('{FIX["s"]}');
INSERT INTO public.profiles (id, full_name, role) VALUES
  ('{FIX["t"]}','Promoted Real','student'), ('{FIX["s"]}','ZZSMOKE Student','student');
""", True),
    ("NEG exam assigned to a second student",
                                 REAL + ACCOUNTS + QUESTION + EXAM + ATTEMPT
                                 + f"INSERT INTO public.exam_assignments (exam_id, student_id)"
                                   f" VALUES ('{FIX["ex"]}','{T}3');", True),
    ("NEG exam paper references a real question",
                                 REAL + ACCOUNTS + QUESTION
                                 + f"INSERT INTO public.exams (id, title, created_by)"
                                   f" VALUES ('{FIX["ex"]}','ZZSMOKE Exam','{FIX["t"]}');"
                                   + f"INSERT INTO public.exam_questions (id, exam_id, question_id)"
                                   f" VALUES ('bb000000-0000-0000-0000-0000000000ff','{FIX["ex"]}','{T}9');"
                                 + ATTEMPT, True),
]

MANIFEST_DEFAULT = {"teacher": FIX["t"], "student": FIX["s"], "exam": FIX["ex"],
                    "question": FIX["q"], "attempt": FIX["a"]}
MANIFEST_NEG_EXAM = dict(MANIFEST_DEFAULT, exam=f"{T}7")
MANIFEST_NEG_ATT = dict(MANIFEST_DEFAULT, attempt=f"{T}b")

TABLES = ["auth.users", "public.profiles", "public.exams", "public.questions",
          "public.programming_test_cases", "public.exam_questions",
          "public.exam_assignments", "public.exam_attempts", "public.answers",
          "public.security_events"]
# auth.users deliberately stays at 5: this script stops at public.profiles and
# the auth identities are removed separately by the Auth Admin API. That
# non-mutation is itself part of what we are asserting.
REAL_IDS = {"auth.users": 5, "public.profiles": 3, "public.exams": 1, "public.questions": 1,
            "public.programming_test_cases": 1, "public.exam_questions": 1,
            "public.exam_assignments": 1, "public.exam_attempts": 1,
            "public.answers": 1, "public.security_events": 1}


def build_sql(ids):
    raw = SQL_FILE.read_text(encoding="utf-8")
    raw = raw.replace("'00000000-0000-0000-0000-000000000001'::uuid", f"'{ids['teacher']}'::uuid")
    raw = raw.replace("'00000000-0000-0000-0000-000000000002'::uuid", f"'{ids['student']}'::uuid")
    raw = raw.replace("'00000000-0000-0000-0000-000000000003'::uuid", f"'{ids['exam']}'::uuid")
    raw = raw.replace("'00000000-0000-0000-0000-000000000004'::uuid", f"'{ids['question']}'::uuid")
    raw = raw.replace("'00000000-0000-0000-0000-000000000005'::uuid", f"'{ids['attempt']}'::uuid")
    return raw


def counts(conn):
    out = {}
    with conn.cursor() as cur:
        for t in TABLES:
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
    return out


def seed(conn, sql):
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS auth CASCADE")
        cur.execute(SCHEMA)
        # Seeding mirrors what the real RPCs do; run_cleanup() resets this to
        # 'false' so every cleanup attempt faces a fresh SQL-editor session.
        cur.execute("SELECT set_config('quizeazz.authoritative_context','true',false)")
        cur.execute(sql)
    conn.commit()


def run_cleanup(conn, ids, context=True):
    """context=False reproduces a real SQL Editor session: the GUC is unset."""
    err = None
    try:
        with conn.cursor() as cur:
            if context:
                cur.execute("SELECT set_config('quizeazz.authoritative_context','false',false)")
            else:
                cur.execute("RESET quizeazz.authoritative_context")
            cur.execute(build_sql(ids))
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        err = str(exc).split("\n")[0]
        return err
    conn.commit()
    return None


def main():
    passed = failed = 0
    conn = psycopg.connect(DSN)
    conn.autocommit = False
    for idx, (name, sql, expect_abort) in enumerate(SCENARIOS):
        print(f"\n=== [{idx}] {name} ===")
        manifest = MANIFEST_DEFAULT
        if name == "NEG real exam id in manifest":
            manifest = MANIFEST_NEG_EXAM
        elif name == "NEG attempt belongs to a real student":
            manifest = MANIFEST_NEG_ATT
        try:
            seed(conn, sql)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            print(f"  [ ] seed failed: {str(exc).splitlines()[0]}")
            failed += 1
            continue
        before = counts(conn)
        err = run_cleanup(conn, manifest)
        after = counts(conn)
        aborted = err is not None

        ok = True
        if aborted != expect_abort:
            print(f"  [ ] abort={aborted} expected={expect_abort}  {err}")
            ok = False
        if expect_abort:
            if before != after:
                print(f"  [ ] MUTATED ON ABORT: {before} -> {after}")
                ok = False
            else:
                print("  [ ] nothing mutated on abort")
        else:
            if after != REAL_IDS:
                print(f"  [ ] residual fixture rows: {after}")
                ok = False
            else:
                print(f"  [ ] fixture removed, real data intact")
            # idempotency: a second identical run must delete nothing and not abort
            b2 = counts(conn)
            err2 = run_cleanup(conn, manifest)
            a2 = counts(conn)
            if err2 is not None or b2 != a2:
                print(f"  [ ] NOT IDEMPOTENT: err={err2} {b2} -> {a2}")
                ok = False
            else:
                print("  [ ] re-run is a safe no-op")
        print("  PASS" if ok else "  FAIL")
        passed += ok
        failed += (not ok)

    # ---- LEAK PROOF --------------------------------------------------------
    # The cleanup script enables the authoritative context itself, and only
    # after every validation gate has passed. The setting is transaction-local,
    # so it must be gone once the transaction ends - on this connection and on
    # any other. This is what proves the admin context cannot escape.
    print("\n=== authoritative context does not leak out of the transaction ===")
    ok = True
    try:
        seed(conn, REAL + ACCOUNTS + QUESTION + EXAM + ATTEMPT)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        print(f"  [ ] seed failed: {str(exc).splitlines()[0]}")
        return 1
    err = run_cleanup(conn, MANIFEST_DEFAULT, context=False)
    if err is not None or counts(conn) != REAL_IDS:
        print(f"  [ ] cleanup did not complete: {err}")
        ok = False
    else:
        print("  [ ] cleanup completed (it self-enables the context after validation)")

    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('quizeazz.authoritative_context', true)")
        same = cur.fetchone()[0]
    conn.commit()
    if same == 'true':
        print(f"  [ ] LEAK on same connection after COMMIT: {same!r}")
        ok = False
    else:
        print(f"  [ ] same connection after COMMIT: not enabled ({same!r})")

    other = psycopg.connect(DSN)
    with other.cursor() as cur:
        cur.execute("SELECT current_setting('quizeazz.authoritative_context', true)")
        fresh = cur.fetchone()[0]
    other.commit()
    other.close()
    if fresh == 'true':
        print(f"  [ ] LEAK on a fresh connection: {fresh!r}")
        ok = False
    else:
        print(f"  [ ] fresh connection: not enabled ({fresh!r})")
    print("  PASS" if ok else "  FAIL")
    passed += ok
    failed += (not ok)

    # ---- SINGLE-TRANSACTION PROOF ----------------------------------------
    # The critical claim: if a DELETE fails part-way through, every delete
    # already executed must roll back. We force that by installing a trigger
    # that raises on DELETE of public.questions - which the script reaches
    # AFTER deleting answers, security_events, the attempt, exam_questions,
    # exam_assignments and the exam. If the block were not one transaction,
    # those rows would already be gone.
    print("\n=== single transaction: a mid-sequence failure rolls everything back ===")
    ok = True
    try:
        seed(conn, REAL + ACCOUNTS + QUESTION + EXAM + ATTEMPT)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        print(f"  [ ] seed failed: {str(exc).splitlines()[0]}")
        return 1
    with conn.cursor() as cur:
        cur.execute("""
          CREATE FUNCTION public.block_q_delete() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'injected failure during cleanup'; END; $$;
          CREATE TRIGGER t_block BEFORE DELETE ON public.questions
            FOR EACH ROW EXECUTE FUNCTION block_q_delete();""")
    conn.commit()

    before = counts(conn)
    err = run_cleanup(conn, MANIFEST_DEFAULT, context=False)
    after = counts(conn)
    if err is None:
        print("  [ ] cleanup completed - the injected failure never fired")
        ok = False
    else:
        print(f"  [ ] aborted mid-sequence: {err[:60]}")
    if before != after:
        print(f"  [ ] PARTIAL DELETE COMMITTED: {before}")
        print(f"                              -> {after}")
        ok = False
    else:
        print("  [ ] every earlier delete rolled back - zero mutation")

    # after removing the injected trigger, the same script must now succeed
    with conn.cursor() as cur:
        cur.execute("DROP TRIGGER t_block ON public.questions")
        cur.execute("DROP FUNCTION public.block_q_delete()")
    conn.commit()
    err2 = run_cleanup(conn, MANIFEST_DEFAULT, context=False)
    if err2 is not None or counts(conn) != REAL_IDS:
        print(f"  [ ] did not recover after the obstacle cleared: {err2}")
        ok = False
    else:
        print("  [ ] completes cleanly once the obstacle is removed")
    print("  PASS" if ok else "  FAIL")
    passed += ok
    failed += (not ok)

    print(f"\n{'=' * 60}\n{passed} passed, {failed} failed")
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())