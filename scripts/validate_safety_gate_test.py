"""
Tests for the disposable-target safety gate shared by validate_bootstrap.py and
validate_cleanup_partial.py.

Fully offline and deterministic. No database, no container, no network, and no
Supabase CLI is used or needed:

  * PART 1 imports each validator with a VALID loopback DSN so the module-level
    gate lets it through, then replaces `psycopg.connect` with a recording
    tripwire and calls `enforce_disposable_target()` directly. Any connection
    attempt would show up in the tripwire.

  * PART 2 runs the real scripts as subprocesses with rejected configurations.
    Their DSNs point at 127.0.0.1:59999, where nothing is listening. If the gate
    did NOT run first, the script would instead attempt a connection and fail
    with psycopg.OperationalError - a different exit code and a traceback. So
    "exit 2 + REFUSED banner + no traceback + no OperationalError" proves the
    gate preceded any connect.

Neither part executes SQL: the gate refuses, or (for accepted configs) returns
without touching the driver.

Run:  python scripts/validate_safety_gate_test.py
"""
import contextlib
import importlib
import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TARGETS = ("validate_bootstrap", "validate_cleanup_partial")

ACK = "yes-this-is-a-disposable-test-database"
DSN_ENV = "ZZSMOKE_VALIDATION_DSN"
ACK_ENV = "QUIZEAZZ_DISPOSABLE_DSN_ACK"

# A recognisable password so the suite can prove it is never echoed. It is a
# fixture string for a database that is never contacted.
VALID_DSN = "postgresql://postgres:SENTINEL_PW_9f3a@127.0.0.1:59999/zz_probe"
SECRET = "SENTINEL_PW_9f3a"

# Nothing listens here; used only so a missed gate fails loudly.
DEAD_DSN = "postgresql://postgres:pw@127.0.0.1:59999/zz_dead_probe"

# (label, dsn, opt_in, expected refusal reason)
REJECT = [
    ("missing DSN", None, ACK, "is unset or empty"),
    ("empty DSN", "", ACK, "is unset or empty"),
    ("whitespace DSN", "     ", ACK, "is unset or empty"),
    ("malformed DSN", "this is not a dsn", ACK, "not a valid PostgreSQL connection string"),
    ("remote host", "postgresql://u:p@db.example.com:5432/prod", ACK, "is not a loopback address"),
    ("supabase host", "postgresql://u:p@xyzcompany.supabase.co:5432/p", ACK, "is not a loopback address"),
    ("localhost hostname", "postgresql://u:p@localhost:5432/zz", ACK, "does not identify the target"),
    ("unix socket path", "host=/var/run/postgresql dbname=zz user=postgres", ACK,
     "Unix-socket connections are not supported"),
    # libpq treats a backslash as an escape, so the quoted form survives and the
    # unquoted form collapses to "pipepostgres". Both must be refused, by
    # whichever branch catches them.
    ("win pipe QUOTED", 'host="\\\\pipe\\\\postgres" dbname=zz', ACK,
     "Unix-socket connections are not supported"),
    ("win pipe unquoted (escape collapses)", "host=\\pipe\\postgres dbname=zz", ACK,
     "is not a loopback address"),
    ("multi-host", "host=127.0.0.1,127.0.0.2 port=5432 dbname=zz", ACK,
     "multi-host connection strings are not supported"),
    ("no host", "dbname=zz user=postgres", ACK, "names no host"),
    ("no dbname", "postgresql://u:p@127.0.0.1:5432", ACK, "names no database"),
    ("IPv6 non-loopback", "postgresql://u:p@[2001:db8::1]:5432/zz", ACK, "is not a loopback address"),
    ("IPv4-mapped loopback", "postgresql://u:p@[::ffff:127.0.0.1]:5432/zz", ACK, "is not a loopback address"),
    ("opt-in absent", VALID_DSN, None, "is not set to the exact value required"),
    ("opt-in wrong value", VALID_DSN, "yes", "is not set to the exact value required"),
    ("opt-in wrong case", VALID_DSN, ACK.upper(), "is not set to the exact value required"),
    ("opt-in empty string", VALID_DSN, "", "is not set to the exact value required"),
    ("port out of range", "postgresql://u:p@127.0.0.1:99999/zz", ACK, "outside the range"),
    ("port not a number", "host=127.0.0.1 port=notanumber dbname=zz", ACK, "is not a number"),
]

# (label, dsn) - configurations the gate must accept.
ACCEPT = [
    ("valid IPv4 loopback", VALID_DSN),
    ("valid IPv6 loopback", "postgresql://postgres:pw@[::1]:59999/zz_probe"),
    ("IPv6 with zone id", "postgresql://postgres:pw@[::1%25eth0]:59999/zz_probe"),
    ("URI-encoded creds", "postgresql://postgres:p%40ss%3Aword%2Fslash@127.0.0.1:59999/zz_probe"),
    ("keyword form loopback", "host=127.0.0.1 port=59999 dbname=zz_probe user=postgres password=pw"),
    ("no port (libpq default)", "postgresql://postgres:pw@127.0.0.1/zz_probe"),
]

# (label, dsn, opt_in) for the subprocess pass.
SUBPROC_CASES = [
    ("missing DSN", None, ACK),
    ("absent opt-in", DEAD_DSN, None),
    ("wrong opt-in", DEAD_DSN, "yes"),
    ("remote host", "postgresql://u:p@db.example.com:5432/prod", ACK),
    ("supabase host", "postgresql://u:p@abc.supabase.co:5432/p", ACK),
    ("malformed DSN", "this is not a dsn", ACK),
    ("unix socket", "host=/var/run/postgresql dbname=zz", ACK),
    ("multi-host", "host=127.0.0.1,127.0.0.2 port=5432 dbname=zz", ACK),
    ("no host", "dbname=zz user=postgres", ACK),
    ("no dbname", "postgresql://u:p@127.0.0.1:5432", ACK),
]

results = []


def check(name, cond, detail=""):
    print(f"  [{'x' if cond else ' '}] {name}" + (f"\n        {detail}" if detail else ""))
    results.append(bool(cond))


def load(mod_name):
    """Import with a VALID env so the module-level gate lets us through."""
    os.environ[DSN_ENV] = VALID_DSN
    os.environ[ACK_ENV] = ACK
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop(mod_name, None)
    with contextlib.redirect_stdout(io.StringIO()):
        return importlib.import_module(mod_name)


def part1():
    print("=== PART 1  gate unit checks (psycopg.connect is a tripwire) ===")
    import psycopg

    real_connect = psycopg.connect
    for mod_name in TARGETS:
        print(f"\n--- {mod_name}")
        mod = load(mod_name)
        attempts = []

        def tripwire(*a, **k):
            attempts.append(a)
            raise RuntimeError("connect attempted")

        psycopg.connect = tripwire
        try:
            raised = 0
            for label, dsn, ack, why in REJECT:
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        mod.enforce_disposable_target(dsn, ack)
                except SystemExit as exc:
                    raised += 1
                    out = buf.getvalue()
                    check(f"{label}: nonzero exit",
                          isinstance(exc.code, int) and exc.code != 0, f"code={exc.code}")
                    check(f"{label}: REFUSED banner", "REFUSED" in out)
                    check(f"{label}: actionable reason", why in out, f"expected {why!r}")
                    check(f"{label}: documents opt-in var", ACK_ENV in out)
                    check(f"{label}: no secret", SECRET not in out)
                    check(f"{label}: no traceback", "Traceback" not in out)
                    if dsn and dsn.strip():
                        check(f"{label}: DSN not echoed", dsn not in out)
                except Exception as exc:  # noqa: BLE001
                    check(f"{label}: SystemExit", False,
                          f"raised {type(exc).__name__}: {exc}")
                else:
                    check(f"{label}: refused", False, "RETURNED - GATE LEAKED")

            check(f"all {len(REJECT)} rejections raised SystemExit",
                  raised == len(REJECT), f"{raised}/{len(REJECT)}")
            check("NO CONNECTION attempted on any rejection", not attempts, str(attempts))

            for label, dsn in ACCEPT:
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        got = mod.enforce_disposable_target(dsn, ACK)
                except SystemExit as exc:
                    check(f"{label}: accepted", False, f"refused exit {exc.code}")
                else:
                    out = buf.getvalue()
                    check(f"{label}: accepted, DSN unchanged", got == dsn)
                    check(f"{label}: PASSED banner", "gate: PASSED" in out)
                    check(f"{label}: no secret", SECRET not in out and "p%40ss" not in out)
                    check(f"{label}: loopback caveat",
                          "does NOT prove this database is disposable" in out)
                    check(f"{label}: db identity printed", "database :" in out)

            check("NO CONNECTION attempted on acceptance", not attempts, str(attempts))
        finally:
            psycopg.connect = real_connect


def part2():
    print("\n=== PART 2  real scripts as subprocesses (must refuse at import) ===")
    for name in TARGETS:
        print(f"\n--- scripts/{name}.py")
        for label, dsn, ack in SUBPROC_CASES:
            env = {k: v for k, v in os.environ.items()
                   if k not in (DSN_ENV, ACK_ENV)}
            if dsn is not None:
                env[DSN_ENV] = dsn
            if ack is not None:
                env[ACK_ENV] = ack
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / f"{name}.py")],
                cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            out = proc.stdout + proc.stderr
            checks = {
                "exit 2": proc.returncode == 2,
                "REFUSED banner": "REFUSED" in out,
                "no traceback": "Traceback" not in out,
                "no connection error": ("OperationalError" not in out
                                        and "connection failed" not in out.lower()),
                "gate did not PASS": "gate: PASSED" not in out,
                "no test case ran": ("=== REGRESSION" not in out
                                     and "complete fixture" not in out),
            }
            bad = [k for k, v in checks.items() if not v]
            check(f"{label}: refused before connecting", not bad,
                  f"returncode={proc.returncode} failing={bad} output={out.strip()[:200]!r}")


def main():
    saved = {k: os.environ.get(k) for k in (DSN_ENV, ACK_ENV)}
    try:
        part1()
        part2()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    failed = results.count(False)
    passed = len(results) - failed
    print(f"\n{'=' * 60}\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
