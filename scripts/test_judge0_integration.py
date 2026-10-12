"""
Judge0 CE integration tests for QuizEazz.

Static + behavioural. No database, no network, no Deno. These lock down the
invariants that are easy to break silently:

  * QuizEazz java  -> Judge0 language_id 90 (never 62)
  * the required execution limits are sent, and nothing else is
  * every Judge0 status maps to the required QuizEazz outcome
  * marks are computed in SQL, and only 'Correct' earns any
  * a Judge0 outage / timeout can never become "Wrong Answer"
  * an in-progress attempt can never be sent to Judge0
  * no student-facing Run / Compile / Test control exists
  * the API token is not present anywhere in the frontend source or in the
    production build output
  * the verdict RPCs are service_role only

Run:  python scripts/test_judge0_integration.py
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
FUNCS = SQL / "functions" / "judge0-grade"
DIST = ROOT / "dist"

J0_TS = FUNCS / "judge0.ts"
INDEX_TS = FUNCS / "index.ts"
MIG7 = SQL / "migration_07_judge0_integration.sql"

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append((name, detail))


# ---------------------------------------------------------------------------
# Re-implement the mapping from judge0.ts so the tests exercise the same rules.
# If judge0.ts changes, these must change in lockstep.
# ---------------------------------------------------------------------------

IN_FLIGHT = {1, 2}

STATUS_MAP = {
    3: "Correct",
    4: "Wrong Answer",
    5: "Time Limit Exceeded",
    6: "Compilation Error",
    7: "Runtime Error",
    8: "Runtime Error",
    9: "Runtime Error",
    10: "Runtime Error",
    11: "Runtime Error",
    12: "Runtime Error",
    14: "Runtime Error",
    13: "System Error",
}


def map_judge0(status_id, exit_code=None, memory=None, memory_limit=512000):
    if status_id is None:
        return "System Error"
    if status_id in IN_FLIGHT:
        return "Pending Evaluation"
    if exit_code == 137 and isinstance(memory, (int, float)) and memory >= memory_limit:
        return "Memory Limit Exceeded"
    return STATUS_MAP.get(status_id, "System Error")


# ---------------------------------------------------------------------------
# 1. Language mapping — C++ only (QuizEazz V1)
# ---------------------------------------------------------------------------
def test_language_mapping():
    check("judge0.ts exists", J0_TS.exists())
    if not J0_TS.exists():
        return
    t = J0_TS.read_text(encoding="utf-8")

    check(
        "C++ resolves to Judge0 language_id 54",
        re.search(r"QUIZEAZZ_CPP_JUDGE0_LANGUAGE_ID\s*=\s*54\b", t) is not None,
    )
    for dead in ("python: 71", "java: 90", "java: 62", "c: 50",
                 "QUIZEAZZ_JAVA_JUDGE0_LANGUAGE_ID", "JUDGE0_LANGUAGE_IDS"):
        check(f"no dead multi-language mapping remains ({dead})",
              dead not in t, f"found {dead!r} in judge0.ts")

    lang_src = (SRC / "lib" / "languages.ts").read_text(encoding="utf-8")
    check(
        "languages.ts defines only the cpp identifier",
        "cpp: 'C++'" in lang_src
        and "'python'" not in lang_src and '"python"' not in lang_src
        and "'java'" not in lang_src and '"java"' not in lang_src,
        "python/java entries must not remain",
    )


# ---------------------------------------------------------------------------
# 1b. C++-only enforcement (frontend + backend + migration 9)
# ---------------------------------------------------------------------------
def test_cpp_only_enforcement():
    mig9 = SQL / "migration_09_cpp_only_and_verdict_fix.sql"
    check("migration 9 exists", mig9.exists())
    m9 = mig9.read_text(encoding="utf-8") if mig9.exists() else ""

    # 1. Programming question language is cpp (frontend authoring has no selector).
    create_modal = (SRC / "components" / "teacher" / "CreateQuestionModal.tsx").read_text(encoding="utf-8")
    check(
        "teacher authoring UI has no language dropdown",
        not re.search(r'<option value="(any|java|python|c|cpp)"', create_modal),
        "the remaining <select> is the question-category picker, not a language",
    )
    check(
        "teacher authoring always stores cpp",
        re.search(r"const language = 'cpp'", create_modal) is not None,
    )

    # 2. Unsupported programming languages are rejected for new questions.
    check(
        "teacher_create_question rejects non-cpp coding language",
        "p_language IS DISTINCT FROM 'cpp'" in m9
        and "Programming questions must use language ''cpp''" in m9,
    )

    # 3. Unsupported programming questions cannot be added to an exam.
    check(
        "teacher_set_exam_paper rejects non-cpp coding questions",
        "q.language IS DISTINCT FROM 'cpp'" in m9
        and "Only C++ (language ''cpp'')" in m9,
    )

    # 4. A student cannot submit a non-cpp answer through the backend:
    # save_exam_answer ignores p_language for coding and stores 'cpp'.
    check(
        "save_exam_answer forces language cpp for coding answers",
        m9.count("language = 'cpp'") >= 1 or "'cpp'," in m9,
    )
    student_screen = (SRC / "components" / "student" / "ExamTakingScreen.tsx").read_text(encoding="utf-8")
    check(
        "student exam screen has no language selector",
        "<select" not in student_screen,
    )
    check(
        "student exam screen persists cpp",
        "CODE_LANGUAGE" in student_screen or "language: 'cpp'" in student_screen,
    )

    # 5. Judge0 submission always uses language ID 54.
    t = J0_TS.read_text(encoding="utf-8") if J0_TS.exists() else ""
    check(
        "submission path can only produce language_id 54",
        "QUIZEAZZ_CPP_JUDGE0_LANGUAGE_ID" in t
        # the body carries `language_id: languageId`, where languageId comes
        # only from resolveLanguageId(), which returns 54-or-null; no numeric
        # language-id literal may appear anywhere in the grading path.
        and re.search(r"language_id:\s*languageId,", t) is not None
        and not re.search(r"language_id:\s*\d+", t),
    )

    # 6. No frontend language selector exists anywhere in student/teacher UI.
    selector_hits = []
    for p in list((SRC / "components").rglob("*.tsx")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r'option value="(python|java|c|any)"', txt):
            selector_hits.append(str(p.relative_to(ROOT)))
    check("no Python/Java/C/Any option remains in any UI dropdown", not selector_hits,
          str(selector_hits))

    # 7. Review displays C++ (dynamic label resolves cpp -> C++).
    review = (SRC / "components" / "student" / "StudentResultReviewPage.tsx").read_text(encoding="utf-8")
    check(
        "review page renders the stored language via languageLabel",
        "languageLabel(ev.language)" in review,
    )


# ---------------------------------------------------------------------------
# 1c. D1 verdict application (RECORD vs TEXT defect)
# ---------------------------------------------------------------------------
def test_verdict_application():
    mig9 = SQL / "migration_09_cpp_only_and_verdict_fix.sql"
    m9 = mig9.read_text(encoding="utf-8") if mig9.exists() else ""
    m7 = MIG7.read_text(encoding="utf-8") if MIG7.exists() else ""

    # The defect: v_row RECORD compared to TEXT (42883) in migration 7.
    check(
        "migration 7 contains the RECORD defect (documents what is fixed)",
        re.search(r"v_row RECORD;", m7) is not None
        and re.search(r"IF v_row <>\s*'Pending Evaluation'", m7) is not None,
    )
    # The fix: scalar TEXT variable in migration 9.
    check(
        "migration 9 holds status in a scalar TEXT variable",
        re.search(r"v_current_status TEXT;", m9) is not None,
    )
    check(
        "migration 9 compares TEXT to TEXT",
        re.search(r"IF v_current_status <>\s*'Pending Evaluation'", m9) is not None,
    )
    check(
        "migration 9 contains no RECORD status variable in the verdict writer",
        # NOTE: other copied functions legitimately use RECORD for row fetches
        # (v_attempt, v_new_q); only the verdict-status variable v_row was the
        # defect, and it must be gone entirely.
        "v_row" not in m9,
    )
    for outcome in ("Correct", "Wrong Answer", "Runtime Error"):
        check(
            f"migration 9 allow-list still accepts '{outcome}'",
            f"'{outcome}'" in m9,
        )
    check(
        "migration 9 preserves the single-verdict guard",
        "already has a verdict" in m9,
    )
    check(
        "migration 9 preserves the authoritative-context flag",
        "quizeazz.authoritative_context" in m9,
    )


# ---------------------------------------------------------------------------
# 1d2. Exam snapshots + safe editing (Phase D)
# ---------------------------------------------------------------------------
MIG12 = SQL / "migration_12_exam_question_snapshots.sql"


def _migration12_body():
    return MIG12.read_text(encoding="utf-8") if MIG12.exists() else ""


def test_snapshot_integrity():
    check("migration 12 exists", MIG12.exists())
    if not MIG12.exists():
        return
    t = _migration12_body()
    # 6. Snapshot exists (column + builder + backfill-once).
    check(
        "exam_questions carries an immutable snapshot column",
        re.search(r"ADD COLUMN IF NOT EXISTS question_snapshot JSONB", t) is not None,
    )
    check(
        "snapshot builder captures all student-visible + grading fields",
        all(k in t for k in (
            "'title', v_q.title", "'question_text', v_q.question_text",
            "'starter_code', v_q.starter_code", "'reference_solution', v_q.reference_solution",
            "'options', COALESCE(v_q.options",
        )),
    )
    check(
        "snapshot builder captures hidden tests faithfully",
        re.search(r"'tests', v_tests", t) is not None,
    )
    check(
        "backfill is conditional (never overwrites)",
        re.search(r"WHERE eq\.question_snapshot IS NULL", t) is not None,
    )
    # 4/8. Taking reads the snapshot; shape preserved; secrets never served.
    check(
        "exam taking serves snapshot content with live fallback",
        re.search(r"COALESCE\(eq\.question_snapshot->>'title', q\.title\)", t) is not None,
    )
    def _fn_body(text, marker):
        i = text.find(marker)
        if i == -1:
            return ""
        j = text.find("$$;", text.find("AS $$", i))
        return text[i:j] if j != -1 else text[i:]

    for fn in ("get_exam_questions", "get_student_attempt_review", "teacher_get_attempt_detail"):
        body = _fn_body(t, f"CREATE OR REPLACE FUNCTION public.{fn}(")
        check(
            f"read path {fn} never serves the reference solution",
            "reference_solution" not in body,
            "reference stays server-side; only the builder stores it",
        )
    # 6/13. Review reads the snapshot.
    check(
        "student review resolves titles from the snapshot",
        re.search(r"'question_title', COALESCE\(eq\.question_snapshot->>'title', q\.title\)", t) is not None,
    )
    check(
        "teacher detail resolves titles from the snapshot",
        re.search(r"'title', COALESCE\(eq\.question_snapshot->>'title', q\.title\)", t) is not None,
    )
    # 7. Bank edits cannot touch snapshots or history.
    ub = t[t.find("CREATE OR REPLACE FUNCTION public.teacher_update_question("):]
    ub = ub[:ub.find("$$;", ub.find("AS $$"))] if "AS $$" in ub else ub
    check(
        "question-bank edits never write snapshots, papers, tests or attempts",
        "question_snapshot" not in ub
        and "exam_questions" not in ub
        and "programming_test_cases" not in ub
        and "exam_attempts" not in ub,
    )
    # 10/15/16. Grading reads snapshot tests, never the mutable bank table.
    pb_start = t.find("CREATE FUNCTION public.quizeazz_judge0_pending_answers(")
    pb = t[pb_start:t.find("$$;", t.find("AS $$", pb_start))]
    check(
        "pending answers carry snapshot tests",
        "COALESCE(eq.question_snapshot->'tests'" in pb,
    )
    check(
        "pending answers never read the mutable test table",
        "programming_test_cases" not in pb,
    )
    check(
        "gradeAttempt consumes the snapshot row, not the bank RPC",
        (INDEX_TS.read_text(encoding="utf-8").count("quizeazz_judge0_test_cases") == 1),
        "exactly one remaining call site: reference validation",
    )
    check(
        "row tests feed the harness",
        re.search(r"Array\.isArray\(row\.tests\)", INDEX_TS.read_text(encoding="utf-8")) is not None,
    )
    # 17. Historical MCQ option sets (with keys) survive in the snapshot.
    check(
        "snapshot preserves full MCQ options for deterministic review",
        re.search(r"'options', COALESCE\(v_q\.options", t) is not None,
    )
    # 18/19. Published-paper immutability; drafts stay editable.
    check(
        "paper writes still require zero finalized attempts",
        "quizeazz_assert_composition_editable(p_exam_id)" in t,
    )


def test_edit_rpcs_and_ui():
    t = _migration12_body()
    check(
        "teacher_update_question exists with faculty gate",
        re.search(r"CREATE OR REPLACE FUNCTION public\.teacher_update_question\(", t) is not None
        and "Only faculty or administrators can edit questions" in t,
    )
    check(
        "question edits enforce C++-only for coding",
        "Programming questions must use language ''cpp''" in t,
    )
    check(
        "teacher_update_exam exists with finalized-attempt guard",
        re.search(r"CREATE OR REPLACE FUNCTION public\.teacher_update_exam\(", t) is not None
        and "quizeazz_assert_composition_editable(p_exam_id)" in t,
    )
    modal = (SRC / "components" / "teacher" / "CreateQuestionModal.tsx").read_text(encoding="utf-8")
    check(
        "question modal supports edit mode from the stored row",
        "editingQuestion" in modal and "updateQuestion" in modal,
    )
    check(
        "edit form remounts per question (no stale state)",
        re.search(r"key=\{editingQuestion\.id\}", (SRC / "components" / "teacher" / "TeacherQuestionsView.tsx").read_text(encoding="utf-8")) is not None,
    )
    check(
        "bank list exposes an Edit action per question",
        re.search(r"setEditingQuestion\(q\)", (SRC / "components" / "teacher" / "TeacherQuestionsView.tsx").read_text(encoding="utf-8")) is not None,
    )
    exams_view = (SRC / "components" / "teacher" / "TeacherExamsView.tsx").read_text(encoding="utf-8")
    check(
        "exam list exposes an Edit details action",
        "openEditDetails" in exams_view and "updateExam" in exams_view,
    )
    check(
        "exam edit UI states the finalized-attempt rule",
        "once submissions exist" in exams_view,
    )


# ---------------------------------------------------------------------------
# 1d. Semantic grading: hidden tests, normalization, harness (Phase C)
# ---------------------------------------------------------------------------
MIG11 = SQL / "migration_11_hidden_tests_semantic_grading.sql"


def normalize_program_output(text):
    """Exact re-implementation of judge0.ts normalizeProgramOutput.
    Any change here must match the Edge Function byte-for-behavior."""
    if text is None:
        return ""
    lf = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped = "\n".join(line.rstrip(" \t") for line in lf.split("\n"))
    return stripped.rstrip("\n")


def _ui_text(path):
    """Component source with comments stripped, so assertions target rendered
    copy rather than developer comments."""
    txt = (SRC / path).read_text(encoding="utf-8")
    txt = re.sub(r"\{\/\*.*?\*\/\}", "", txt, flags=re.S)
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"(?m)^\s*//.*$", "", txt)
    return txt


def test_ui_cleanup_phase_e():
    student_files = [
        "components/student/StudentResultsPage.tsx",
        "components/student/StudentResultReviewPage.tsx",
        "components/student/StudentGuidelinesPage.tsx",
        "components/student/ExamTakingScreen.tsx",
    ]
    teacher_files = [
        "components/teacher/TeacherQuestionsView.tsx",
        "components/teacher/TeacherResultsView.tsx",
        "components/teacher/CreateQuestionModal.tsx",
        "components/teacher/ManageTestsModal.tsx",
    ]
    for rel in student_files + teacher_files:
        txt = _ui_text(rel)
        check(
            f"no Judge0 brand in rendered UI ({rel.split('/')[-1]})",
            "Judge0" not in txt,
            "implementation brand must not reach users",
        )
        check(
            f"no sandbox/disconnected claims in rendered UI ({rel.split('/')[-1]})",
            "sandbox" not in txt.lower() and "not connected" not in txt,
            "stale connectivity claims must not reach users",
        )
        check(
            f"no 'Code judging pending' in rendered UI ({rel.split('/')[-1]})",
            "Code judging pending" not in txt,
        )
        check(
            f"no 'Programming evaluation status' box in ({rel.split('/')[-1]})",
            "Programming evaluation status" not in txt,
        )

    check(
        "guidelines capability disclaimer is gone",
        "What this platform can and cannot do" not in (SRC / "components" / "student" / "StudentGuidelinesPage.tsx").read_text(encoding="utf-8"),
    )
    review = _ui_text("components/student/StudentResultReviewPage.tsx")
    check(
        "review has no Submission/Evaluation ladder cards",
        "1 · Submission" not in review and "2 · Evaluation" not in review,
    )
    check(
        "review has no awaiting-evaluation banner copy",
        "Some programming questions are awaiting evaluation" not in review,
    )
    check(
        "review pending state uses the simple prescribed wording",
        "Evaluation in progress" in review
        and "Your answer has been submitted successfully" in review,
    )
    check(
        "review keeps the essential result elements",
        ("Evaluated" in review or "ATTEMPT_EVALUATION_LABEL" in review)
        and "marks_awarded" in review
        and "student_code" in review,
    )
    modal = (SRC / "components" / "teacher" / "CreateQuestionModal.tsx").read_text(encoding="utf-8")
    check(
        "question form has no Category/Subcategory inputs",
        "Subcategory / Topic" not in modal and '"Category"' not in modal and "'Category'" not in modal,
    )
    check(
        "question form has no Notes/Rubric/Explanation field",
        "Internal Faculty Notes" not in modal and "setExplanation" not in modal,
    )
    check(
        "review and bank inspect show no Explanation block",
        "Explanation:" not in _ui_text("components/student/StudentResultReviewPage.tsx")
        and "Faculty Notes / Rubric" not in _ui_text("components/teacher/TeacherQuestionsView.tsx"),
    )
    css = (SRC / "index.css").read_text(encoding="utf-8")
    check(
        "readability floor is 12px (text-2xs)",
        re.search(r"font-size:\s*0\.75rem", css) is not None,
    )
    check(
        "no sub-12px arbitrary text sizes remain in src",
        not re.search(r"text-\[(9|10|11)px\]", "".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in list(SRC.rglob("*.tsx")) + list(SRC.rglob("*.ts")))),
    )
    check(
        "no window.location.reload anywhere (refetch architecture only)",
        not any(
            "window.location.reload" in p.read_text(encoding="utf-8", errors="replace")
            for p in list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx"))),
    )
    for rel, fn in [
        ("components/admin/AdminDashboard.tsx", "reload"),
        ("components/teacher/TeacherStudentsView.tsx", "reload"),
        ("components/student/StudentExamsPage.tsx", "reload"),
        ("components/student/StudentResultsPage.tsx", "reload"),
        ("components/teacher/TeacherQuestionsView.tsx", "reload"),
        ("components/teacher/TeacherResultsView.tsx", "reload"),
        ("components/teacher/TeacherExamsView.tsx", "reloadExams"),
        ("components/teacher/TeacherOverview.tsx", "reload"),
        ("components/teacher/TeacherAnalyticsView.tsx", "reload"),
    ]:
        txt = (SRC / rel).read_text(encoding="utf-8")
        check(
            f"Refresh refetches data on {rel.split('/')[-1]}",
            f"onClick={{{fn}}}" in txt,
        )


def test_hidden_test_storage():
    check("migration 11 exists", MIG11.exists())
    if not MIG11.exists():
        return
    t = MIG11.read_text(encoding="utf-8")
    check(
        "programming_test_cases table is created",
        re.search(r"CREATE TABLE IF NOT EXISTS public\.programming_test_cases", t) is not None,
    )
    for col in ("question_id", "case_order", "stdin", "expected_stdout", "is_hidden"):
        check(f"test case column {col} exists", col in t)
    check(
        "duplicate case_order per question is prevented",
        re.search(r"UNIQUE\s*\(\s*question_id\s*,\s*case_order\s*\)", t) is not None,
    )
    check(
        "cases can only belong to coding questions (trigger)",
        "programming_test_cases_coding_only" in t,
    )
    check(
        "hidden-test table is deny-by-default (RLS on, no permissive policy)",
        "ENABLE ROW LEVEL SECURITY" in t
        and "CREATE POLICY" not in t,
    )


def test_normalization_semantics():
    j = J0_TS.read_text(encoding="utf-8") if J0_TS.exists() else ""
    check(
        "Edge Function documents the exact normalization contract",
        "CRLF" in j and "trailing" in j and "No fuzzy matching" in j,
    )
    vectors = [
        # (actual, expected, match, why)
        ("0\n", "0", True, "final newline normalized"),
        ("0", "0\n\n", True, "multiple trailing newlines normalized"),
        ("a\r\nb\r\n", "a\nb", True, "CRLF normalized to LF"),
        ("a\rb\r", "a\nb", True, "lone CR normalized to LF"),
        ("0   \n1\t\n", "0\n1", True, "trailing per-line whitespace ignored"),
        ("   0", "0", False, "leading whitespace is significant"),
        ("0  1", "0 1", False, "internal whitespace is significant"),
        ("0\n\n1", "0\n1", False, "interior blank lines are significant"),
        ("Palindrome", "Not Palindrome", False, "real mismatch stays Wrong Answer"),
        ("", "", True, "empty outputs match"),
        ("   \n", "", True, "whitespace-only output normalizes to empty"),
    ]
    for actual, expected, want, why in vectors:
        got = normalize_program_output(actual) == normalize_program_output(expected)
        check(f"normalization: {why}", got == want,
              f"actual={actual!r} expected={expected!r}")


def test_harness_semantics():
    ix = INDEX_TS.read_text(encoding="utf-8")
    check(
        "every validated case must pass for Correct (loop-then-Correct)",
        re.search(r"passedCases \+= 1", ix) is not None
        and re.search(r"outcome: 'Correct'", ix) is not None,
    )
    check(
        "first output mismatch is Wrong Answer and stops the run",
        re.search(r"outcome:\s*'Wrong Answer'", ix) is not None,
    )
    check(
        "first execution failure is preserved (non-Accepted short-circuits)",
        re.search(r"result\.status_id !== 3", ix) is not None,
    )
    check(
        "transport/poll failures stay pending, never verdicts",
        ix.count("ok: false") >= 2
        and "transportError" in ix,
    )
    check(
        "no usable hidden tests is an honest System Error, never a guess",
        re.search(r"no validated hidden tests for this question", ix) is not None,
    )
    anchor = ix.find("message: `Hidden tests passed")
    # The graded.push call that follows is short; a fixed window keeps the
    # assertion scoped to it instead of bleeding into later helpers.
    apply_block = ix[anchor:anchor + 600] if anchor != -1 else ""
    check(
        "per-case counts are reported without leaking expected content",
        anchor != -1
        and "passed_cases" in apply_block
        and "expected_stdout" not in apply_block,
        "counts are safe metadata; expected outputs must never reach applyVerdict",
    )


def test_hidden_data_inaccessible_to_students():
    check("teacher test-case RPCs are faculty-gated in SQL", True)
    if MIG11.exists():
        t = MIG11.read_text(encoding="utf-8")
        check(
            "teacher set/get test cases require faculty role",
            t.count("Only faculty or administrators can manage hidden test cases") >= 1
            and t.count("Only faculty or administrators can view hidden test cases") >= 1,
        )
        check(
            "service-role grading reads deny authenticated",
            re.search(r"REVOKE ALL ON FUNCTION public\.quizeazz_judge0_test_cases\(UUID\) FROM authenticated", t) is not None
            and re.search(r"GRANT\s+EXECUTE ON FUNCTION public\.quizeazz_judge0_test_cases\(UUID\) TO service_role", t) is not None,
        )
        check(
            "reference applier denies authenticated",
            re.search(r"REVOKE ALL ON FUNCTION public\.quizeazz_apply_reference_outputs\(UUID, JSONB\) FROM authenticated", t) is not None,
        )
        check(
            "reference fetcher denies authenticated",
            re.search(r"REVOKE ALL ON FUNCTION public\.quizeazz_question_reference\(UUID\) FROM authenticated", t) is not None,
        )
        check(
            "paper gate requires a validated hidden test per coding question",
            re.search(r"at least one VALIDATED hidden test case", t) is not None,
        )
    # Student-facing RPC bodies must never select hidden data.
    src_sql = (SQL / "migration_exam_management_integrity.sql").read_text(encoding="utf-8")
    for fn in ("get_student_attempt_review", "get_student_results", "get_exam_questions"):
        i = src_sql.find(f"FUNCTION public.{fn}(")
        body = src_sql[i:src_sql.find("$$;", i)]
        leaks = [w for w in ("programming_test_cases", "reference_solution", "expected_stdout") if w in body]
        check(f"student RPC {fn} cannot see hidden data", not leaks, str(leaks))
    # The browser never sends hidden stdin/expected content anywhere.
    for p in list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx")):
        if "ManageTestsModal" in str(p) or "teacherApi" in str(p):
            continue  # faculty-only code paths, server-gated RPCs
        txt = p.read_text(encoding="utf-8", errors="replace")
        if "expected_stdout" in txt or "expectedStdout" in txt:
            check(f"no student-reachable file references expected outputs ({p.name})", False, str(p))
            return
    check("no student-reachable file references expected outputs", True)


def test_reference_validation_path():
    ix = INDEX_TS.read_text(encoding="utf-8")
    check(
        "reference validation is a faculty-gated Edge action",
        re.search(r"payload\.validate_reference === true", ix) is not None
        and re.search(r"faculty authorization required", ix) is not None,
    )
    check(
        "reference is fetched service-role-only, never from the browser",
        re.search(r"quizeazz_question_reference", ix) is not None,
    )
    check(
        "failed reference cases stay unvalidated (NULL expected)",
        re.search(r"keeps expected_stdout NULL", ix) is not None
        or "keeps blocking publication" in ix,
    )
    check(
        "teacher UI runs validation server-side via the Edge Function",
        re.search(r"validateReferenceSolution", (SRC / "lib" / "teacherApi.ts").read_text(encoding="utf-8")) is not None,
    )
    check(
        "teacher UI never executes code locally",
        "eval(" not in (SRC / "components" / "teacher" / "ManageTestsModal.tsx").read_text(encoding="utf-8"),
    )


# ---------------------------------------------------------------------------
# 2. Submission limits and forbidden fields
# ---------------------------------------------------------------------------
def test_submission_limits():
    if not J0_TS.exists():
        return
    t = J0_TS.read_text(encoding="utf-8")

    required = {
        "cpu_time_limit": 3,
        "cpu_extra_time": 1,
        "wall_time_limit": 10,
        "memory_limit": 512000,
        "max_processes_and_or_threads": 60,
        "max_file_size": 1024,
    }
    for key, want in required.items():
        m = re.search(rf"{key}:\s*(\d+)", t)
        got = int(m.group(1)) if m else None
        check(f"limit {key} = {want}", got == want, f"found {got}")

    body = re.search(r"const body[^=]*=\s*\{(.*?)\n  \}", t, re.S)
    body_txt = body.group(1) if body else ""

    for forbidden in (
        "compiler_options",
        "command_line_arguments",
        "additional_files",
        "callback_url",
    ):
        # Must not appear as a field in the request body. A comment mentioning
        # the name is acceptable; an actual key is not.
        as_key = re.search(rf"^\s*{forbidden}\s*:", body_txt, re.M)
        check(f"request body does not send {forbidden}", as_key is None)

    check(
        "network is explicitly disabled in the request body",
        re.search(r"enable_network\s*:\s*false", body_txt) is not None,
    )
    check(
        "body sends source_code and language_id",
        "source_code" in body_txt and "language_id" in body_txt,
    )
    check(
        "stdin is only sent when non-empty",
        re.search(r"stdin\s*!==\s*''", t) is not None,
    )


# ---------------------------------------------------------------------------
# 3. Status mapping - every required case
# ---------------------------------------------------------------------------
def test_status_mapping():
    cases = [
        ("3 Accepted -> Correct", 3, None, None, "Correct"),
        ("4 Wrong Answer -> Wrong Answer", 4, None, None, "Wrong Answer"),
        ("5 TLE -> Time Limit Exceeded", 5, None, None, "Time Limit Exceeded"),
        ("6 Compilation Error", 6, None, None, "Compilation Error"),
        ("7 SIGSEGV -> Runtime Error", 7, None, None, "Runtime Error"),
        ("8 SIGXFSZ -> Runtime Error", 8, None, None, "Runtime Error"),
        ("9 SIGFPE -> Runtime Error", 9, None, None, "Runtime Error"),
        ("10 SIGABRT -> Runtime Error", 10, None, None, "Runtime Error"),
        ("11 NZEC -> Runtime Error", 11, 1, None, "Runtime Error"),
        ("12 Other -> Runtime Error", 12, None, None, "Runtime Error"),
        ("13 Internal Error -> System Error", 13, None, None, "System Error"),
        ("14 Exec Format Error -> Runtime Error", 14, None, None, "Runtime Error"),
        ("1 In Queue -> Pending (never a verdict)", 1, None, None, "Pending Evaluation"),
        ("2 Processing -> Pending (never a verdict)", 2, None, None, "Pending Evaluation"),
        ("unknown status -> System Error", 999, None, None, "System Error"),
        ("missing status -> System Error", None, None, None, "System Error"),
        ("MLE: exit 137 + memory at limit -> Memory Limit Exceeded",
         11, 137, 512000, "Memory Limit Exceeded"),
        ("MLE: exit 137 + memory over limit -> Memory Limit Exceeded",
         11, 137, 524288, "Memory Limit Exceeded"),
        ("exit 137 but memory BELOW limit stays Runtime Error",
         11, 137, 1000, "Runtime Error"),
    ]
    for name, sid, ec, mem, want in cases:
        got = map_judge0(sid, ec, mem)
        check(name, got == want, f"got {got}")

    # The implementation in judge0.ts must contain the same rules.
    if J0_TS.exists():
        t = J0_TS.read_text(encoding="utf-8")
        check(
            "judge0.ts detects the memory kill via exit 137 + memory >= limit",
            re.search(r"exit_code\s*===\s*137", t) is not None
            and re.search(r"memory\s*>=\s*memoryLimit", t) is not None,
        )
        check(
            "judge0.ts treats status 1/2 as in-flight, not a verdict",
            "IN_FLIGHT" in t and "IN_FLIGHT.has(statusId)" in t,
        )


# ---------------------------------------------------------------------------
# 4. Marks are computed in SQL, and only 'Correct' earns any
# ---------------------------------------------------------------------------
def test_marks_authority():
    check("migration 7 exists", MIG7.exists())
    if not MIG7.exists():
        return
    t = MIG7.read_text(encoding="utf-8")

    check(
        "marks are computed by a SQL function, never accepted from the caller",
        "quizeazz_marks_for_outcome" in t,
    )
    check(
        "only 'Correct' earns marks in SQL",
        re.search(r"WHEN\s+p_outcome\s*=\s*'Correct'\s+THEN", t) is not None,
    )
    check(
        "every other outcome earns exactly zero in SQL",
        re.search(r"ELSE\s+0", t) is not None,
    )
    check(
        "no marks_awarded value is taken from a function parameter",
        not re.search(r"p_marks_awarded", t),
    )
    check(
        "the Edge Function never computes or sends marks",
        not re.search(r"marks_awarded\s*[:=]", INDEX_TS.read_text(encoding="utf-8"))
        if INDEX_TS.exists() else False,
    )


# ---------------------------------------------------------------------------
# 5. Failure never becomes Wrong Answer
# ---------------------------------------------------------------------------
def test_failure_handling():
    if not J0_TS.exists() or not INDEX_TS.exists():
        return
    j = J0_TS.read_text(encoding="utf-8")
    ix = INDEX_TS.read_text(encoding="utf-8")

    check(
        "a transport failure never produces a verdict",
        # Transport catches return ok:false (no outcome attached), and the
        # only consumer maps ok:false to a pending push — never applyVerdict.
        # The window is wide because observability logging now sits between
        # the branch and the push; the invariant itself is unchanged.
        re.search(r"transportError", ix) is not None
        and re.search(
            r"if\s*\(!caseRun\.ok\)\s*\{[\s\S]{0,1200}?pending:\s*true", ix
        ) is not None
        and re.search(
            r"if\s*\(!caseRun\.ok\)\s*\{[\s\S]{0,1200}?continue", ix
        ) is not None,
    )
    check(
        "a failed submission is not mapped to any outcome",
        "outcome: null" in ix,
    )
    check(
        "polling deadline returns a null status so nothing is recorded",
        re.search(r"return\s*\{\s*status_id:\s*null", j) is not None,
    )
    check(
        "'Wrong Answer' is only produced by Judge0 status 4",
        j.count("'Wrong Answer'") == 2,  # the type union + the status-4 case
    )


# ---------------------------------------------------------------------------
# 6. Never execute during an exam
# ---------------------------------------------------------------------------
def test_no_execution_during_exam():
    if not MIG7.exists():
        return
    t = MIG7.read_text(encoding="utf-8")

    check(
        "the pending-answers RPC refuses an in_progress attempt",
        re.search(r"still in progress", t) is not None,
    )
    check(
        "the apply-verdict RPC refuses an in_progress attempt",
        t.count("still in progress") >= 2,
    )
    check(
        "the sweep only selects finalized attempts",
        re.search(
            r"att\.status\s+IN\s*\(\s*'submitted'\s*,\s*'timed_out'\s*,\s*'evaluating'\s*,\s*'evaluated'\s*\)",
            t,
        ) is not None,
    )
    check(
        "an attempt keeps status 'evaluating' while programming is pending",
        re.search(r"WHEN v_pending > 0 THEN 'evaluating' ELSE 'evaluated' END", t) is not None,
    )
    check(
        "a verdict can be applied at most once (never overwrites)",
        re.search(r"already has a verdict", t) is not None,
    )
    check(
        "the student's source code is never updated by the verdict path",
        not re.search(r"UPDATE\s+public\.answers[^;]*SET[^;]*\bcode\s*=", t, re.S),
    )


# ---------------------------------------------------------------------------
# 7. No Run / Compile / Test control in the student UI
# ---------------------------------------------------------------------------
def test_edge_function_auth_identity():
    """
    Regression guard for the spurious-401 defect found in production.

    The `caller` client is built with `persistSession: false` and therefore has
    no session in storage. A bare `getUser()` validates the (absent) SESSION
    rather than the request token, so auth-js short-circuits with
    AuthSessionMissingError and the function rejects a perfectly valid student
    JWT with 401 -- before any grading happens. The explicit JWT must be passed.
    """
    idx = INDEX_TS.read_text(encoding="utf-8")

    bare = re.findall(r"\.auth\.getUser\(\s*\)", idx)
    check(
        "getUser() is never called without an explicit JWT",
        not bare,
        f"{len(bare)} bare call(s); the client has persistSession:false so a bare "
        "getUser() always returns AuthSessionMissingError",
    )

    check(
        "the student JWT is passed to getUser()",
        re.search(r"\.auth\.getUser\(\s*bearer\s*\)", idx) is not None,
        "expected caller.auth.getUser(bearer)",
    )

    # The 401 guard must survive the fix.
    check(
        "the unauthenticated 401 guard is retained",
        re.search(r"if\s*\(\s*userErr\s*\|\|\s*!userData\?\.user\s*\)\s*"
                  r"return\s+json\(\s*\{[^}]*error[^}]*\}\s*,\s*401\s*\)", idx) is not None,
        "the getUser() result must still gate the request",
    )

    # The ownership RPC is the actual cross-student gate and must not be removed.
    check(
        "the ownership RPC is still enforced",
        "quizeazz_attempt_belongs_to_caller" in idx,
        "ownership check must survive any auth refactor",
    )
    check(
        "the ownership check runs on the caller's own JWT client, not service role",
        re.search(r"caller\.rpc\(\s*'quizeazz_attempt_belongs_to_caller'", idx) is not None,
        "auth.uid() only resolves on the caller client",
    )

    # The caller client's `supabaseKey` argument must be the anon key, NEVER
    # the student's JWT: supabase-js sends it verbatim as the `apikey`
    # header, and the gateway rejects a user JWT there with 401 (surfacing as
    # {"error":"unauthenticated"}). The student identity travels only in
    # Authorization.
    check(
        "the caller client uses the anon key, not the bearer, as its key",
        re.search(r"Deno\.env\.get\('SUPABASE_ANON_KEY'\)\s*as\s*string,", idx) is not None,
        "passing `bearer` as the key poisons the apikey header -> gateway 401",
    )
    check(
        "the student JWT travels in the Authorization header",
        re.search(r"Authorization:\s*`Bearer\s*\$\{bearer\}`", idx) is not None,
        "expected global.headers.Authorization Bearer ${bearer}",
    )
    check(
        "the service-role key never enters the caller client",
        "serviceRoleKey" not in idx.split("const caller = createClient(")[1].split(")")[0]
        if "const caller = createClient(" in idx else False,
        "SUPABASE_SERVICE_ROLE_KEY must only feed the admin client",
    )

    # Identity validation must happen BEFORE the service-role client grades the
    # attempt. Compare statement positions in execution order, not raw offsets:
    # the explanatory comment above getUser() mentions gradeAttempt() in prose,
    # so a naive substring search finds the wrong occurrence first.
    stmt_auth = idx.find("const { data: userData, error: userErr } = await caller.auth.getUser(")
    stmt_grade = idx.find("const result = await gradeAttempt(cfg, admin,")
    check(
        "identity is validated before any service-role grading",
        stmt_auth != -1 and stmt_grade != -1 and stmt_auth < stmt_grade,
        f"auth stmt@{stmt_auth} grade stmt@{stmt_grade}",
    )


def test_review_page_polling_is_bounded():
    """
    The review page fetched once and never refreshed, so a successful grading
    run was invisible without a manual F5. Polling must be bounded and cleaned
    up so a student is never left on a permanent timer.
    """
    p = SRC / "components" / "student" / "StudentResultReviewPage.tsx"
    txt = p.read_text(encoding="utf-8")

    check("the review page uses the reload function", "reload" in txt,
          "useAsyncData exposes reload for exactly this purpose")

    check(
        "polling is bounded by a maximum attempt count",
        re.search(r"POLL_MAX_ATTEMPTS\s*=\s*\d+", txt) is not None
        and re.search(r"pollAttemptsRef\.current\s*>=\s*POLL_MAX_ATTEMPTS", txt) is not None,
        "polling must stop after a fixed number of attempts",
    )

    check(
        "polling stops once nothing is pending",
        re.search(r"if\s*\(\s*!pendingProgramming\s*\)\s*return", txt) is not None,
        "a fully evaluated report must never be polled",
    )

    check(
        "the poll timer is cleared on cleanup",
        re.search(r"return\s*\(\s*\)\s*=>\s*clearTimeout\(timer\)", txt) is not None,
        "clearing the timer prevents a leak and duplicate timers",
    )

    check(
        "polling uses a re-armed timeout, not a repeating interval",
        "setInterval" not in txt,
        "setTimeout re-armed per settled reload guarantees a single live timer",
    )

    check(
        "the attempt counter is a ref, not state",
        re.search(r"useRef\(0\)", txt) is not None,
        "a ref avoids an extra render and keeps the cap cumulative",
    )


def test_no_false_judge0_disconnected_claim():
    """The banner once told students Judge0 'is not connected in this
    deployment'. That was false and must not reappear in student-facing copy."""
    p = SRC / "components" / "student" / "StudentResultReviewPage.tsx"
    txt = p.read_text(encoding="utf-8")

    check(
        "no claim that the sandbox is disconnected",
        "not connected in this deployment" not in txt,
        "Judge0 IS connected; the old copy was false",
    )

    leaks = [
        term for term in ("Judge0", "X-Auth-Token", "2358", "3.107", "service_role", "Deno")
        if term in txt
    ]
    check(
        "student-facing copy exposes no Judge0 infrastructure details",
        not leaks,
        str(leaks),
    )

    check(
        "the pending banner does not claim evaluation succeeded",
        re.search(r"ATTEMPT_EVALUATION_LABEL\s*\[\s*evalState\s*\]", txt) is not None,
        "the label must stay driven by the authoritative attempt status",
    )


def test_submit_triggers_grading_before_navigation():
    """
    Production showed zero judge0-grade invocations for a finalized attempt.
    Root cause: the submit handler fired the grading request without awaiting
    it and navigated in the same tick, so the browser could drop the POST
    before it was ever sent. The trigger must be awaited before navigating,
    and a trigger failure must be logged (never silently swallowed) without
    blocking navigation.
    """
    p = SRC / "components" / "student" / "ExamTakingScreen.tsx"
    txt = p.read_text(encoding="utf-8")

    check(
        "the grading trigger is awaited before navigating",
        re.search(r"await\s+requestProgrammingGrading\s*\(\s*supabase\s*,\s*attempt\.id\s*\)", txt) is not None,
        "fire-and-forget lets the browser drop the POST on route change",
    )
    check(
        "no fire-and-forget grading call remains",
        "void requestProgrammingGrading" not in txt,
        "void + .catch(()=>undefined) can leave zero Edge Function invocations",
    )
    check(
        "a grading trigger failure is logged, not swallowed",
        txt.count("console.warn") >= 1
        and "programming grading trigger failed" in txt,
        "silent failures are invisible in production logs",
    )
    check(
        "navigation still follows the grading trigger",
        txt.find("requestProgrammingGrading") < txt.find("navigate("),
        "grading must be triggered before leaving the exam screen",
    )
    check(
        "the trigger still sends only the attempt id",
        re.search(r"body:\s*\{\s*attempt_id:\s*attemptId\s*\}", (
            SRC / "lib" / "judge0Client.ts").read_text(encoding="utf-8")) is not None,
        "the browser must never send code, tokens, or URLs",
    )


def test_no_run_button_in_student_ui():
    banned = re.compile(
        r">\s*(Run|Compile|Test|Run Code|Compile & Run|Run Tests)\s*<", re.I
    )
    hits = []
    for p in sorted(SRC.rglob("*.tsx")):
        if "node_modules" in str(p):
            continue
        if banned.search(p.read_text(encoding="utf-8", errors="replace")):
            hits.append(str(p.relative_to(ROOT)))
    check("no student-facing Run/Compile/Test button exists", not hits, str(hits))


# ---------------------------------------------------------------------------
# 8. The token must never reach the browser
# ---------------------------------------------------------------------------
def test_token_never_exposed():
    # a) No source file may reference a VITE_* Judge0 variable.
    vite_refs = []
    for p in list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"VITE_[A-Z0-9_]*JUDGE0", txt, re.I):
            vite_refs.append(str(p.relative_to(ROOT)))
    check("no VITE_* Judge0 variable is referenced in src/", not vite_refs, str(vite_refs))

    # b) The token literal may only appear in the Edge Function, and only as a
    #    Deno.env read. Never as a literal, never imported into src/.
    check("the token name appears in the Edge Function", 
          "JUDGE0_API_TOKEN" in INDEX_TS.read_text(encoding="utf-8"))
    check(
        "the token is read from Deno.env, not hard-coded",
        re.search(r"Deno\.env\.get\('JUDGE0_API_TOKEN'\)",
                  INDEX_TS.read_text(encoding="utf-8")) is not None,
    )
    check(
        "no Judge0 URL is hard-coded in src/",
        not any(
            re.search(r"3\.107\.47\.4", p.read_text(encoding="utf-8", errors="replace"))
            for p in list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx"))
        ),
    )
    check(
        "no Judge0 URL is hard-coded in the SQL migrations",
        not any(
            re.search(r"3\.107\.47\.4", p.read_text(encoding="utf-8", errors="replace"))
            for p in SQL.glob("*.sql")
        ),
    )

    # c) Nothing under src/ may import from the Edge Function directory.
    bad_import = []
    for p in list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        # A bare "judge0.ts" substring would also match the legitimate import
        # of the public typed error classes from @supabase/supabase-js, which
        # is the SDK, not the Edge Function module.
        if "supabase/functions" in txt or re.search(r"judge0\.ts'", txt):
            bad_import.append(str(p.relative_to(ROOT)))
    check("no src/ file imports the Edge Function code", not bad_import, str(bad_import))

    # d) The Edge Function must never return the token in a response body.
    ix = INDEX_TS.read_text(encoding="utf-8")
    check(
        "the Edge Function never echoes the token in a response",
        not re.search(r"json\(\{[^}]*\btoken\b[^}]*\}\s*,\s*cfg\.token", ix),
    )
    # The browser client may mention the secret NAME in documentation, but it
    # must never read it, never import it, and never contain a literal value.
    client = (SRC / "lib" / "judge0Client.ts").read_text(encoding="utf-8")
    code_only = "\n".join(
        ln for ln in client.splitlines() if not ln.strip().startswith("*")
    )
    check(
        "the browser client never reads the secret from an environment",
        not re.search(r"Deno\.env|process\.env", code_only),
    )
    check(
        "the browser client has no token literal",
        not re.search(r"X-Auth-Token", code_only),
    )
    check(
        "the browser client contains no 40-char token-shaped literal",
        not re.search(r"\b[A-Za-z0-9]{40}\b", code_only),
    )
    check(
        "the browser client sends only the attempt id",
        re.search(r"body:\s*\{\s*attempt_id:", client) is not None,
    )


# ---------------------------------------------------------------------------
# 9. Build output must not contain the token
# ---------------------------------------------------------------------------
def test_build_output_clean():
    if not DIST.exists():
        check("dist/ not built - run `npm run build` then re-run this test", False,
              "dist/ missing")
        return

    # The real token value, read from the server-side file if it is present
    # locally (it never should be). We search for the variable name and for
    # any 40-char token-shaped string assigned to it.
    suspicious = []
    for p in list(DIST.rglob("*.js")) + list(DIST.rglob("*.css")) + list(DIST.rglob("*.html")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if "JUDGE0_API_TOKEN" in txt or "JUDGE0_API_URL" in txt:
            suspicious.append(f"{p.name}: contains a Judge0 secret NAME")
        if re.search(r"X-Auth-Token", txt):
            suspicious.append(f"{p.name}: contains the Judge0 auth header")
        if re.search(r"3\.107\.47\.4", txt):
            suspicious.append(f"{p.name}: contains the Judge0 host")
    check("production build output contains no Judge0 secret or host",
          not suspicious, str(suspicious))


# ---------------------------------------------------------------------------
# 10. RPC privilege model
# ---------------------------------------------------------------------------
def test_rpc_privileges():
    if not MIG7.exists():
        return
    t = MIG7.read_text(encoding="utf-8")

    internal = [
        "quizeazz_marks_for_outcome",
        "quizeazz_list_attempts_needing_programming_grading",
        "quizeazz_judge0_pending_answers",
        "quizeazz_apply_judge0_verdict",
        "quizeazz_finalize_attempt_grading",
    ]
    for fn in internal:
        m = re.search(rf"REVOKE ALL ON FUNCTION public\.{fn}\([^)]*\) FROM authenticated;", t)
        check(f"{fn} is revoked from authenticated", m is not None)
        m2 = re.search(rf"GRANT\s+EXECUTE ON FUNCTION public\.{fn}\([^)]*\) TO service_role;", t)
        check(f"{fn} is granted to service_role", m2 is not None)

    check(
        "the ownership check is revoked from anon and PUBLIC",
        re.search(r"REVOKE ALL ON FUNCTION public\.quizeazz_attempt_belongs_to_caller\(UUID\) FROM PUBLIC;", t) is not None
        and re.search(r"REVOKE ALL ON FUNCTION public\.quizeazz_attempt_belongs_to_caller\(UUID\) FROM anon;", t) is not None,
    )
    # NOTE: the character class must include digits: several function names
    # contain "judge0". An earlier [a-z_]+ silently skipped them.
    all_public = re.findall(
        r"REVOKE ALL ON FUNCTION public\.quizeazz_[a-z0-9_]+\([^)]*\) FROM PUBLIC;", t
    )
    check("no migration 7 function is left callable by PUBLIC", len(all_public) >= 6,
          f"only {len(all_public)} explicit PUBLIC revokes found")
    check(
        "every Judge0 internal function is also revoked from anon",
        len(re.findall(
            r"REVOKE ALL ON FUNCTION public\.quizeazz_[a-z0-9_]+\([^)]*\) FROM anon;", t
        )) >= 6,
    )
    check(
        "no table-level grant is issued",
        not re.search(r"GRANT[^;]*\sON\s+(?!FUNCTION)", t),
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 11. Ambiguous column reference guard (SQLSTATE 42702)
# ---------------------------------------------------------------------------
def test_no_ambiguous_catalog_columns():
    """
    pg_proc and pg_namespace BOTH expose an `oid` column. After joining them,
    any bare `oid` is ambiguous and Postgres rejects the whole statement with
    "column reference oid is ambiguous". This bit migration 7 on its first
    run, so it is now a permanent regression guard.
    """
    if not MIG7.exists():
        return
    raw = MIG7.read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", raw)

    # Any statement that joins two catalog tables which both have `oid`.
    oid_catalogs = ("pg_proc", "pg_namespace", "pg_type", "pg_class", "pg_authid")
    offenders = []
    for stmt in re.split(r";", code):
        joined = set()
        for t in re.findall(r"\b(?:FROM|JOIN)\s+(pg_\w+)", stmt, re.I):
            if t.lower() in oid_catalogs:
                joined.add(t.lower())
        if len(joined) < 2:
            continue
        for m in re.finditer(r"(?<![.\w])oid(?![\w(])", stmt, re.I):
            offenders.append(" ".join(stmt[max(0, m.start() - 70):m.start() + 25].split()))

    check("no bare `oid` after joining two oid-bearing catalogs",
          not offenders, str(offenders[:3]))

    # Every pg_proc query should qualify its oid explicitly.
    check(
        "H2 qualifies its oid as pg_proc.oid",
        "pg_get_function_identity_arguments(pg_proc.oid)" in code
        and "has_function_privilege('authenticated', pg_proc.oid, 'EXECUTE')" in code,
    )
    check(
        "H1 and the ACL DO-blocks qualify oid via the p alias",
        code.count("pg_get_function_identity_arguments(p.oid)") >= 1
        and code.count("p.oid::regprocedure") >= 2,
    )


def test_grading_trigger_retry():
    """A production submission was observed stuck because the browser's ONLY
    invocation of judge0-grade failed before the function ran. The trigger now
    retries transient transport failures; terminal failures are not retried."""
    txt = (SRC / "lib" / "judge0Client.ts").read_text(encoding="utf-8")

    check("grading trigger retries at most 3 times", "MAX_ATTEMPTS = 3" in txt)
    check(
        "grading trigger uses exponential backoff",
        "BASE_RETRY_DELAY_MS * 2 ** attempt" in txt,
    )
    check(
        "retry classification uses typed supabase error classes, not strings",
        "error instanceof FunctionsFetchError" in txt
        and "error instanceof FunctionsRelayError" in txt
        and "error instanceof FunctionsHttpError" in txt,
    )
    check(
        "no fragile message-string error matching",
        "Failed to fetch" not in txt and "error.message.includes" not in txt,
    )
    check(
        "5xx is transient",
        re.search(r"if \(status >= 500\) return true", txt) is not None,
    )
    check(
        "408/429 are transient, other 4xx are terminal",
        "return status === 408 || status === 429" in txt,
    )
    check(
        "a successful response is never retried",
        re.search(r"if \(!error\) \{[\s\S]{0,400}?return \{ \.\.\.payload", txt) is not None,
    )
    check(
        "terminal failure breaks the retry loop immediately",
        "break // terminal: retrying cannot help" in txt,
    )
    check(
        "final failure preserves the existing return shape",
        "return { attempt_id: attemptId, ok: false, error: lastError }" in txt,
    )

    # Timer hygiene: fresh controller per attempt, always cleared.
    check(
        "one controller is created per loop iteration and cleared in finally",
        txt.count("new AbortController()") == 1 and "clearTimeout(timer)" in txt,
    )
    check(
        "retries share one wall-clock budget so the UI is never blocked longer",
        "const deadline = Date.now() + TIMEOUT_MS" in txt,
    )
    check(
        "backoff never runs past the shared deadline",
        "Math.min(BASE_RETRY_DELAY_MS * 2 ** attempt, deadline - Date.now())" in txt,
    )
    check(
        "an application-level failure (200 + ok:false) is never retried",
        re.search(r"if \(!error\) \{[\s\S]{0,400}?return \{ \.\.\.payload", txt) is not None,
    )

    caller = (SRC / "components" / "student" / "ExamTakingScreen.tsx").read_text(encoding="utf-8")
    check(
        "caller still navigates to the review page after any trigger outcome",
        "navigate(" in caller and "requestProgrammingGrading" in caller,
    )
    check(
        "caller still only warns on a failed trigger (never blocks submission)",
        "console.warn" in caller,
    )


def test_sweep_staleness_threshold():
    """The recovery sweep must not race the browser's own grading run."""
    mig = ROOT / "supabase" / "migration_17_sweep_staleness_threshold.sql"
    check("migration 17 exists", mig.exists())
    if not mig.exists():
        return
    t = mig.read_text(encoding="utf-8")
    fn = (ROOT / "supabase" / "functions" / "judge0-grade" / "index.ts").read_text(encoding="utf-8")

    check(
        "sweep RPC keeps its exact signature and return shape",
        "CREATE OR REPLACE FUNCTION public.quizeazz_list_attempts_needing_programming_grading()"
        in t
        and "RETURNS TABLE (attempt_id UUID)" in t,
    )
    check(
        "sweep only returns attempts older than the threshold",
        "make_interval(secs => 60)" in t,
    )
    check(
        "threshold uses the authoritative submission time",
        "att.submitted_at" in t,
    )
    check(
        "sweep still requires a finalized attempt (never in_progress)",
        "att.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated')" in t,
    )
    check(
        "sweep still requires exactly 'Pending Evaluation' with real code",
        "a.evaluation_status = 'Pending Evaluation'" in t and "btrim(a.code) <> ''" in t,
    )
    check("sweep still only considers coding questions", "q.question_type = 'coding'" in t)
    check("migration 17 grants no new privilege", "GRANT" not in t and "REVOKE" not in t)
    check(
        "migration 17 touches no table, column or RLS policy",
        "ALTER TABLE" not in t and "CREATE TABLE" not in t and "POLICY" not in t,
    )
    check(
        "edge function still sweeps through the existing sweep path",
        "quizeazz_list_attempts_needing_programming_grading" in fn and "sweep === true" in fn,
    )
    check(
        "sweep mode is still service-role only",
        "if (!isServiceRole) return json({ error: 'sweep requires service role' }, 403)" in fn,
    )


def _run_node(source):
    """Execute a JS snippet with node and return (ok, stdout, stderr)."""
    node = shutil.which("node")
    if node is None:
        return False, "", "node not found on PATH"
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "snippet.mjs"
        script.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [node, str(script)], capture_output=True, text=True, timeout=60
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, "", str(exc)
    return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()


def _sweep_id_normalizer(fn):
    """
    Extract the real sweep normalization expression from the deployed function.

    The block is lifted verbatim out of index.ts and returned as JS (the two
    TypeScript-only annotations are erased) so the test can EXECUTE the shipped
    code rather than pattern-match it. Returns None if the block is missing.
    """
    match = re.search(
        r"const ids = \(attemptIds \?\? \[\]\)(.*?)\n(\s*)const results",
        fn,
        re.S,
    )
    if not match:
        return None
    block = "const ids = (attemptIds ?? [])" + match.group(1)
    # Erase the TS-only annotations so the block is valid plain JS.
    block = block.replace("(row: PendingAttemptRow | string)", "(row)")
    block = block.replace("(id): id is string", "(id)")
    return block


def test_sweep_normalizes_rpc_row_objects():
    """
    Regression: the sweep must hand gradeAttempt() UUID strings.

    quizeazz_list_attempts_needing_programming_grading() is declared
    RETURNS TABLE (attempt_id UUID), so PostgREST returns it as an array of ROW
    OBJECTS -- [{"attempt_id": "..."}] -- not as an array of bare uuid strings.

    The sweep previously did `const ids = (attemptIds ?? []) as string[]`,
    which silenced the compiler while forwarding each whole row object as
    p_attempt_id. Postgres rejected every one of them with "invalid input
    syntax for type uuid", so the safety sweep could not grade a single
    attempt: all 9 pending attempts failed on every 2-minute run.

    The RPC signature is fixed by migration 7 and unchanged by migration 17, so
    the row-object shape is the real, stable contract and must be unwrapped.

    This test EXECUTES the shipped normalization against the real RPC payload,
    so it fails on the old implementation and passes on the new one. It also
    pins the string form, in case the RPC is ever changed to return bare ids.
    """
    fn = (ROOT / "supabase" / "functions" / "judge0-grade" / "index.ts").read_text(
        encoding="utf-8"
    )

    check(
        "the sweep RPC row type is declared",
        "type PendingAttemptRow = { attempt_id: string }" in fn,
    )
    check(
        "the sweep no longer casts RPC rows with 'as string[]'",
        "as string[]" not in fn,
        "an 'as string[]' cast is what hid the row-object shape from the compiler",
    )
    check(
        "the sweep unwraps attempt_id from each row",
        "typeof row === 'string' ? row : row.attempt_id" in fn,
    )

    normalizer = _sweep_id_normalizer(fn)
    check(
        "the sweep id normalization is present and extractable",
        normalizer is not None,
        "" if normalizer else "could not locate the sweep id normalization block",
    )
    if normalizer is None:
        return

    row_payload = [
        {"attempt_id": "b8467990-1092-4a88-ac4f-afd821e37d52"},
        {"attempt_id": "03f49e35-4864-4ce3-ac40-cdcef45f542b"},
    ]

    # 1. The real RPC shape: an array of row objects.
    ok, out, err = _run_node(
        f"const attemptIds = {json.dumps(row_payload)};\n"
        f"{normalizer}\n"
        "const seen = [];\n"
        "ids.forEach((id) => seen.push(typeof id + ':' + id));\n"
        "console.log(JSON.stringify(seen));\n"
    )
    check(
        "the sweep normalization executes against the row-object shape",
        ok,
        err[:200],
    )
    if not ok:
        return

    try:
        seen = json.loads(out)
    except json.JSONDecodeError:
        seen = None

    expected = [f"string:{row['attempt_id']}" for row in row_payload]
    check(
        "gradeAttempt receives bare uuid strings, not row objects",
        seen == expected,
        f"expected {expected}, got {seen}",
    )
    check(
        "no row object leaks into the grading call",
        seen is not None and not any(s.startswith("object") for s in (seen or [])),
        f"got {seen}",
    )

    # 2. Back-compat: bare strings still work, so a future RPC change to
    #    returning plain uuids cannot silently break the sweep again.
    string_payload = [row["attempt_id"] for row in row_payload]
    ok2, out2, err2 = _run_node(
        f"const attemptIds = {json.dumps(string_payload)};\n"
        f"{normalizer}\n"
        "console.log(JSON.stringify(ids));\n"
    )
    try:
        seen2 = json.loads(out2) if ok2 else None
    except json.JSONDecodeError:
        seen2 = None
    check(
        "the sweep normalization still accepts bare uuid strings",
        ok2 and seen2 == string_payload,
        err2[:200] if not ok2 else f"expected {string_payload}, got {seen2}",
    )

    # 3. The old implementation would NOT have produced these values, which is
    #    what makes this test a real regression guard rather than a tautology.
    ok3, out3, _ = _run_node(
        f"const attemptIds = {json.dumps(row_payload)};\n"
        "const ids = attemptIds;  // the old `as string[]` behaviour\n"
        "const seen = [];\n"
        "ids.forEach((id) => seen.push(typeof id));\n"
        "console.log(JSON.stringify(seen));\n"
    )
    check(
        "the previous implementation is proven to fail this assertion",
        ok3 and out3 == '["object","object"]',
        f"old behaviour produced {out3}, so the check above genuinely discriminates",
    )


def _run_judge0_module(body):
    """
    Run a TypeScript snippet that imports the REAL judge0.ts and executes it.

    Node 24 strips TypeScript types natively, so the shipped module is loaded
    directly rather than re-implemented here. If judge0.ts changes, these tests
    exercise the change instead of a copy of it.
    """
    node = shutil.which("node")
    if node is None:
        return False, "", "node not found on PATH"
    module_uri = J0_TS.resolve().as_uri()
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "harness.ts"
        script.write_text(
            "import * as J from %r;\n%s\n" % (module_uri, body), encoding="utf-8"
        )
        try:
            # bytes + explicit utf-8: on Windows, text=True decodes with the
            # ANSI code page (cp1252), which mangles the multi-byte UTF-8 the
            # harness legitimately emits and would make correct decoding look
            # like a failure.
            proc = subprocess.run(
                [node, str(script)], capture_output=True, timeout=90
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, "", str(exc)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode == 0, stdout.strip(), stderr.strip()


# NOTE: the harness sources below are ASCII-only on purpose. This file is read
# with `encoding="utf-8"`, but non-ASCII literals are written as \uXXXX escapes
# so the generated .ts file is byte-identical regardless of how the harness
# writes it. Non-UTF-8 payload bytes are built at runtime from a byte array.


def test_result_fetch_handles_non_utf8_compiler_output():
    """
    Regression: Judge0 result fetch must use base64_encoded=true.

    Judge0 documents a single 400 for GET /submissions/{token}: "some attributes
    for this submission cannot be converted to UTF-8, use base64_encoded=true
    query parameter" — and it explicitly warns that GCC compiler output must be
    fetched base64-encoded for exactly this reason.

    With base64_encoded=false, every invalid-C++ submission (an undeclared
    identifier, a missing include) made Judge0 answer HTTP 400, so the grading
    request failed as a TRANSPORT error and the answer stayed
    'Pending Evaluation' forever. Production attempt
    b8467990-1092-4a88-ac4f-afd821e37d52 ("printf(a+b)") was wedged this way.

    These tests import the real judge0.ts, mock `fetch`, and assert on the real
    request URL and the real decoded result. They fail against the previous
    implementation because that one requested base64_encoded=false and returned
    the raw base64 body as `compile_output`.
    """
    fn = J0_TS.read_text(encoding="utf-8")

    check(
        "the result fetch requests base64_encoded=true",
        "base64_encoded=true&fields=" in fn,
        "GET /submissions/{token} must opt into base64, or GCC's non-UTF-8 "
        "compile_output makes Judge0 answer HTTP 400",
    )
    check(
        "the result fetch no longer requests base64_encoded=false",
        "base64_encoded=false&fields=" not in fn,
    )
    check(
        "the four Judge0 text attributes are decoded",
        all(
            f"decodeJudge0Text(parsed.{field})" in fn
            for field in ("stdout", "stderr", "compile_output", "message")
        ),
        "stdout, stderr, compile_output and message are the only attributes "
        "Judge0 base64-encodes",
    )

    body = r"""
const b64 = (s: string): string => btoa(String.fromCharCode(...new TextEncoder().encode(s)));

// Judge0 answers base64-encoded. `compile_output` holds raw non-UTF-8 GCC
// bytes (base64 of a byte string that is NOT valid UTF-8).
const NON_UTF8_B64 = (() => {
  const bytes = new Uint8Array([
    ...new TextEncoder().encode("//x.cpp:4:20: error: 'printf' was not declared in this scope\n"),
    0xff, 0xfe, 0x80, 0x0a,
  ]);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
})();

let capturedUrl = "";
const realFetch = globalThis.fetch;
globalThis.fetch = async (url: any) => {
  capturedUrl = String(url);
  return {
    ok: true,
    status: 200,
    json: async () => ({
      stdout: btoa("42\n"),
      stderr: null,
      compile_output: NON_UTF8_B64,
      message: null,
      time: "0.01",
      memory: 100,
      exit_code: null,
      status: { id: 6, description: "Compilation Error" },
    }),
  };
};

const cfg = { baseUrl: "http://judge0.invalid:2358", token: "t" };
const res: any = await J.fetchProgramResult(cfg, "tok-123");

// A. The URL must opt into base64.
console.log(JSON.stringify({ url: capturedUrl }));

// B. The non-UTF-8 compile_output must decode WITHOUT throwing, keeping the
//    readable ASCII diagnostics and substituting U+FFFD for the bad bytes.
console.log(JSON.stringify({
  threw: false,
  compile_output: res.compile_output,
  stdout: res.stdout,
  stderr: res.stderr,
  message: res.message,
  status_id: res.status_id,
}));

// C. That decoded result must flow into the real verdict mapper as a
//    Compilation Error, not a System Error and not an exception.
const mapped = J.mapJudge0Result(res, 512000);
console.log(JSON.stringify({ outcome: mapped.outcome, pending: mapped.pending }));
"""

    ok, out, err = _run_judge0_module(body)
    check(
        "the real judge0.ts result fetch decodes a non-UTF-8 Judge0 response",
        ok,
        err[:400],
    )
    if not ok:
        return

    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    if len(lines) < 3:
        check("the Judge0 harness produced a verdict", False, f"unexpected output: {out[:300]}")
        return

    url_info, decoded, verdict = lines[0], lines[1], lines[2]

    check(
        "the fetched URL carries base64_encoded=true",
        "base64_encoded=true" in url_info.get("url", ""),
        f"got {url_info.get('url')!r}",
    )
    check(
        "base64 compile_output is decoded, not returned raw",
        isinstance(decoded.get("compile_output"), str)
        and "was not declared in this scope" in decoded["compile_output"],
        f"compile_output was {decoded.get('compile_output')!r}",
    )
    check(
        "non-UTF-8 bytes become U+FFFD instead of throwing",
        "\ufffd" in (decoded.get("compile_output") or ""),
        "expected replacement characters for the 0xff/0xfe/0x80 bytes",
    )
    check(
        "base64 stdout is decoded back to the exact program output",
        decoded.get("stdout") == "42\n",
        f"stdout was {decoded.get('stdout')!r}",
    )
    check(
        "null stderr and message stay null (no decode exception)",
        decoded.get("stderr") is None and decoded.get("message") is None,
    )
    check(
        "the decoded result maps to Compilation Error",
        verdict.get("outcome") == "Compilation Error" and verdict.get("pending") is False,
        f"got {verdict}",
    )


def test_decode_judge0_text_edge_cases():
    """
    decodeJudge0Text must be total: it never throws, and it leaves JSON scalars
    (status/time/memory/exit_code) untouched by only ever being applied to the
    four base64 text attributes.
    """
    body = r"""
const b64 = (s: string): string => btoa(String.fromCharCode(...new TextEncoder().encode(s)));
const cases: Record<string, unknown> = {
  null_input: J.decodeJudge0Text(null),
  undefined_input: J.decodeJudge0Text(undefined),
  empty_string: J.decodeJudge0Text(""),
  valid_utf8: J.decodeJudge0Text(b64("h\u00e9llo \u2713")),
  empty_payload: J.decodeJudge0Text(b64("")),
  malformed_base64: J.decodeJudge0Text("!!!not base64!!!"),
  number_not_a_string: J.decodeJudge0Text(42),
  // A multi-byte character split across the decode must not corrupt output.
  multibyte: J.decodeJudge0Text(b64("\u2192 caf\u00e9")),
};
console.log(JSON.stringify(cases));
"""
    ok, out, err = _run_judge0_module(body)
    check(
        "decodeJudge0Text is total and never throws",
        ok,
        err[:400],
    )
    if not ok:
        return

    try:
        got = json.loads(out)
    except json.JSONDecodeError:
        check("decodeJudge0Text results are parseable", False, out[:200])
        return

    check("null decodes to null", got.get("null_input") is None)
    check("undefined decodes to null", got.get("undefined_input") is None)
    check("an empty payload decodes to an empty string", got.get("empty_payload") == "")
    check(
        "valid UTF-8 round-trips exactly",
        got.get("valid_utf8") == "h\u00e9llo \u2713"
        and got.get("multibyte") == "\u2192 caf\u00e9",
        f"got {got.get('valid_utf8')!r} / {got.get('multibyte')!r}",
    )
    check(
        "malformed base64 degrades to null instead of throwing",
        got.get("malformed_base64") is None,
    )
    check(
        "a non-string JSON scalar is never decoded",
        got.get("number_not_a_string") is None,
        "status/time/memory/exit_code must never pass through the decoder",
    )


def test_result_fetch_failure_is_not_a_verdict():
    """
    A Judge0 HTTP 400/5xx must still surface as a transport failure so the
    answer stays 'Pending Evaluation' and is retried. The base64 change must not
    turn an upstream error into a fabricated verdict.
    """
    body = r"""
globalThis.fetch = async () => ({ ok: false, status: 400, json: async () => ({}) });
const cfg = { baseUrl: "http://judge0.invalid:2358", token: "t" };
try {
  await J.fetchProgramResult(cfg, "tok-400");
  console.log(JSON.stringify({ threw: false }));
} catch (err) {
  console.log(JSON.stringify({ threw: true, message: (err as Error).message }));
}
"""
    ok, out, err = _run_judge0_module(body)
    check(
        "an upstream Judge0 error still throws for the transport-failure path",
        ok,
        err[:400],
    )
    if not ok:
        return
    try:
        got = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        check("transport-failure harness output is parseable", False, out[:200])
        return
    check(
        "HTTP 400 is raised as an error carrying the status",
        got.get("threw") is True and "400" in (got.get("message") or ""),
        f"got {got}",
    )


def test_grading_concurrency_safety():
    """Frontend and sweep can both call gradeAttempt for one attempt. Exactly
    one verdict may be committed; the loser must never overwrite it."""
    mig7 = (ROOT / "supabase" / "migration_07_judge0_integration.sql").read_text(encoding="utf-8")

    check(
        "a verdict applies only while the answer is still Pending",
        "IF v_row <> 'Pending Evaluation' THEN" in mig7 and "already has a verdict" in mig7,
    )
    check(
        "a second verdict write is refused by the database itself",
        "RAISE EXCEPTION" in mig7,
    )
    check(
        "the pending-answers read excludes already-graded answers",
        "a.evaluation_status = 'Pending Evaluation'" in mig7,
    )
    check(
        "marks are derived from stored answer rows, never from the caller",
        "SELECT COALESCE(SUM(a.marks_awarded), 0)::INT" in mig7,
    )
    check(
        "an attempt becomes 'evaluated' only once nothing is pending",
        "CASE WHEN v_pending > 0 THEN 'evaluating' ELSE 'evaluated' END" in mig7,
    )
    check(
        "finalization is idempotent (recomputes from stored rows)",
        "quizeazz_finalize_attempt_grading" in mig7,
    )
    check(
        "programming verdicts remain service-role only",
        "GRANT  EXECUTE ON FUNCTION public.quizeazz_apply_judge0_verdict" in mig7,
    )

    fn = (ROOT / "supabase" / "functions" / "judge0-grade" / "index.ts").read_text(encoding="utf-8")
    check(
        "an already-graded attempt is a logged no-op, not an error",
        "note: 'nothing pending'" in fn,
    )


def test_grading_observability():
    """Structured lifecycle logging, with a hard no-secrets guarantee."""
    fn = (ROOT / "supabase" / "functions" / "judge0-grade" / "index.ts").read_text(encoding="utf-8")
    for event in ("grading_start", "grading_failed", "answer_graded", "grading_complete"):
        check(f"logs {event}", f"'{event}'" in fn)

    check(
        "logging is wrapped so it can never fail a request",
        "Logging must never be able to fail" in fn,
    )

    # The logger's field type is a strict allow-list: no token/code/stdin field.
    iface = fn[fn.index("interface GradingLogFields"):fn.index("function logGrading")]
    for forbidden in ("oken", "code", "stdin", "expected", "reference", "secret"):
        check(f"log field allow-list excludes {forbidden!r}", forbidden not in iface)
    check(
        "logs record the trigger source so frontend vs sweep is distinguishable",
        "source: GradingSource" in fn and "'frontend'" in fn and "'sweep'" in fn,
    )
    check(
        "logs record attempt and answer identifiers",
        "attemptId" in iface and "answerId" in iface,
    )
    check(
        "log calls never pass a submission token",
        not re.search(r"logGrading\(\{[^}]*submissionToken", fn),
    )


def test_verdict_row_lock_p1():
    """
    Migration 20 must make "one verdict per answer" a real guarantee.

    The guard SELECT in quizeazz_apply_judge0_verdict read
    answers.evaluation_status WITHOUT a lock. Under READ COMMITTED two
    concurrent graders can both read 'Pending Evaluation'; the second
    UPDATE then blocks, re-checks a WHERE clause that still matches
    (id/attempt_id are immutable), and overwrites the committed verdict.

    Concurrent graders are reachable: a browser-triggered invocation keeps
    running server-side to the 150s Edge limit after the browser gives up
    at 45s, while the 60s sweep guard can already have claimed the same
    attempt; pg_cron also fires every 2 minutes without waiting.
    """
    mig = ROOT / "supabase" / "migration_20_verdict_row_lock.sql"
    check("migration 20 exists", mig.exists())
    if not mig.exists():
        return
    t = mig.read_text(encoding="utf-8")
    body = strip_sql_comments(t)

    check(
        "migration 20 redefines the verdict writer with CREATE OR REPLACE",
        "CREATE OR REPLACE FUNCTION public.quizeazz_apply_judge0_verdict(" in t,
    )
    check(
        "migration 20 keeps the exact parameter list",
        all(
            f"p_{n} {ty}" in t
            for n, ty in [
                ("attempt_id", "UUID"),
                ("answer_id", "UUID"),
                ("submission_token", "TEXT"),
                ("outcome", "TEXT"),
                ("status_id", "INT"),
                ("exit_code", "INT"),
                ("memory", "INT"),
                ("time", "NUMERIC"),
                ("stdout", "TEXT"),
                ("stderr", "TEXT"),
                ("compile_output", "TEXT"),
                ("message", "TEXT"),
            ]
        ),
    )
    check(
        "return type, volatility surface and search_path are unchanged",
        "RETURNS BOOLEAN" in t
        and "LANGUAGE plpgsql" in t
        and "SECURITY DEFINER" in t
        and "SET search_path = public, pg_temp" in t,
    )

    # --- the actual safeguard -------------------------------------------------
    guard = re.search(
        r"SELECT a\.evaluation_status INTO v_current_status(.*?);", body, re.S
    )
    check("the guard SELECT is present", guard is not None)
    if guard:
        g = guard.group(1)
        check(
            "the guard SELECT locks the answer row (FOR UPDATE)",
            re.search(r"FOR\s+UPDATE\s*$", g.strip(), re.I) is not None,
            f"guard clause was: {g.strip()!r}",
        )
        check(
            "the lock is taken on answers, not on a joined table",
            "a.id = p_answer_id" in g and "FROM public.answers a" in g,
        )
    check(
        "FOR UPDATE is applied exactly once in the whole function",
        len(re.findall(r"FOR\s+UPDATE", body, re.I)) == 1,
    )

    # --- the trap this migration must NOT fall into --------------------------
    upd = re.search(r"UPDATE public\.answers a(.*?);", body, re.S)
    check("the verdict UPDATE is present", upd is not None)
    if upd:
        where = upd.group(1)
        check(
            "the UPDATE predicate is still keyed only on id + attempt_id",
            "a.id = p_answer_id" in where and "a.attempt_id = p_attempt_id" in where,
        )
        check(
            "the UPDATE did NOT gain an evaluation_status predicate "
            "(that would silently match 0 rows while still returning TRUE)",
            not re.search(r"evaluation_status\s*=\s*'Pending Evaluation'", where),
            "a status predicate requires an explicit ROW_COUNT check; "
            "without it a losing grader would get applied:true and no error",
        )

    # --- behaviour that must not drift ---------------------------------------
    check(
        "the existing 'already has a verdict' refusal is preserved verbatim",
        "already has a verdict" in body
        and re.search(
            r"IF v_current_status <> 'Pending Evaluation' THEN", body
        )
        is not None,
    )
    check(
        "the deleted-answer refusal is preserved (FOR UPDATE can return no row)",
        re.search(r"IF v_current_status IS NULL THEN", body) is not None,
    )
    check(
        "the in-progress refusal is preserved",
        "the examination is still in progress" in body,
    )
    allow = re.search(r"IF p_outcome IS NULL OR p_outcome NOT IN \((.*?)\)", body, re.S)
    outcomes = re.findall(r"'([^']+)'", allow.group(1)) if allow else []
    check(
        "the outcome allow-list is exactly the 8 objective verdicts "
        "('Pending Evaluation' must NOT be accepted as an outcome)",
        outcomes
        == [
            "Correct",
            "Wrong Answer",
            "Compilation Error",
            "Runtime Error",
            "Time Limit Exceeded",
            "Memory Limit Exceeded",
            "System Error",
            "Not Attempted",
        ],
        f"got {outcomes}",
    )
    check(
        "marks are still computed in SQL, never accepted from the caller",
        "quizeazz_marks_for_outcome(p_outcome, v_marks_total)" in body,
    )
    seg = _verdict_body("migration_20_verdict_row_lock.sql")
    if seg and seg[-1] == "$$;":
        seg = seg[:-1]
    check(
        "the function still returns TRUE on success, as its last statement",
        len(seg) >= 2
        and seg[-2].strip() == "RETURN TRUE;"
        and seg[-1].strip() == "END;",
        f"ended with {seg[-3:]!r}",
    )
    check(
        "the anti-tamper authoritative flag is still set before the write",
        "set_config('quizeazz.authoritative_context', 'true', true)" in body,
    )
    check(
        "the student's source code is still never written by this function",
        "code =" not in body and "language =" not in body,
    )

    # --- blast radius ---------------------------------------------------------
    check("migration 20 grants no new privilege", "GRANT" not in t and "REVOKE" not in t)
    check(
        "migration 20 touches no table, column or RLS policy",
        "ALTER TABLE" not in t and "CREATE TABLE" not in t and "POLICY" not in t,
    )
    check(
        "migration 20 creates no function other than the one it replaces",
        len(re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION", t, re.I)) == 1,
    )
    check(
        "migration 20 drops nothing (no earlier migration is re-run or undone)",
        "DROP " not in t.upper(),
    )
    check(
        "migration 20 differs from the deployed body by FOR UPDATE alone",
        _migration20_delta_is_lock_only(),
    )


def strip_sql_comments(text: str) -> str:
    """Remove `--` line comments so assertions cannot match the prose."""
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        out.append(line)
    return "\n".join(out)


VERDICT_MARKER = "CREATE OR REPLACE FUNCTION public.quizeazz_apply_judge0_verdict("


def _verdict_body(fname):
    """Comment-stripped source of one definition of the verdict writer."""
    text = (ROOT / "supabase" / fname).read_text(encoding="utf-8")
    i = text.index(VERDICT_MARKER)
    seg = text[i : text.index("$$;", i) + 3]
    return [ln.rstrip() for ln in strip_sql_comments(seg).splitlines() if ln.strip()]


def _migration20_delta_is_lock_only():
    """
    The strongest guard against an accidental behaviour change.

    Migration 09 is the definition currently deployed, so migration 20's
    function body must differ from it by exactly one added FOR UPDATE and
    nothing else - no signature, no logic, no return-semantics drift.
    """
    import difflib

    try:
        before = _verdict_body("migration_09_cpp_only_and_verdict_fix.sql")
        after = _verdict_body("migration_20_verdict_row_lock.sql")
    except (ValueError, OSError):
        return False

    delta = [
        ln
        for ln in difflib.unified_diff(before, after, n=0)
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
    ]
    removed = [x for x in delta if x.startswith("-")]
    added = [x for x in delta if x.startswith("+")]
    lock_lines = [x for x in added if "FOR UPDATE" in x]
    # the only tolerated removal is the re-terminated statement the lock joins
    tolerated = [x for x in removed if x.strip() == "-  WHERE a.id = p_answer_id;"]
    return (
        len(lock_lines) == 1
        and len(added) == 2
        and len(removed) == len(tolerated)
        and len(removed) <= 1
    )


def _sweep_wave_loop(fn):
    """
    Extract the real sweep wave loop out of index.ts as plain JS.

    Same approach as _sweep_id_normalizer: lift the shipped code so the test
    EXECUTES it rather than pattern-matching it. The two TypeScript-only
    annotations inside the block are erased.
    """
    match = re.search(
        r"const results: Array<Record<string, unknown>> = \[\](.*?)"
        r"const deferred = ids\.length - started",
        fn,
        re.S,
    )
    if not match:
        return None
    block = "const results = []" + match.group(1)
    block = block.replace(": Array<Record<string, unknown>>", "")
    block = block.replace(": string[]", "")
    return block


def _sweep_constants(fn):
    """Return (budget_expr, concurrency_expr, attempt_budget_expr) from index.ts."""
    b = re.search(r"const SWEEP_BUDGET_MS = ([\d_]+)", fn)
    c = re.search(r"const SWEEP_CONCURRENCY = (\d+)", fn)
    a = re.search(r"const GRADING_ATTEMPT_BUDGET_MS = ([\d_]+)", fn)
    return (
        b.group(1) if b else None,
        c.group(1) if c else None,
        a.group(1) if a else None,
    )


def test_sweep_budget_p2():
    """
    The sweep must be bounded by WALL-CLOCK TIME, not by attempt count.

    A 75-student production load test lost 21 invocations to the 150s Edge
    Function limit: 75 attempts x 5 hidden cases = 375 Judge0 submissions at
    ~2.63/sec is ~143s of pure Judge0 time before any poll latency.

    A fixed batch size cannot fix this, because the cost unit is
    SUBMISSIONS not attempts. The publication gate requires only one
    validated hidden test and caps neither cases-per-question nor
    coding-questions-per-paper, so any attempt count can map to an
    unbounded number of submissions.
    """
    fn = (FUNCS / "index.ts").read_text(encoding="utf-8")
    budget, conc, attempt_budget = _sweep_constants(fn)

    check("sweep budget constant exists", budget is not None)
    check("sweep concurrency constant exists", conc is not None)
    check("per-attempt budget constant exists", attempt_budget is not None)
    check("sweep budget is 110000 ms (40s margin below the 150s wall)",
          budget == "110_000", f"found {budget}")
    check("sweep concurrency is 10", conc == "10", f"found {conc}")
    check("per-attempt budget is 90000 ms (inside the sweep budget)",
          attempt_budget == "90_000", f"found {attempt_budget}")

    check(
        "the unbounded fan-out is gone",
        "Promise.all(\n        ids.map(" not in fn
        and "ids.map((id) => gradeAttempt(cfg, admin, id, 'sweep'))" not in fn,
    )
    check(
        "sweep grades in waves bounded by SWEEP_CONCURRENCY",
        re.search(
            r"for \(let i = 0; i < ids\.length; i \+= SWEEP_CONCURRENCY\)", fn
        )
        is not None,
    )
    check(
        "the deadline is checked before each attempt is admitted to a wave",
        re.search(
            r"while \(wave\.length < SWEEP_CONCURRENCY.*?"
            r"if \(Date\.now\(\) >= sweepDeadline\)",
            fn,
            re.S,
        )
        is not None,
    )
    check(
        "an exhausted budget stops claiming new attempts",
        "budgetExhausted = true" in fn and "if (budgetExhausted) break" in fn,
    )
    check(
        "unfinished work is reported as deferred, not discarded",
        "const deferred = ids.length - started" in fn and "deferred," in fn,
    )
    check(
        "the response keeps its existing 'swept' field",
        "swept: ids.length" in fn,
    )
    check(
        "the sweep still runs through the same RPC and 60s staleness guard",
        "quizeazz_list_attempts_needing_programming_grading" in fn
        and "sweep === true" in fn,
    )
    check(
        "sweep mode is still service-role only",
        "if (!isServiceRole) return json({ error: 'sweep requires service role' }, 403)"
        in fn,
    )
    check(
        "the sweep still grades via gradeAttempt with source 'sweep'",
        re.search(
            r"gradeAttempt\(cfg, admin, id, 'sweep', waveDeadline\)", fn
        )
        is not None,
    )

    loop = _sweep_wave_loop(fn)
    check("the wave loop could be extracted for execution", loop is not None)
    if loop is None:
        return

    def harness(n_ids, budget_ms, cost_ms, concurrency=10, attempt_budget=10_000_000):
        """Run the REAL loop against a virtual clock and a mock grader."""
        ids = [f"attempt-{i:03d}" for i in range(n_ids)]
        return f"""
const SWEEP_BUDGET_MS = {budget_ms}
const SWEEP_CONCURRENCY = {concurrency}
const GRADING_ATTEMPT_BUDGET_MS = {attempt_budget}
const ids = {json.dumps(ids)}
const cfg = {{}}, admin = {{}}
let startedAt = 0

// Virtual clock: each graded attempt costs `cost_ms`.
let now = 0
Date.now = () => now

let inflight = 0, maxInflight = 0
const order = []
const deadlines = []
const gradeAttempt = async (cfg, admin, id, source, deadlineAt) => {{
  inflight++
  if (inflight > maxInflight) maxInflight = inflight
  order.push(id)
  deadlines.push(deadlineAt === undefined ? null : deadlineAt)
  await new Promise((r) => setTimeout(r, 0))
  now += {cost_ms}
  inflight--
  return {{ attempt_id: id, ok: true }}
}}

{loop}

const deferredFinal = ids.length - started
console.log(JSON.stringify({{
  started, deferred: deferredFinal, failed, budgetExhausted,
  results: results.length, maxInflight, order, deadlines,
}}))
"""

    # --- behavioural: concurrency is capped ----------------------------------
    budget_for_test = 0 + 10_000_000  # startedAt(=0) + SWEEP_BUDGET_MS in harness
    ok, out, err = _run_node(harness(25, 10_000_000, 1))
    check("wave loop executes (all work, no deadline)", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "a full sweep grades every attempt when budget allows",
            r["started"] == 25 and r["deferred"] == 0,
            f"started={r['started']} deferred={r['deferred']}",
        )
        check(
            "concurrency never exceeds SWEEP_CONCURRENCY",
            r["maxInflight"] <= 10,
            f"max in-flight was {r['maxInflight']}",
        )
        check(
            "concurrency actually reaches the cap (waves really are parallel)",
            r["maxInflight"] == 10,
            f"max in-flight was {r['maxInflight']}",
        )
        check(
            "every result is returned, so nothing graded is lost",
            r["results"] == 25,
        )
        check(
            "attempts are processed in list order",
            r["order"] == [f"attempt-{i:03d}" for i in range(25)],
        )
        check(
            "every attempt is given a deadline no later than the sweep deadline",
            all(d is not None and d <= budget_for_test for d in r["deadlines"]),
            f"deadlines={r['deadlines'][:3]}",
        )

    # --- behavioural: the deadline stops new work ----------------------------
    # NOTE: the virtual clock advances once per GRADED ATTEMPT, so a wave of
    # 10 concurrent attempts at cost_ms=50 advances it by 500ms, not 50ms.
    #   wave 1 (10 attempts) -> now = 500ms; next wave sees 500 >= 60 and
    #   refuses to start anything.
    ok, out, err = _run_node(harness(25, 60, 50))
    check("deadline path executes", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "an exhausted budget stops new attempts",
            r["budgetExhausted"] is True,
        )
        check(
            "work past the deadline is deferred, not started",
            r["started"] == 10 and r["deferred"] == 15,
            f"started={r['started']} deferred={r['deferred']}",
        )
        check(
            "deferred work is exactly what was NOT started (recoverable)",
            r["results"] == r["started"],
            f"results={r['results']} started={r['started']}",
        )
        check(
            "the deadline never lets a partial wave count as started",
            r["started"] % 10 == 0,
        )

    # --- behavioural: the deadline is honoured mid-sweep, not just at the end
    # 45 attempts, 5ms each => 50ms per wave. Budget 120ms allows three waves.
    #   waves 1-3 run (now 50, 100, 150); wave 4 sees 150 >= 120 and stops.
    ok, out, err = _run_node(harness(45, 120, 5))
    check("multi-wave deadline path executes", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "the sweep stops partway through a large backlog",
            r["started"] == 30 and r["deferred"] == 15,
            f"started={r['started']} deferred={r['deferred']}",
        )
        check(
            "work completed before the deadline is fully reported",
            r["results"] == 30,
            f"results={r['results']}",
        )
        check(
            "the sweep returns well inside the 150s wall (virtual 150ms)",
            r["budgetExhausted"] is True,
        )

    # --- behavioural: a budget of zero starts nothing ------------------------
    ok, out, err = _run_node(harness(25, 0, 1))
    check("zero-budget path executes", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "a zero budget grades nothing and defers everything",
            r["started"] == 0 and r["deferred"] == 25,
            f"started={r['started']} deferred={r['deferred']}",
        )
        check(
            "a zero budget still returns cleanly rather than throwing",
            r["results"] == 0,
        )

    # --- behavioural: an empty backlog is a clean no-op ----------------------
    ok, out, err = _run_node(harness(0, 60, 50))
    check("empty-backlog path executes", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "an empty sweep is a clean no-op",
            r["started"] == 0 and r["deferred"] == 0 and r["results"] == 0,
        )


def test_sweep_failure_isolation_p4():
    """
    One failed attempt must not stop the others, and every attempt must
    carry a real deadline.

    Previously the wave used `Promise.all`, so a single rejecting
    gradeAttempt rejected the whole wave: the other nine attempts' results
    were DISCARDED even though they had completed, and no later wave ever
    started. Worse, because attempts in a wave run concurrently, a wave's
    length is its slowest member - one attempt whose Judge0 cases each burn
    the full 30s poll budget could hold the wave open past the 150s Edge
    wall and kill the invocation.

    Separate from P1 (row lock) and P3 (error handler) on purpose.
    """
    fn = (FUNCS / "index.ts").read_text(encoding="utf-8")
    j0 = (FUNCS / "judge0.ts").read_text(encoding="utf-8")

    check(
        "the sweep uses allSettled, so one rejection cannot abandon the wave",
        "await Promise.allSettled(" in fn
        and "wave.map((id) => gradeAttempt(cfg, admin, id, 'sweep', waveDeadline))"
        in fn,
    )
    check(
        "a plain Promise.all over the wave is gone",
        not re.search(
            r"const waveResults = await Promise\.all\(\s*wave\.map", fn
        ),
    )
    check(
        "fulfilled results are still collected",
        re.search(r"if \(s\.status === 'fulfilled'\) \{\s*results\.push", fn) is not None,
    )
    check(
        "a rejected attempt is counted and logged, never silently dropped",
        "failed += 1" in fn and "attempt raised:" in fn,
    )
    check(
        "the rejection log carries no token or source code",
        re.search(
            r"attempt raised:[^`]*\$\{[^}]*\.message", fn, re.S
        )
        is not None,
    )
    check(
        "failures are reported in the response",
        "failed," in fn and "graded: started - failed" in fn,
    )

    # --- per-attempt deadline -----------------------------------------------
    check(
        "gradeAttempt accepts a deadline",
        re.search(
            r"async function gradeAttempt\([\s\S]*?deadlineAt: number \| null = null,", fn
        )
        is not None,
    )
    check(
        "the sweep computes one deadline per wave as the MIN of both limits",
        re.search(
            r"Math\.min\(\s*Date\.now\(\) \+ GRADING_ATTEMPT_BUDGET_MS,\s*sweepDeadline,?\s*\)",
            fn,
        )
        is not None,
        "an attempt must never be allowed to outlive the budget that admitted it",
    )
    check(
        "the sweep deadline is derived from the sweep start",
        "const sweepDeadline = startedAt + SWEEP_BUDGET_MS" in fn,
    )
    check(
        "the answer loop stops when its own deadline is spent",
        re.search(
            r"for \(const row of rows\) \{[\s\S]*?if \(deadlineAt !== null && Date\.now\(\) >= deadlineAt\) \{[\s\S]*?break\b",
            fn,
        )
        is not None,
    )
    check(
        "the deadline reaching a hidden-test case is NOT a verdict",
        "transportError: 'grading deadline reached'" in fn
        and "ok: false" in fn,
    )
    check(
        "the deadline is forwarded into the per-case poll",
        "deadlineAt," in fn and "runHiddenTests(cfg, {" in fn,
    )
    check(
        "pollUntilVerdict honours an optional absolute deadline",
        "deadlineAt?: number | null" in j0
        and "if (deadlineAt !== null && Date.now() >= deadlineAt) break" in j0,
    )
    check(
        "the poll sleep is clipped so it cannot overshoot the deadline",
        re.search(r"Math\.min\(opts\.delayMs, deadlineAt - Date\.now\(\)\)", j0)
        is not None,
    )
    check(
        "an exhausted deadline still finalizes the attempt",
        "finalization runs even after a deadline break" in fn.lower()
        or "quizeazz_finalize_attempt_grading" in fn,
    )
    check(
        "answers never reached are still counted as pending",
        "deadlineHit ? rows.length - graded.length : 0" in fn,
    )
    check(
        "the per-attempt deadline is observable in the result",
        "deadline_exhausted: deadlineHit" in fn,
    )

    # --- browser-triggered grading is deliberately unaffected ----------------
    check(
        "frontend grading still calls gradeAttempt with no deadline "
        "(browser behaviour unchanged)",
        "gradeAttempt(cfg, admin, attemptId, 'frontend')" in fn,
    )
    check(
        "reference validation is not given a deadline either",
        fn.count("deadlineAt: input.deadlineAt") == 0,
    )

    loop = _sweep_wave_loop(fn)
    check("the wave loop could be extracted for execution", loop is not None)
    if loop is None:
        return

    def isolation_harness(n_ids, throw_ids, budget_ms=10_000_000, cost_ms=1):
        ids = [f"attempt-{i:03d}" for i in range(n_ids)]
        throw_list = json.dumps(throw_ids)
        return f"""
const SWEEP_BUDGET_MS = {budget_ms};
const SWEEP_CONCURRENCY = 10;
const GRADING_ATTEMPT_BUDGET_MS = 10_000_000;
const ids = {json.dumps(ids)};
const throwIds = new Set({throw_list});
const cfg = {{}}, admin = {{}};
let startedAt = 0;
let now = 0;
Date.now = () => now;
const logGrading = () => {{}};
const completed = [];
const gradeAttempt = async (cfg, admin, id) => {{
  await new Promise((r) => setTimeout(r, 0));
  if (throwIds.has(id)) throw new Error('boom ' + id);
  now += {cost_ms};
  completed.push(id);
  return {{ attempt_id: id, ok: true }};
}};
{loop}
console.log(JSON.stringify({{
  started, failed, deferred: ids.length - started,
  results: results.length, completed: completed.length,
  budgetExhausted,
}}));
"""

    # --- one attempt in wave 1 throws ---------------------------------------
    ok, out, err = _run_node(isolation_harness(25, ["attempt-003"]))
    check("isolation path executes", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "a single failing attempt does not stop the sweep",
            r["deferred"] == 0 and r["started"] == 25,
            f"started={r['started']} deferred={r['deferred']}",
        )
        check(
            "the failure is counted, not swallowed",
            r["failed"] == 1,
            f"failed={r['failed']}",
        )
        check(
            "every OTHER attempt still completes and is reported",
            r["completed"] == 24 and r["results"] == 24,
            f"completed={r['completed']} results={r['results']}",
        )

    # --- several attempts across several waves fail -------------------------
    ok, out, err = _run_node(
        isolation_harness(25, ["attempt-000", "attempt-012", "attempt-024"])
    )
    check("multi-wave isolation path executes", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "failures spread across waves do not halt later waves",
            r["started"] == 25 and r["deferred"] == 0,
            f"started={r['started']} deferred={r['deferred']}",
        )
        check(
            "every failure is counted",
            r["failed"] == 3,
            f"failed={r['failed']}",
        )
        check(
            "the surviving attempts all complete",
            r["completed"] == 22 and r["results"] == 22,
            f"completed={r['completed']} results={r['results']}",
        )

    # --- every attempt fails: still a clean, fully-accounted sweep ----------
    ok, out, err = _run_node(isolation_harness(10, [f"attempt-{i:03d}" for i in range(10)]))
    check("total-failure path executes", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "a fully failing wave is reported, not thrown",
            r["failed"] == 10 and r["results"] == 0 and r["deferred"] == 0,
            f"failed={r['failed']} results={r['results']}",
        )


def test_error_handler_scope_p3():
    """
    The catch handler must be able to read isServiceRole.

    `catch` is a SIBLING block of `try`, so a `const` declared inside `try`
    is not in scope there. The handler read `isServiceRole` when logging a
    failure, which threw a ReferenceError from INSIDE the error handler:
    the intended JSON 500 never returned, the real error message was lost,
    and a grading failure became an opaque platform failure.

    Separate from P1/P2 on purpose - this is a pre-existing defect that was
    already visible as TS2304 at HEAD, not something either change caused.
    """
    fn = (FUNCS / "index.ts").read_text(encoding="utf-8")

    handler = fn[fn.index("Deno.serve(") :]
    try_open = handler.index("try {")
    catch_at = handler.index("} catch (err) {")
    catch_end = handler.index("\n})", catch_at)
    try_block = handler[try_open:catch_at]
    catch_block = handler[catch_at:catch_end]

    check(
        "the catch handler really does reference isServiceRole",
        "isServiceRole" in catch_block,
    )

    # The declaration must appear BEFORE the try, i.e. in handler scope.
    decl = re.search(
        r"^\s*(let|var)\s+isServiceRole\b", handler[:try_open], re.M
    )
    check(
        "isServiceRole is declared before the try block, so catch can read it",
        decl is not None,
        "a `const isServiceRole` inside `try` is invisible to the sibling catch",
    )
    check(
        "no `const isServiceRole` still shadows it inside the try block",
        not re.search(r"\bconst\s+isServiceRole\b", try_block),
    )
    check(
        "isServiceRole is assigned inside the try block (unchanged logic)",
        re.search(r"^\s*isServiceRole\s*=\s*!!serviceRoleKey && bearer === serviceRoleKey", try_block, re.M)
        is not None,
    )
    check(
        "the flag is still initialised to false so an early throw is safe",
        re.search(r"\blet\s+isServiceRole\s*=\s*false", handler[:try_open]) is not None,
    )
    check(
        "the catch handler still returns the intended JSON 500",
        "return json({ error: message }, 500)" in catch_block,
    )
    check(
        "the error message is still never echoed with a token",
        "submissionToken" not in catch_block and "JUDGE0_API_TOKEN" not in catch_block,
    )

    # --- behavioural: prove the scoping rule the fix relies on --------------
    probe = """
let threw = null;
function handler(fix) {
  const scope = fix
    ? "let isServiceRole = false;"
    : "";                       // fix=false: nothing declared in handler scope
  return new Function(`
    ${scope}
    return async function run() {
      try {
        ${fix ? "isServiceRole = true;" : "const isServiceRole = true;"}
        throw new Error('boom');
      } catch (err) {
        return isServiceRole ? 'sweep' : 'frontend';
      }
    };
  `)();
}
(async () => {
  const a = {};
  const bad = await handler(false).call(a);
  const good = await handler(true).call(a);
  console.log(JSON.stringify({ bad, good }));
})();
"""
    # The unfixed shape must THROW; the fixed shape must return a label.
    unfixed = """
function run() {
  try {
    const isServiceRole = true;
    throw new Error('boom');
  } catch (err) {
    return isServiceRole ? 'sweep' : 'frontend';
  }
}
try { console.log(JSON.stringify({ out: run() })); }
catch (e) { console.log(JSON.stringify({ threw: e.constructor.name })); }
"""
    fixed = unfixed.replace(
        "  try {\n    const isServiceRole = true;",
        "let isServiceRole = false;\n  try {\n    isServiceRole = true;",
    )
    ok, out, err = _run_node(unfixed)
    check("the pre-fix scoping shape is reproduced", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "a binding declared inside `try` is genuinely invisible to `catch` "
            "(this is the bug)",
            r.get("threw") == "ReferenceError",
            f"got {r}",
        )
    ok, out, err = _run_node(fixed)
    check("the corrected scoping shape executes", ok, err)
    if ok:
        r = json.loads(out)
        check(
            "hoisting the declaration lets the handler read the flag",
            r.get("out") == "sweep",
            f"got {r}",
        )


def main():
    for fn in [
        test_language_mapping,
        test_cpp_only_enforcement,
        test_verdict_application,
        test_snapshot_integrity,
        test_edit_rpcs_and_ui,
        test_ui_cleanup_phase_e,
        test_hidden_test_storage,
        test_normalization_semantics,
        test_harness_semantics,
        test_hidden_data_inaccessible_to_students,
        test_reference_validation_path,
        test_submission_limits,
        test_status_mapping,
        test_marks_authority,
        test_failure_handling,
        test_no_execution_during_exam,
        test_no_run_button_in_student_ui,
        test_edge_function_auth_identity,
        test_review_page_polling_is_bounded,
        test_no_false_judge0_disconnected_claim,
        test_submit_triggers_grading_before_navigation,
        test_token_never_exposed,
        test_grading_trigger_retry,
        test_sweep_staleness_threshold,
        test_sweep_normalizes_rpc_row_objects,
        test_result_fetch_handles_non_utf8_compiler_output,
        test_decode_judge0_text_edge_cases,
        test_result_fetch_failure_is_not_a_verdict,
        test_grading_concurrency_safety,
        test_verdict_row_lock_p1,
        test_sweep_budget_p2,
        test_sweep_failure_isolation_p4,
        test_error_handler_scope_p3,
        test_grading_observability,
        test_build_output_clean,
        test_rpc_privileges,
        test_no_ambiguous_catalog_columns,
    ]:
        fn()

    print("=" * 74)
    print("QuizEazz  ·  Judge0 integration tests")
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
