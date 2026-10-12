"""
burst_75_harness.py - 75-SUBMISSION GRADING BURST VALIDATION (three legs).

LOCAL AND DISPOSABLE ONLY. This harness never contacts Judge0, Supabase Auth,
any Supabase project endpoint, or production. Every Judge0 response is
synthetic.

DOCKER RESOURCE CLEANUP
  Every CONTAINER and VOLUME this harness creates is created by this run,
  labelled, inventoried, and removed by exact ID. Leg C is the only leg that
  creates Docker resources; cleanup_owned() removes only what this run's own
  ledger names, and only when the live ID and the owner label still match.

LEG B DATABASE RESIDUE - NOT CLEANED UP
  Leg B is the exception, and the difference is not cosmetic. It does NOT
  create a database: it connects to a DISPOSABLE database supplied by the
  operator and creates tables, rows and cluster roles inside that existing
  database, then removes NONE of them. Those objects are this run's residue
  and they persist after it finishes. Leg B is disabled unless an operator
  acknowledges that residue explicitly, and the acknowledgement does not make
  the residue removable.

WHAT IS DEMONSTRATED, AND WHAT IS NOT
  Leg A  Orchestration: wave admission, wall-clock budget, deferral, recovery,
         failure isolation, and the browser-path hang. Runs the LIFTED shipped
         code under a VIRTUAL clock. Virtual time is model time. It proves
         NOTHING about the Supabase Edge Runtime's execution ceiling.
  Leg B  Real PostgreSQL: the ACTUAL migration 20 verdict body under genuine
         concurrent connections. This is the only leg that can prove the
         FOR UPDATE row lock.
  Leg C  Real Edge Runtime + real wall clock: whether a hung Judge0 request is
         terminated by the runtime or survives until an external watchdog.

SYNTHETIC JUDGE0 CAVEAT
  No C++ is compiled and no program is executed. "Correct" in these results
  means "the fake Judge0 returned status_id 3 with stdout matching
  expected_stdout". Compilation correctness and Judge0 correctness are NOT
  validated by this harness.

SCOPE CAVEAT
  75 means 75 synthetic ATTEMPTS pushed through the sweep path. This is NOT a
  test of 75 simultaneous authenticated browser clients, and no such claim is
  made.

Usage
  python scripts/burst_75_harness.py --leg a                  # orchestration, no containers
  python scripts/burst_75_harness.py --leg a --no-inventory  # same, and never calls Docker
  python scripts/burst_75_harness.py --leg b                  # needs a disposable DSN AND both acks below
  python scripts/burst_75_harness.py --leg c                  # needs the cached edge image
  python scripts/burst_75_harness.py --inventory              # read-only docker inventory
  python scripts/burst_75_harness.py --leg all                # runs Leg A, then refuses Leg B

Leg B needs all three of these before it will run at all. The two
acknowledgements are independent, and neither is remembered between runs:
  ZZSMOKE_BURST_DSN=postgresql://USER:PASSWORD@127.0.0.1:PORT/DISPOSABLE_DB
  QUIZEAZZ_DISPOSABLE_DSN_ACK=yes-this-is-a-disposable-test-database
  QUIZEAZZ_LEG_B_DESTRUCTIVE_ACK=yes-leg-b-leaves-objects-in-the-target-database
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FUNCS = ROOT / "supabase" / "functions" / "judge0-grade"
DRIVER = SCRIPTS / "burst_75_leg_a.driver.mjs"
FAKE_J0 = SCRIPTS / "burst_75_fake_judge0.mjs"

# Every resource this harness creates carries this label. Cleanup selects on the
# label AND verifies the recorded ID, so a name prefix alone can never trigger a
# deletion.
OWNER_LABEL = "com.quizeazz.burst-run"
RUN_ID = "burst75-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

# Leg B safety constants. Identical in spirit to the committed validators: the
# opt-in is required on every run and is never remembered.
DSN_ENV = "ZZSMOKE_BURST_DSN"
ACK_ENV = "QUIZEAZZ_DISPOSABLE_DSN_ACK"
ACK_VALUE = "yes-this-is-a-disposable-test-database"
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
# Leg B writes tables, rows and cluster roles into the DSN's OWN database and
# never cleans them up. That cannot be made safe by cleanup logic alone, so it is
# disabled unless an operator explicitly acknowledges the residue.
LEG_B_ACK_ENV = "QUIZEAZZ_LEG_B_DESTRUCTIVE_ACK"
LEG_B_ACK_VALUE = "yes-leg-b-leaves-objects-in-the-target-database"

LEDGE = Path(os.environ.get("TEMP", "/tmp")) / f"burst75_resources_{RUN_ID}.json"
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'x' if cond else ' '}] {name}" + (f"\n        {detail}" if detail else ""))
    return bool(cond)


# ============================================================== INVENTORY ====
def docker(*args):
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def inventory():
    """Read-only snapshot of every docker resource. Recorded pre and post."""
    _, containers, _ = docker("ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}")
    _, volumes, _ = docker("volume", "ls", "--format", "{{.Name}}")
    _, networks, _ = docker("network", "ls", "--format", "{{.Name}}")
    _, images, _ = docker("images", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}")
    return {
        "containers": [l for l in containers.splitlines() if l],
        "volumes": [l for l in volumes.splitlines() if l],
        "networks": [l for l in networks.splitlines() if l],
        "images": [l for l in images.splitlines() if l],
    }


def record(kind, ident, name, port=None):
    """Append a created resource to the ownership ledger, then persist it."""
    entry = {"kind": kind, "id": ident, "name": name, "port": port, "run": RUN_ID}
    data = json.loads(LEDGE.read_text(encoding="utf-8")) if LEDGE.exists() else {"run": RUN_ID, "created": []}
    data["created"].append(entry)
    LEDGE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"    recorded {kind} {ident} ({name})")
    return entry


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Run-specific scratch directory.
#
# It MUST live inside the repository. Node's ESM resolver resolves a bare
# specifier like 'typescript' by walking UP from the importing file's own
# directory, so a runner under %TEMP% cannot see the repo's node_modules.
# NODE_PATH does not help: it is a CommonJS-only mechanism and the ESM
# resolver ignores it entirely (verified: with NODE_PATH set to the repo's
# node_modules, an ESM probe under %TEMP% still fails ERR_MODULE_NOT_FOUND,
# while the identical probe inside the repo resolves and transpiles).
#
# The directory is named for this run and carries an owner marker. Cleanup
# removes exactly that path and only after confirming the marker matches.
# ---------------------------------------------------------------------------
SCRATCH_PREFIX = ".burst75_"


def make_scratch(kind):
    """Create this run's scratch dir inside the repo, with an ownership marker."""
    path = SCRIPTS / f"{SCRATCH_PREFIX}{kind}_{RUN_ID}"
    if path.exists():
        raise SystemExit(
            f"REFUSING to reuse an existing scratch directory: {path}\n"
            f"  It was not created by this run, so removing it later would not be safe.")
    path.mkdir(parents=True)
    (path / ".owner").write_text(RUN_ID + "\n", encoding="utf-8")
    print(f"    scratch dir {path.relative_to(ROOT)} (owner-verified)")
    return path


def remove_scratch(path):
    """Delete a scratch dir only when its marker proves this run created it."""
    marker = path / ".owner"
    if not path.exists():
        return "already gone"
    if not marker.exists():
        print(f"    REFUSING to remove {path}: no owner marker found")
        return "refused: no owner marker"
    if marker.read_text(encoding="utf-8").strip() != RUN_ID:
        print(f"    REFUSING to remove {path}: owner marker names a different run")
        return "refused: foreign owner"
    shutil.rmtree(path)
    return "removed"


def cleanup_owned():
    """
    Remove ONLY resources this run created and recorded, and only when the
    live ID still matches the recorded ID and still carries our owner label.

    No prune. No prefix matching. Ambiguity is a hard stop, never a guess.
    """
    if not LEDGE.exists():
        print("  no ledger - nothing to clean up")
        return 0
    data = json.loads(LEDGE.read_text(encoding="utf-8"))
    created = data.get("created", [])
    removed, refused = [], []

    for e in reversed(created):
        kind, ident = e["kind"], e["id"]
        if kind == "container":
            rc, out, _ = docker("inspect", ident, "--format",
                                f"{{{{.Id}}}}|{{{{index .Config.Labels \"{OWNER_LABEL}\"}}}}")
            if rc != 0:
                refused.append((ident, "already gone"))
                continue
            live_id, label = out.split("|", 1)
            if live_id != ident or label != RUN_ID:
                refused.append((ident, f"ownership mismatch (label={label!r})"))
                continue
            rc, out, err = docker("rm", "-f", ident)
            (removed if rc == 0 else refused).append(ident if rc == 0 else (ident, err))

        elif kind == "volume":
            rc, out, _ = docker("volume", "inspect", ident, "--format",
                                f"{{{{.Name}}}}|{{{{index .Labels \"{OWNER_LABEL}\"}}}}")
            if rc != 0:
                refused.append((ident, "already gone"))
                continue
            live_name, label = out.split("|", 1)
            if live_name != e["name"] or label != RUN_ID:
                refused.append((ident, f"ownership mismatch (label={label!r})"))
                continue
            rc, out, err = docker("volume", "rm", ident)
            (removed if rc == 0 else refused).append(ident if rc == 0 else (ident, err))

    print(f"  removed   : {len(removed)}")
    for r in removed:
        print(f"      {r}")
    if refused:
        print(f"  REFUSED   : {len(refused)} (left in place on purpose)")
        for r in refused:
            print(f"      {r}")
    return len(refused)


# ============================================================ LEG A ==========
def leg_a():
    print("\n" + "=" * 78)
    print("LEG A  -  75-submission orchestration burst (lifted code, virtual clock)")
    print("=" * 78)
    sys.path.insert(0, str(SCRIPTS))
    import simulate_grading_recovery as sim  # noqa: E402  (has __main__ guard)

    index_ts = (FUNCS / "index.ts").read_text(encoding="utf-8")
    judge0_ts = (FUNCS / "judge0.ts").read_text(encoding="utf-8")

    harness_ts = sim.build_harness_ts(index_ts, judge0_ts)
    tmp = make_scratch("leg_a")
    try:
        return _leg_a_run(tmp, sim, harness_ts)
    finally:
        print(f"    scratch {tmp.name}: {remove_scratch(tmp)}")


def _leg_a_run(tmp, sim, harness_ts):
    # Test-only export. build_harness_ts() emits `async function gradeAttempt`
    # at module top level (verified: brace depth 0 before the declaration), so
    # re-exporting it under a distinct name is valid and changes nothing about
    # the production source, which is never edited. The browser-path hang probe
    # needs this because the sweep block alone cannot reach it.
    harness_ts = harness_ts + "\nexport { gradeAttempt as __gradeAttempt };\n"
    harness_file = tmp / "harness.ts"
    harness_file.write_text(harness_ts, encoding="utf-8")
    print(f"  assembled harness: {len(harness_ts)} chars "
          f"(real code + faked network I/O + test-only __gradeAttempt export)")

    print(f"\n  real code lifted from the working tree:")
    for label in ("sweep block", "gradeAttempt()", "runHiddenTests()", "applyVerdict()",
                  "pollUntilVerdict()", "mapJudge0Result()"):
        print(f"      {label}")
    print("  faked: submitProgram(), fetchProgramResult(), admin.rpc(), the clock")

    t0 = time.monotonic()
    # The DRIVER is the entry point, exactly as simulate_grading_recovery.py
    # does at its main(). It reads this path, transpiles it, imports it, and
    # calls H.runSweepReal / H.__gradeAttempt. sim.RUNNER is NOT used: it calls
    # mod.default(), which the generated harness does not export.
    proc = subprocess.run(["node", str(DRIVER), str(harness_file)], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=600)
    wall = time.monotonic() - t0
    if proc.returncode != 0:
        check("leg A driver exited 0", False, (proc.stderr or proc.stdout)[-1500:])
        return
    data = json.loads(proc.stdout)

    print(f"\n  VIRTUAL elapsed time and REAL wall clock are reported separately:")
    print(f"      real wall clock for leg A driver : {wall:.2f}s")
    for sc in data["scenarios"]:
        print(f"      {sc['name']}")
        print(f"          virtual elapsed : {sc['totalVirtualMs'] / 1000:.1f}s model time")
        print(f"          sweeps run      : {len(sc['sweeps'])}")
        print(f"          final           : {json.dumps(sc['final']['byOutcome'])}")
        print(f"          still pending   : {sc['final']['pending']}")
        print(f"          max apply_count : {sc['final']['maxApply']}")

    b75 = data["scenarios"][0]
    rpcf = data["scenarios"][1]

    # --- acceptance criteria -------------------------------------------
    print("\n  --- acceptance criteria ---")
    total_graded = sum(s["graded"] + s["failed"] + s["deferred"] for s in b75["sweeps"])
    check("accounting is complete: graded+failed+deferred covers every attempt offered",
          total_graded == sum(s["swept"] for s in b75["sweeps"]),
          f"{total_graded} vs {sum(s['swept'] for s in b75['sweeps'])}")
    deferred_sweeps = [s["index"] for s in b75["sweeps"] if s["deferred"] > 0]
    deferred_total = sum(s["deferred"] for s in b75["sweeps"])
    check("some work was deferred at least once (budget engaged)",
          bool(deferred_sweeps),
          f"{deferred_total} attempt(s) deferred across sweep(s) {deferred_sweeps[:8]}"
          if deferred_sweeps else
          "no sweep reported deferral - the 110s budget never bind")
    check("all 75 attempts reached a terminal state",
          b75["final"]["pending"] == 0 and b75["final"]["done"] == 75,
          f"pending={b75['final']['pending']} done={b75['final']['done']}")

    # No transport/deadline outcome may masquerade as Wrong Answer. The wrong
    # count must equal exactly the deliberately-wrong submissions.
    wrong = b75["final"]["byOutcome"].get("Wrong Answer", 0)
    check("no transport/deadline outcome became Wrong Answer",
          wrong == b75["expected_wrong"],
          f"observed {wrong}, seeded genuinely-wrong {b75['expected_wrong']} "
          "(a surplus would mean a timeout was mis-recorded)")
    ce = b75["final"]["byOutcome"].get("Compilation Error", 0)
    check("compile-error count matches the seeded compile errors",
          ce == b75["expected_compile_error"], f"observed {ce}, seeded {b75['expected_compile_error']}")
    # NOT EVIDENCE of exactly-once processing. The fake's single-verdict guard
    # returns BEFORE apply_count is incremented (driver L325-337), and the
    # answer's status is already terminal by that point, so apply_count > 1 is
    # unreachable and doubleApplyDetected can never fire. This check confirms
    # only that the fake's own bookkeeping is self-consistent. Exactly-once
    # processing is demonstrated by Leg B, against real threads and the real
    # migration-20 row lock - never here.
    check("bookkeeping consistency only - NOT exactly-once evidence: "
          "apply_count never exceeded 1 (cannot fail in this fake)",
          b75["final"]["maxApply"] == 1 and b75["stats"]["doubleApplyDetected"] == 0,
          f"maxApply={b75['final']['maxApply']} "
          f"doubleApplyDetected={b75['stats']['doubleApplyDetected']} "
          "- structurally incapable of detecting a double-apply")
    check("every sweep respected its own reported budget_exhausted accounting",
          all(s["swept"] == s["graded"] + s["failed"] + s["deferred"] for s in b75["sweeps"]))

    # Failure isolation: the rejecting attempt stays pending, and the sweeps
    # that ran after the rejection still made progress on the other 74.
    #
    # An earlier version required EVERY later sweep to report graded > 0. That
    # is wrong: the rejecting attempt's answer stays 'Pending Evaluation'
    # forever, so it stays eligible forever, and once the other 74 have drained
    # the remaining sweeps legitimately admit only that one attempt, whose RPC
    # throws -> failed++, graded = 0. Progress after convergence is meaningless.
    #
    # The property is proved from PER-ATTEMPT evidence instead: which distinct
    # attempts reached a terminal state, and which one is still pending.
    # Sweep-level counters are used only for the weaker "at least one later
    # sweep made progress" fact.
    #
    # No apply_count/maxApply clause appears here on purpose: the fake bumps
    # apply_count only after its single-verdict guard passes, so the value can
    # never exceed 1 by construction. Checking it would prove nothing.
    sweeps_after = rpcf["sweeps"][1:]
    final = rpcf["final"]
    terminal_ids = set(final.get("terminalAttemptIds", []))
    pending_ids = set(final.get("pendingAttemptIds", []))
    rejected = rpcf["rejected_attempt"]
    check("one rejecting RPC did not stop the other 74",
          final["done"] == 74 and final["pending"] == 1
          and rpcf["stats"]["rpcThrows"] > 0
          and rejected not in terminal_ids
          and pending_ids == {rejected}
          and len(terminal_ids) == 74
          and len(sweeps_after) > 0
          and any(s["graded"] > 0 for s in sweeps_after),
          f"done={final['done']} pending={final['pending']} "
          f"rpcThrows={rpcf['stats']['rpcThrows']} "
          f"distinct-terminal={len(terminal_ids)} "
          f"rejected-terminal={rejected in terminal_ids} "
          f"pending-ids={sorted(pending_ids)} "
          f"graded-after={[s['graded'] for s in sweeps_after][:6]}...")
    check("the rejecting attempt was the only unresolved one",
          rpcf["final"]["pending"] == 1, f"pending={rpcf['final']['pending']}")

    print("\n  NOTE: Leg A uses a VIRTUAL clock. These durations are model time.")
    print("  They do NOT demonstrate Edge Runtime termination behaviour.")
    return data


def leg_a_hang():
    """Isolated probe: does the browser path ever regain control? Real watchdog."""
    print("\n" + "-" * 78)
    print("LEG A-HANG  -  browser path, Judge0 fetch never resolves")
    print("-" * 78)
    sys.path.insert(0, str(SCRIPTS))
    import simulate_grading_recovery as sim  # noqa: E402

    index_ts = (FUNCS / "index.ts").read_text(encoding="utf-8")
    judge0_ts = (FUNCS / "judge0.ts").read_text(encoding="utf-8")
    harness_ts = sim.build_harness_ts(index_ts, judge0_ts)
    tmp = make_scratch("hang")
    try:
        return _hang_run(tmp, harness_ts)
    finally:
        print(f"    scratch {tmp.name}: {remove_scratch(tmp)}")


def _hang_run(tmp, harness_ts):
    # Same test-only export as the burst run: the browser path lives inside
    # gradeAttempt, which the sweep block does not expose.
    harness_ts = harness_ts + "\nexport { gradeAttempt as __gradeAttempt };\n"
    hf = tmp / "harness.ts"
    hf.write_text(harness_ts, encoding="utf-8")

    watchdog = 25
    t0 = time.monotonic()
    try:
        proc = subprocess.run(["node", str(DRIVER), "--mode", "hang", str(hf)], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=watchdog)
        wall = time.monotonic() - t0
        watchdog_fired = False
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except subprocess.TimeoutExpired:
        wall = time.monotonic() - t0
        watchdog_fired = True
        payload = {}

    print(f"  external watchdog      : {watchdog}s")
    print(f"  real wall clock        : {wall:.2f}s")
    print(f"  watchdog fired         : {watchdog_fired}")
    print(f"  control returned       : {payload.get('control_returned', 'unknown')}")
    if watchdog_fired:
        print("  RESULT: the browser path did NOT regain control. The unwatchdogable")
        print("  fetch() has no AbortSignal, so no deadline can interrupt it.")
        print("  This is a MODEL finding on a VIRTUAL clock under Node - it is NOT")
        print("  proof of Edge Runtime behaviour. Leg C measures that for real.")
    check("hang probe reported an unambiguous outcome",
          watchdog_fired or payload.get("control_returned") is not None)
    return {"watchdog_fired": watchdog_fired, "wall_s": wall, **payload}


# ============================================================ LEG B ==========
def guard_dsn():
    """
    Fail closed BEFORE any connection. Parses with the psycopg driver, requires
    a numeric loopback host, a database name, and the exact opt-in.
    """
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ.get(DSN_ENV)
    ack = os.environ.get(ACK_ENV)

    def refuse(msg):
        print(f"\n  REFUSED (leg B): {msg}")
        print(f"  No connection was opened. Nothing was executed.")
        print(f"    {DSN_ENV}=postgresql://USER:PASSWORD@127.0.0.1:PORT/DISPOSABLE_DB")
        print(f"    {ACK_ENV}={ACK_VALUE}")
        raise SystemExit(2)

    if not dsn or not dsn.strip():
        refuse(f"{DSN_ENV} is unset or empty.")
    try:
        params = conninfo_to_dict(dsn)
    except Exception as exc:  # noqa: BLE001
        refuse(f"{DSN_ENV} is not a valid connection string ({type(exc).__name__}).")
    host = (params.get("host") or "").strip().strip("[]").split("%", 1)[0].lower()
    if host not in LOOPBACK_HOSTS:
        refuse(f"host {host!r} is not a loopback address.")
    if not params.get("dbname"):
        refuse("the connection string names no database.")
    if ack != ACK_VALUE:
        refuse(f"{ACK_ENV} is not set to the exact value required.")
    print(f"  leg B gate PASSED: loopback {host}, database {params['dbname']}, opt-in acknowledged")
    print("  NOTE: a loopback host does NOT prove the database is disposable; the")
    print("  opt-in above is the operator's assertion and the only basis for it.")
    return dsn


def leg_b():
    print("\n" + "=" * 78)
    print("LEG B  -  verdict row-lock under real concurrency (disposable PostgreSQL)")
    print("=" * 78)
    dsn = guard_dsn()
    import psycopg
    import test_verdict_concurrency as tvc  # noqa: E402  (has __main__ guard)

    n_attempts = 75
    attempts = [f"3333{i:04d}-3333-3333-3333-333333333333" for i in range(n_attempts)]
    answers = [f"4444{i:04d}-4444-4444-4444-444444444444" for i in range(n_attempts)]

    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(tvc.SCHEMA_SQL)
            cur.execute(tvc.ACL_SQL)
            cur.execute(tvc.verdict_body("migration_20_verdict_row_lock.sql"))
            cur.execute("SELECT public.quizeazz_apply_judge0_verdict FROM pg_proc LIMIT 0")
        tvc.ensure_roles(psycopg, dsn)
        # Seed 75 pending answers.
        with conn.cursor() as cur:
            for aid, ans in zip(attempts, answers):
                cur.execute("INSERT INTO public.exams (id) VALUES (%s)", (aid,))
                cur.execute("INSERT INTO public.exam_attempts (id, exam_id, status) VALUES (%s,%s,'submitted')",
                            (aid, aid))
                cur.execute("INSERT INTO public.questions (id, question_type) VALUES (%s,'coding')", (aid,))
                cur.execute("INSERT INTO public.answers (id, attempt_id, question_id, evaluation_status, "
                            "marks_total) VALUES (%s,%s,%s,'Pending Evaluation',10)", (ans, aid, aid))
    finally:
        conn.close()

    # Fire CONCURRENT applications: two threads per answer, opposing outcomes.
    apply_sql = """
        SELECT public.quizeazz_apply_judge0_verdict(
          %s::uuid, %s::uuid, NULL, %s, 3, 0, 1000, 0.01, 'out', '', '', 'burst');
    """
    barrier = threading.Barrier(2 * n_attempts)
    outcomes = []
    lock = threading.Lock()

    def worker(attempt, answer, outcome):
        c = psycopg.connect(dsn)
        try:
            barrier.wait(timeout=60)
            with c.cursor() as cur:
                cur.execute(apply_sql, (attempt, answer, outcome))
                row = cur.fetchone()
                c.commit()
            with lock:
                outcomes.append((answer, outcome, "ok", row[0]))
        except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
            with lock:
                outcomes.append((answer, outcome, "raised", type(exc).__name__))
            try:
                c.rollback()
            except Exception:
                pass
        finally:
            c.close()

    threads = []
    for aid, ans in zip(attempts, answers):
        threads.append(threading.Thread(target=worker, args=(aid, ans, "Correct")))
        threads.append(threading.Thread(target=worker, args=(aid, ans, "Wrong Answer")))
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    wall = time.monotonic() - t0

    ok_calls = [o for o in outcomes if o[2] == "ok"]
    raised = [o for o in outcomes if o[2] == "raised"]

    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.answers "
                        "WHERE evaluation_status = 'Pending Evaluation'")
            still_pending = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM public.answers")
            total_answers = cur.fetchone()[0]
    finally:
        conn.close()

    print(f"  real wall clock for the concurrent phase : {wall:.2f}s")
    print(f"  apply calls issued    : {2 * n_attempts}")
    print(f"  succeeded             : {len(ok_calls)}")
    print(f"  refused (row lock)    : {len(raised)}")

    print("\n  --- acceptance criteria ---")
    check("every answer received exactly ONE verdict",
          still_pending == 0,
          f"{total_answers - still_pending}/{total_answers} verdicts, {still_pending} still pending")
    check("the losing racer was refused, never silently overwrote",
          len(raised) == n_attempts,
          f"expected {n_attempts} refusals, saw {len(raised)}")
    check("no two successful applications targeted the same answer",
          len({o[0] for o in ok_calls}) == len(ok_calls),
          f"{len(ok_calls)} successes over {len({o[0] for o in ok_calls})} distinct answers")
    # Replaces a vacuous assertion. The old condition
    #   all(len({o[1] for o in outcomes if o[0] == a}) >= 1 ...)
    # could only be false if an answer produced NO outcome at all, and its label
    # claimed the opposite of what this harness does by design: two racers with
    # DIFFERENT outcomes are launched at every answer on purpose.
    #
    # The invariant the data model supports is a per-answer partition: exactly
    # one racer committed and exactly one was refused. It fails if the row lock is
    # absent (both commit), if neither commits, and if both are refused.
    per_answer = {}
    for o in outcomes:
        per_answer.setdefault(o[0], []).append(o)
    committed_each = [a for a, v in per_answer.items()
                      if sum(1 for o in v if o[2] == "ok") == 1]
    refused_each = [a for a, v in per_answer.items()
                    if sum(1 for o in v if o[2] == "raised") == 1]
    check("every answer had exactly one commit and exactly one refusal",
          len(per_answer) == n_attempts
          and len(committed_each) == n_attempts
          and len(refused_each) == n_attempts,
          f"{len(per_answer)} answers seen; {len(committed_each)} with exactly one"
          f" commit; {len(refused_each)} with exactly one refusal; {len(outcomes)} calls")
    print("\n  NOTE: this is the only leg that exercises the REAL migration 20 body")
    print("  against a REAL PostgreSQL. Leg A's JS guard is a model, not proof.")


# ============================================================ LEG C ==========
def leg_c_preflight():
    """
    Verify, without assuming, that a safe local route to the fake exists.
    Returns (ok, blocker_reason). Aborts rather than improvising.
    """
    rc, out, err = docker("images", "public.ecr.aws/supabase/edge-runtime:v1.77.4",
                          "--format", "{{.ID}}")
    if rc != 0 or not out:
        return False, "the cached edge-runtime image is not present; pulling is not authorised"
    # The route: --add-host=host.docker.internal:host-gateway. This does NOT
    # rely on container loopback reaching the host (it cannot) and does NOT
    # require joining or modifying any shared Docker network.
    return True, "image present; route will be --add-host=host.docker.internal:host-gateway"


def leg_c():
    print("\n" + "=" * 78)
    print("LEG C  -  real Edge Runtime, real wall clock (cached image only)")
    print("=" * 78)

    class Blocker(Exception):
        pass

    ok, reason = leg_c_preflight()
    if not ok:
        print(f"\n  BLOCKER: {reason}")
        print("  Leg C is NOT EXECUTED. Nothing was started, nothing pulled.")
        return
    print(f"  preflight: {reason}")

    edge_port = free_port()
    cname = f"{RUN_ID}-edge"
    vname = f"{RUN_ID}-j0cfg"
    fake_port = free_port()
    fake_proc = None
    container_started = False

    try:
        # --- 1. discover the entrypoint; do NOT guess it ---------------
        rc, out, err = docker("run", "--rm", "--entrypoint", "sh",
                              "public.ecr.aws/supabase/edge-runtime:v1.77.4",
                              "-c", "ls /home/deno/bin 2>/dev/null; echo '--'; command -v edge-runtime")
        print(f"\n  entrypoint discovery (exit {rc}):")
        for line in out.splitlines():
            print(f"      {line}")
        if rc != 0 or not any("edge-runtime" in l for l in out.splitlines()):
            raise Blocker(
                "could not discover a usable entrypoint inside the cached image; "
                "guessing an invocation is not acceptable")

        # --- 2. start the fake Judge0 on host loopback ------------------
        fake_proc = subprocess.Popen(
            ["node", str(FAKE_J0), "--mode", "normal", "--latency", "200",
             "--port", str(fake_port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready = json.loads(fake_proc.stdout.readline())
        print(f"\n  fake Judge0 : {ready['host']}:{ready['port']} mode={ready['mode']}")
        print(f"  binding is loopback-only by construction (the server refuses any other interface)")

        # --- 3. start the Edge container with an explicit host route ----
        print(f"\n  starting Edge Runtime container {cname}")
        print(f"    published  : 127.0.0.1:{edge_port}  (loopback only)")
        print(f"    source mount: {FUNCS} -> /home/deno/functions/main  (READ-ONLY)")
        print(f"    judge0 route: http://host.docker.internal:{fake_port}")
        print("    host route is added explicitly with --add-host=...:host-gateway;")
        print("    no shared network is joined, created or modified.")

        rc, out, err = docker(
            "run", "-d", "--name", cname,
            "--add-host", "host.docker.internal:host-gateway",
            "-p", f"127.0.0.1:{edge_port}:9000",
            "-v", f"{FUNCS}:/home/deno/functions/main:ro",
            "-e", f"JUDGE0_API_URL=http://host.docker.internal:{fake_port}",
            "-e", "JUDGE0_API_TOKEN=local-fake-token",
            "--label", f"{OWNER_LABEL}={RUN_ID}",
            "public.ecr.aws/supabase/edge-runtime:v1.77.4",
        )
        if rc != 0:
            raise Blocker(f"container did not start: {err[:300]}")
        container_started = True
        cid = out.strip()
        record("container", cid, cname, edge_port)
        print(f"    container id {cid}")

        # --- 4. VERIFY the route from inside the container --------------
        # Nothing is trusted until the container itself has fetched the fake
        # and returned our own marker string.
        print("\n  verifying the container -> fake route (from inside the container):")
        rc, out, err = docker(
            "exec", cname, "sh", "-c",
            f"curl -s --max-time 10 http://host.docker.internal:{fake_port}/__health || echo ROUTE_FAILED",
        )
        print(f"      reply: {out.strip()[:160]}")
        if "ROUTE_FAILED" in out or '"ok":true' not in out.replace(" ", ""):
            raise Blocker(
                "the container could not reach the host fake Judge0 on the "
                "documented route; no safe local route is available without "
                "pulling images or changing shared Docker networking")

        # --- 5. normal-latency run --------------------------------------
        print("\n  running the normal-latency case:")
        rc, out, err = docker("exec", cname, "sh", "-c",
                              "curl -s --max-time 60 -X POST http://127.0.0.1:9000/ "
                              "-H 'content-type: application/json' "
                              "-H 'Authorization: Bearer x' -d '{}' || echo REQUEST_FAILED")
        print(f"      {out.strip()[:300]}")
        print(f"      NOTE: index.ts requires a real attempt_id and a valid Supabase")
        print(f"      JWT, which this harness cannot supply without a project. The")
        print(f"      run above establishes reachability and auth rejection only.")

    except Blocker as b:
        print(f"\n  BLOCKER: {b}")
        print("  Leg C is NOT EXECUTED past this point. No workaround was improvised.")
    finally:
        if fake_proc is not None:
            fake_proc.terminate()
            try:
                fake_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                fake_proc.kill()
        if container_started:
            print(f"\n  container {cname} recorded in the ledger; "
                  f"run cleanup to remove it by ID.")


# ================================================================= MAIN ======
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leg", choices=["a", "b", "c", "all"], default="a")
    ap.add_argument("--inventory", action="store_true")
    ap.add_argument("--no-inventory", action="store_true",
                    help="leg A only: run without invoking the Docker inventory")
    args = ap.parse_args()

    # Fail closed on --no-inventory before any leg runs, any resource is
    # created, or any inventory is taken. Legs B and C own containers,
    # volumes and images; the before/after inventory diff is how that
    # ownership is audited, so the flag is refused for them rather than
    # silently returning an empty diff.
    if args.no_inventory:
        if args.inventory:
            print("\n  REFUSED: --inventory and --no-inventory contradict each other.")
            return 2
        if args.leg != "a":
            print(f"\n  REFUSED: --no-inventory requires --leg a; got --leg {args.leg}.")
            print("  Legs B and C create Docker resources and need the inventory diff.")
            return 2

    print("=" * 78)
    print(f"QuizEazz - 75-submission burst validation   run id {RUN_ID}")
    print("=" * 78)
    print("  synthetic Judge0 responses only - no C++ is compiled, no program runs")
    print("  75 = 75 synthetic ATTEMPTS, NOT 75 simultaneous browser clients")

    if args.inventory:
        print(json.dumps(inventory(), indent=2))
        return 0

    if args.no_inventory:
        print("\n  --no-inventory: the Docker inventory will NOT be taken.")
        print("  Leg A creates no container, volume or image. Its scratch directory is")
        print("  removed by owner-verified cleanup in a finally block, which never calls Docker.")
        # Fail closed if a ledger for this run id already exists: cleanup_owned()
        # removes whatever the ledger names, so running without an inventory
        # would leave any such removal with no before/after record.
        if LEDGE.exists():
            print(f"\n  REFUSED: an ownership ledger already exists for run id {RUN_ID}")
            print(f"  at {LEDGE}. Execution without inventory is refused, because")
            print("  cleanup acts on whatever the ledger names.")
            return 2
        pre = {}
    else:
        print(f"\n  pre-inventory (recorded for the post-run diff):")
        pre = inventory()
        print(f"    containers={len(pre['containers'])} volumes={len(pre['volumes'])} "
              f"networks={len(pre['networks'])} images={len(pre['images'])}")

    try:
        if args.leg in ("a", "all"):
            leg_a()
            leg_a_hang()
        if args.leg in ("b", "all"):
            # Disabled by default. Leg B creates its schema, 75 attempts, seed
            # rows and three cluster roles in the DSN's own database, and removes
            # none of them. Adding cleanup risks deleting objects this run did not
            # create, so the leg is gated rather than cleaned.
            if os.environ.get(LEG_B_ACK_ENV) != LEG_B_ACK_VALUE:
                print("\n  REFUSED: Leg B is disabled unless you acknowledge that it")
                print("  writes tables, rows and cluster roles into the DSN's OWN")
                print("  database and performs NO cleanup of them.")
                print(f"    {LEG_B_ACK_ENV}={LEG_B_ACK_VALUE}")
                return 2
            leg_b()
        if args.leg in ("c", "all"):
            leg_c()
    finally:
        print("\n" + "=" * 78)
        print("post-inventory and cleanup")
        print("=" * 78)
        if pre:
            post = inventory()
            for k in pre:
                added = [x for x in post[k] if x not in pre[k]]
                print(f"    {k}: +{len(added)} {added if added else ''}")
        else:
            print("  inventory skipped by --no-inventory (leg A creates no Docker resource)")
        refused = cleanup_owned()
        print(f"\n  summary: {results.count(True)} assertions passed, "
              f"{results.count(False)} failed, {refused} cleanup refusals")
        print("  HARNESS DEFINITION REMINDER: this does NOT validate C++ compilation,")
        print("  Judge0 correctness, or 75 concurrent authenticated browser clients.")

    return 1 if results.count(False) else 0


if __name__ == "__main__":
    sys.exit(main())