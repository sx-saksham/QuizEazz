"""
Is there a genuinely isolated, already-existing fixture for an end-to-end
grading smoke test? READ-ONLY, counts only - no identifying data printed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_rpc_contract import _run_linked_select  # noqa: E402

QUERIES = [
    ("profiles by role (counts only)",
     "SELECT role, count(*) AS n FROM public.profiles GROUP BY role ORDER BY role"),
    ("attempts by status (counts only)",
     "SELECT status, count(*) AS n FROM public.exam_attempts GROUP BY status ORDER BY status"),
    ("attempts holding a coding answer",
     "SELECT count(DISTINCT a.attempt_id) AS n FROM public.answers a "
     "JOIN public.questions q ON q.id=a.question_id WHERE q.question_type='coding'"),
    ("attempts that are BOTH finalized AND still ungraded",
     "SELECT count(DISTINCT a.attempt_id) AS n FROM public.answers a "
     "JOIN public.questions q ON q.id=a.question_id "
     "JOIN public.exam_attempts att ON att.id=a.attempt_id "
     "WHERE q.question_type='coding' AND a.evaluation_status='Pending Evaluation' "
     "AND att.status IN ('submitted','timed_out','evaluating','evaluated')"),
    ("exams in the system (counts only)", "SELECT count(*) AS n FROM public.exams"),
]

for label, sql in QUERIES:
    rows, err = _run_linked_select(sql)
    print(f"=== {label} ===")
    if err or not rows:
        print(f"  unavailable: {err}")
    else:
        for r in rows:
            print("  " + ", ".join(f"{k}={v}" for k, v in r.items()))
    print()

rows, err = _run_linked_select(
    "SELECT count(*) AS n FROM public.profiles "
    "WHERE role = 'student' AND ("
    "  coalesce(full_name,'') ILIKE '%test%' OR coalesce(full_name,'') ILIKE '%quiz%'"
    "  OR coalesce(full_name,'') ILIKE '%load%')")
print("=== clearly-synthetic student profiles that could be a fixture ===")
if err or not rows:
    print(f"  unavailable: {err}")
else:
    print(f"  count: {rows[0]['n']}")
    if rows[0]["n"] == 0:
        print("  -> none exist. Any existing account belongs to a real user, and")
        print("     grading one would modify real student work - which is forbidden.")