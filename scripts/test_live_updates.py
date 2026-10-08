"""
Tests for live updates: Realtime wiring for the faculty security log and
candidate-side exam-availability refresh, plus the destructive
teacher_clear_security_events() RPC contract.

No database, no network. These assert the *shape* of the implementation and
EXECUTE the boundary-calculation helper with node so the time logic is really
verified rather than pattern-matched.

Run:  python scripts/test_live_updates.py
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SQL = ROOT / "supabase"

LIVE_UPDATES = SRC / "lib" / "liveUpdates.ts"
EXAM_SCHEDULE = SRC / "lib" / "examSchedule.ts"
STUDENT_EXAMS = SRC / "components" / "student" / "StudentExamsPage.tsx"
ANALYTICS = SRC / "components" / "teacher" / "TeacherAnalyticsView.tsx"
TEACHER_API = SRC / "lib" / "teacherApi.ts"
MIG18 = SQL / "migration_18_realtime_security_log.sql"
MIG14 = SQL / "migration_14_faculty_clear_security_log.sql"
MIG19 = SQL / "migration_19_clear_security_events_explicit_where.sql"

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append((name, detail))


def _src(rel):
    return (SRC / rel).read_text(encoding="utf-8")


def _run_node(source):
    """Execute a TypeScript snippet with node; node 24 strips types natively."""
    node = shutil.which("node")
    if node is None:
        return False, "", "node not found on PATH"
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "snippet.ts"
        script.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run([node, str(script)], capture_output=True, timeout=90)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, "", str(exc)
    return (
        proc.returncode == 0,
        proc.stdout.decode("utf-8", errors="replace").strip(),
        proc.stderr.decode("utf-8", errors="replace").strip(),
    )


# ---------------------------------------------------------------------------
# 1. Clear security log: the destructive RPC contract
# ---------------------------------------------------------------------------
def test_clear_log_explicit_where():
    """
    Regression for the production failure:

        POST /rest/v1/rpc/teacher_clear_security_events
        {"code":"21000","message":"DELETE requires a WHERE clause"}

    Migration 14 deleted with no predicate. That statement is valid SQL, but
    the RPC request path rejected it, so faculty could never clear the audit
    log. Migration 19 writes the identical deletion as `WHERE true` — a
    tautology, so the affected rows are unchanged (every row still goes) — and
    it is only the explicitness the path requires.
    """
    mig19 = MIG19.read_text(encoding="utf-8")
    # Comment lines are documentation and legitimately mention words like
    # "policy" and "grant" when describing what was NOT changed. Structural
    # checks therefore run against the executable SQL only.
    sql = "\n".join(
        line for line in mig19.splitlines() if not line.lstrip().startswith("--")
    )

    check(
        "the DELETE now carries an explicit predicate",
        re.search(r"DELETE\s+FROM\s+public\.security_events\s+WHERE\s+true", sql, re.I)
        is not None,
        "the RPC path rejects a DELETE with no WHERE clause",
    )
    check(
        "no unpredicated DELETE on security_events remains",
        len(re.findall(r"DELETE\s+FROM\s+public\.security_events", sql, re.I))
        == len(
            re.findall(r"DELETE\s+FROM\s+public\.security_events\s+WHERE\s+true", sql, re.I)
        ),
    )
    check(
        "the deletion is still table-wide (WHERE true, not a narrower filter)",
        "WHERE exam_id" not in mig19 and "WHERE id" not in mig19,
        "clear must still remove every audit record, not a subset",
    )
    check(
        "the RPC keeps its name and takes no parameters",
        re.search(
            r"CREATE OR REPLACE FUNCTION public\.teacher_clear_security_events\(\s*\)",
            mig19,
        )
        is not None,
        "a parameterised clear could become an arbitrary-row delete",
    )
    check(
        "the RPC remains SECURITY DEFINER",
        "SECURITY DEFINER" in mig19,
    )
    check(
        "the faculty guard still runs before the deletion",
        sql.index("quizeazz_assert_faculty") < sql.index("DELETE FROM"),
    )
    check(
        "search_path stays pinned",
        "SET search_path = public, pg_temp" in sql,
    )
    check(
        "the return shape is unchanged",
        "'cleared', TRUE" in sql and "'removed', v_removed" in sql,
    )
    check(
        "migration 19 touches no table, RLS policy, grant or data",
        re.search(
            r"\bCREATE\s+TABLE\b|\bALTER\s+TABLE\b|\bDROP\s+TABLE\b|\bCREATE\s+POLICY\b"
            r"|\bGRANT\b|\bREVOKE\b|\bINSERT\s+INTO\b|\bUPDATE\s+public\b",
            sql,
            re.I,
        )
        is None,
        "the fix must be limited to the DELETE statement itself",
    )
    check(
        "migration 19 is idempotent",
        "CREATE OR REPLACE" in mig19,
    )
    check(
        "no client-side table deletion is introduced",
        ".delete()" not in (SRC / "lib" / "teacherApi.ts").read_text(encoding="utf-8"),
    )


def test_clear_security_log_contract():
    """teacher_clear_security_events() must stay faculty-only and destructive.

    Regression guard for the reported "DELETE requires a WHERE clause" symptom.
    The unfiltered DELETE is deliberate and correct: it is the whole point of
    "clear everything". What must never change is that the statement lives
    inside a SECURITY DEFINER function behind a faculty guard, and that no
    client-supplied WHERE clause can ever reach it.
    """
    mig14 = MIG14.read_text(encoding="utf-8")

    check(
        "the clear RPC exists and is SECURITY DEFINER",
        "CREATE OR REPLACE FUNCTION public.teacher_clear_security_events()" in mig14
        and "SECURITY DEFINER" in mig14,
    )
    check(
        "the delete is scoped to the security_events table only",
        "DELETE FROM public.security_events" in mig14,
    )
    check(
        "no other table is deleted by the clear RPC",
        len(re.findall(r"DELETE\s+FROM", mig14, re.I)) == 1,
        "a destructive RPC must touch exactly one table",
    )
    check(
        "the clear RPC takes NO client-supplied parameters",
        re.search(
            r"CREATE OR REPLACE FUNCTION public\.teacher_clear_security_events\(\s*\)",
            mig14,
        )
        is not None,
        "a parameterised clear could become an arbitrary-row delete",
    )
    check(
        "no WHERE clause is accepted from the caller",
        "p_where" not in mig14 and "p_filter" not in mig14,
    )
    check(
        "the faculty guard runs before the delete",
        mig14.index("quizeazz_assert_faculty") < mig14.index("DELETE FROM"),
    )
    check(
        "anon cannot execute the clear RPC",
        "REVOKE ALL ON FUNCTION public.teacher_clear_security_events() FROM anon;" in mig14,
    )
    check(
        "PUBLIC cannot execute the clear RPC",
        "REVOKE ALL ON FUNCTION public.teacher_clear_security_events() FROM PUBLIC;" in mig14,
    )
    check(
        "the RPC returns a clear success payload",
        "'cleared', TRUE" in mig14 and "'removed', v_removed" in mig14,
    )
    check(
        "search_path is pinned on the clear RPC",
        "SET search_path = public, pg_temp" in mig14,
    )


def test_clear_log_frontend_stays_on_the_rpc():
    """The browser must call the faculty RPC, never a direct table delete."""
    api = _src("lib/teacherApi.ts")
    view = _src("components/teacher/TeacherAnalyticsView.tsx")

    check(
        "clearSecurityLog calls the faculty RPC",
        "supabase.rpc('teacher_clear_security_events')" in api,
    )
    check(
        "the browser never issues a direct DELETE on a table",
        ".delete()" not in api,
        "a client-side delete would bypass the faculty guard entirely",
    )
    check(
        "the confirmation UI is preserved",
        "Clear All Records" in view and "This cannot be undone" in view,
    )
    check(
        "clearing the log refetches rather than splicing local state",
        "reload()" in view,
    )


# ---------------------------------------------------------------------------
# 2. Realtime security log
# ---------------------------------------------------------------------------
def test_security_log_realtime():
    """security_events must be published, and both event directions observed."""
    check(
        "the realtime module exists",
        LIVE_UPDATES.exists(),
    )
    lu = LIVE_UPDATES.read_text(encoding="utf-8")
    view = _src("components/teacher/TeacherAnalyticsView.tsx")

    check(
        "INSERT events are observed",
        re.search(r"event:\s*'INSERT'.*security_events", lu, re.S) is not None,
    )
    check(
        "DELETE events are observed so Clear Log empties the view",
        re.search(r"event:\s*'DELETE'.*security_events", lu, re.S) is not None,
    )
    check(
        "the security log subscribes on mount",
        "subscribeToSecurityEvents(" in view,
    )
    check(
        "the subscription is torn down on unmount",
        re.search(
            r"useEffect\(\(\)\s*=>\s*subscribeToSecurityEvents\(", view
        )
        is not None,
        "an effect that returns the unsubscribe function cleans up on unmount",
    )
    check(
        "a refetch is used instead of appending the realtime payload",
        re.search(r"postgres_changes[\s\S]{0,400}?\(\)\s*=>\s*onChanged\(\)", lu) is not None,
    )
    check(
        "no duplicate rows: the payload is never pushed into local state",
        "setEvents(" not in lu,
        "refetching keeps one source of truth so an event cannot be applied twice",
    )

    mig18 = MIG18.read_text(encoding="utf-8")
    check(
        "migration 18 publishes security_events",
        "ALTER PUBLICATION supabase_realtime ADD TABLE public.security_events" in mig18,
    )
    check(
        "migration 18 is idempotent",
        "IF NOT EXISTS" in mig18,
    )
    check(
        "migration 18 does NOT publish exams or exam_assignments",
        "ADD TABLE public.exams" not in mig18
        and "ADD TABLE public.exam_assignments" not in mig18,
        "publishing those would require granting candidates direct table reads",
    )
    check(
        "migration 18 adds no RLS policy",
        re.search(r"\bCREATE\s+POLICY\b|\bALTER\s+TABLE\b|\bGRANT\b|\bREVOKE\b", mig18, re.I)
        is None,
        "publication membership must not change the security surface",
    )
    check(
        "migration 18 creates no function",
        "CREATE FUNCTION" not in mig18.upper().replace("CREATE OR REPLACE FUNCTION", ""),
    )


# ---------------------------------------------------------------------------
# 3. Candidate-side exam availability
# ---------------------------------------------------------------------------
def test_exam_availability_refresh():
    lu = LIVE_UPDATES.read_text(encoding="utf-8")
    page = _src("components/student/StudentExamsPage.tsx")
    api = _src("lib/teacherApi.ts")

    check(
        "the candidate list subscribes to availability changes",
        "subscribeToExamAvailability(" in page,
    )
    check(
        "the candidate subscription is torn down on unmount",
        re.search(r"useEffect\(\(\)\s*=>\s*subscribeToExamAvailability\(", page) is not None,
    )
    check(
        "an availability signal triggers the existing RPC refetch",
        "subscribeToExamAvailability(reload)" in page,
        "data must keep coming from get_student_exam_overview(), never a table read",
    )
    check(
        "a precise boundary timer refetches at starts_at/ends_at",
        "msUntilNextBoundary" in page and "setTimeout" in page,
        "realtime cannot observe a wall-clock instant, so a local timer is required",
    )
    check(
        "the boundary timer is cleared on unmount/change",
        "clearTimeout" in page,
    )
    check(
        "the boundary timer clamps setTimeout overflow",
        "2_147_000_000" in page,
    )
    check(
        "no interval-based server polling was introduced",
        "setInterval" in page and "fetchMyExams" not in page.split("setInterval")[1][:200],
        "the pre-existing 30s interval only re-renders from local state",
    )

    # Every teacher action that can change availability must notify.
    for fn in (
        "setExamStatus",
        "setExamSchedule",
        "setExamAssignments",
        "publishExamNow",
    ):
        body = re.search(
            rf"export async function {fn}\((.*?)\n\}}", api, re.S
        )
        check(
            f"{fn} notifies open candidate tabs",
            body is not None and "notifyExamAvailabilityChanged()" in body.group(1),
        )

    check(
        "the broadcast payload carries no table data",
        re.search(r"payload:\s*\{\s*\}", lu) is not None,
        "a change signal must never leak row contents",
    )


def test_ms_until_next_boundary_behaviour():
    """Execute the real helper so the boundary maths is actually verified."""
    body = r"""
import { msUntilNextBoundary } from 'file:///PLACEHOLDER';

const now = Date.parse('2026-10-09T10:00:00Z');
const iso = (s: string) => new Date(Date.parse('2026-10-09T10:00:00Z') + s * 1000).toISOString();

const cases = {
  // Nearest upcoming start wins over a later one.
  nearest: msUntilNextBoundary(
    [{ starts_at: iso(600), ends_at: null }, { starts_at: iso(60), ends_at: null }],
    now,
  ),
  // An end boundary in the future is also a boundary.
  ends_at: msUntilNextBoundary([{ starts_at: null, ends_at: iso(120) }], now),
  // Boundaries already in the past must not produce a 0/negative loop.
  all_past: msUntilNextBoundary([{ starts_at: iso(-10), ends_at: iso(-5) }], now),
  // No schedule at all.
  none: msUntilNextBoundary([{ starts_at: null, ends_at: null }], now),
  empty: msUntilNextBoundary([], now),
  // Exactly-now is NOT upcoming, so it is ignored rather than firing instantly.
  exactly_now: msUntilNextBoundary([{ starts_at: iso(0), ends_at: null }], now),
};
console.log(JSON.stringify(cases));
"""
    module_uri = EXAM_SCHEDULE.resolve().as_uri()
    ok, out, err = _run_node(body.replace("file:///PLACEHOLDER", module_uri))
    check(
        "msUntilNextBoundary executes against real timestamps",
        ok,
        err[:300],
    )
    if not ok:
        return
    try:
        got = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        check("boundary results are parseable", False, out[:200])
        return

    check("the nearest upcoming start is chosen", got.get("nearest") == 60_000, str(got.get("nearest")))
    check("an upcoming ends_at is a boundary too", got.get("ends_at") == 120_000, str(got.get("ends_at")))
    check("past boundaries yield null (no tight loop)", got.get("all_past") is None, str(got.get("all_past")))
    check("an unscheduled exam yields null", got.get("none") is None, str(got.get("none")))
    check("an empty list yields null", got.get("empty") is None, str(got.get("empty")))
    check(
        "a boundary exactly at now is not treated as upcoming",
        got.get("exactly_now") is None,
        str(got.get("exactly_now")),
    )


def main():
    for fn in (
        test_clear_log_explicit_where,
        test_clear_security_log_contract,
        test_clear_log_frontend_stays_on_the_rpc,
        test_security_log_realtime,
        test_exam_availability_refresh,
        test_ms_until_next_boundary_behaviour,
    ):
        fn()

    print("=" * 74)
    print("QuizEazz  ·  live updates and security-log tests")
    print("=" * 74)
    for name, detail in PASS:
        print(f"  PASS  {name}")
    if FAIL:
        print()
        for name, detail in FAIL:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())