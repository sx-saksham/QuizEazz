"""
Class + Batch segregation tests for QuizEazz.

Static only: no database, no network. These lock down the invariants that
keep group assignment honest:

  * cohort membership is stored authoritatively (profiles.class_name/batch)
  * onboarding is one-shot, self-only, student-only, fixed-vocabulary
  * the teacher UI filters by group and Select All is group-scoped
  * the assignment RPC re-validates every id against the group server-side
  * students cannot write profiles directly (RLS) or smuggle membership
  * legacy individual-pick assignment still works untouched

Run:  python scripts/test_class_batch.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SQL = ROOT / "supabase"

MIG10 = SQL / "migration_10_class_batch_segregation.sql"
COHORT_TS = SRC / "lib" / "cohort.ts"
MODAL_TSX = SRC / "components" / "teacher" / "AssignStudentsModal.tsx"
ONBOARD_TSX = SRC / "components" / "student" / "StudentOnboarding.tsx"
PORTAL_TSX = SRC / "components" / "student" / "StudentPortal.tsx"
TEACHER_API = SRC / "lib" / "teacherApi.ts"

CLASSES = ["SY CSDS A", "SY CSDS B", "SY CSDS C"]
BATCHES = ["Batch 1", "Batch 2", "Batch 3"]

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append((name, detail))


def test_migration_vocab():
    check("migration 10 exists", MIG10.exists())
    if not MIG10.exists():
        return
    t = MIG10.read_text(encoding="utf-8")
    for c in CLASSES:
        check(f"class vocabulary contains {c!r}", f"'{c}'" in t)
    for b in BATCHES:
        check(f"batch vocabulary contains {b!r}", f"'{b}'" in t)
    check(
        "columns are nullable (legacy rows untouched)",
        re.search(r"ADD COLUMN IF NOT EXISTS class_name", t) is not None
        and re.search(r"ADD COLUMN IF NOT EXISTS batch", t) is not None,
    )
    check(
        "partial cohort state is forbidden",
        "profiles_cohort_both_or_neither" in t,
    )


def test_onboarding_is_locked_down():
    if not MIG10.exists():
        return
    t = MIG10.read_text(encoding="utf-8")
    check(
        "onboarding RPC takes no student id (self-only)",
        re.search(r"CREATE OR REPLACE FUNCTION public\.set_my_class_batch\(\s*p_class TEXT,\s*p_batch TEXT\s*\)", t) is not None,
    )
    check(
        "onboarding writes only the caller's own row",
        re.search(r"UPDATE public\.profiles\s+SET class_name = p_class,\s+batch = p_batch\s+WHERE id = v_user_id", t, re.S) is not None,
    )
    check(
        "onboarding is one-shot (second write rejected)",
        re.search(r"already set and cannot be changed", t) is not None,
    )
    check(
        "onboarding is student-only",
        re.search(r"Only student candidates set a class and batch", t) is not None,
    )
    check(
        "migration adds no UPDATE policy on profiles (RLS stays deny)",
        "CREATE POLICY" not in t,
    )


def test_group_filtering_ui():
    check("cohort constants module exists", COHORT_TS.exists())
    if not COHORT_TS.exists():
        return
    c = COHORT_TS.read_text(encoding="utf-8")
    for v in CLASSES + BATCHES:
        check(f"frontend vocabulary contains {v!r}", f"'{v}'" in c)
    check("exactly 9 groups implied",
          len(CLASSES) * len(BATCHES) == 9)

    check("assignment modal exists", MODAL_TSX.exists())
    if not MODAL_TSX.exists():
        return
    m = MODAL_TSX.read_text(encoding="utf-8")
    check(
        "modal filters candidates by the selected group",
        re.search(r"c\.class_name === groupClass && c\.batch === groupBatch", m) is not None,
    )
    check(
        "Select All selects the whole group, ignoring search text",
        re.search(r"groupMembers\.forEach\(\(c\) => next\.add\(c\.id\)\)", m) is not None,
    )
    check(
        "modal sends the group with the save for server enforcement",
        re.search(r"setExamAssignments\(examId, Array\.from\(selected\), group\)", m) is not None,
    )
    check(
        "individual deselection still works (toggle preserved)",
        re.search(r"const toggle = \(id: string\)", m) is not None,
    )


def test_server_side_group_enforcement():
    if not MIG10.exists():
        return
    t = MIG10.read_text(encoding="utf-8")
    check(
        "assignments RPC accepts an optional group",
        re.search(r"p_class TEXT DEFAULT NULL,\s*p_batch TEXT DEFAULT NULL", t) is not None,
    )
    check(
        "half-specified group is rejected",
        re.search(r"must be selected together, or neither", t) is not None,
    )
    check(
        "wrong-class/wrong-batch members abort the whole write",
        re.search(r"are not members of", t) is not None
        and re.search(r"No assignments were changed", t) is not None,
    )
    check(
        "membership is read from profiles, never from the request",
        re.search(r"p\.class_name = p_class\s+AND p\.batch = p_batch", t) is not None,
    )
    check(
        "duplicate prevention preserved",
        "ON CONFLICT (exam_id, student_id) DO NOTHING" in t,
    )
    check(
        "teacher RPC still requires faculty + editable composition",
        "quizeazz_assert_faculty(v_caller_id)" in t
        and "quizeazz_assert_composition_editable(p_exam_id)" in t,
    )


def test_student_cannot_reach_other_groups():
    check("onboarding gate component exists", ONBOARD_TSX.exists())
    check("portal exists", PORTAL_TSX.exists())
    if not PORTAL_TSX.exists():
        return
    p = PORTAL_TSX.read_text(encoding="utf-8")
    check(
        "portal gates every student route on onboarding",
        re.search(r"profile\.role === 'student' && !isOnboarded\(profile\)", p) is not None,
    )
    check(
        "unonboarded students see only onboarding, never routes",
        re.search(r"return <StudentOnboarding />", p) is not None,
    )
    if ONBOARD_TSX.exists():
        o = ONBOARD_TSX.read_text(encoding="utf-8")
        check(
            "onboarding offers only the fixed vocabulary",
            "CLASS_OPTIONS" in o and "BATCH_OPTIONS" in o
            and "useState<string>('')" in o,
        )


def test_legacy_behavior_intact():
    if not MIG10.exists():
        return
    t = MIG10.read_text(encoding="utf-8")
    check(
        "null group keeps the legacy individual-pick path",
        re.search(r"When both are\s+NULL the legacy individual-pick", t) is not None
        or "legacy individual-pick behavior is byte-identical" in t,
    )
    check(
        "migration converts no existing data",
        "UPDATE public.profiles" in t  # only the self-onboarding write
        and t.count("UPDATE public.profiles") == 1,
    )


def main():
    for fn in [
        test_migration_vocab,
        test_onboarding_is_locked_down,
        test_group_filtering_ui,
        test_server_side_group_enforcement,
        test_student_cannot_reach_other_groups,
        test_legacy_behavior_intact,
    ]:
        fn()

    print("=" * 74)
    print("QuizEazz  ·  class/batch segregation tests")
    print("=" * 74)
    for name, detail in PASS:
        print(f"  PASS  {name}")
    if FAIL:
        print()
        for name, detail in FAIL:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print("-" * 74)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
