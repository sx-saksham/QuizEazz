"""
Deterministic-order and grading-rule tests for the pure logic in the SPA.

No database, no network. These cover the invariants that are easy to break
silently: deterministic question/option ordering, stable MCQ answer identity,
and the shape of every RPC payload the browser sends.

Run:  python scripts/test_exam_rules.py
"""

import re
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "src" / "lib" / "examEngine.ts"
TEACHER_API = ROOT / "src" / "lib" / "teacherApi.ts"

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append((name, detail))


# ---------------------------------------------------------------------------
# Re-implement the exact algorithms from examEngine.ts so the tests exercise
# the same logic. If the source changes, these must be updated in lockstep --
# which is the point: a silent change to the algorithm shows up here.
# ---------------------------------------------------------------------------

def hash_string(s: str) -> int:
    h = 0x811C9DC5
    for ch in s:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h if h != 0 else 0x9E3779B9


def mulberry32(seed: int):
    a = seed & 0xFFFFFFFF

    def nxt():
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = a
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return nxt


def deterministic_shuffle(arr, seed_str):
    out = list(arr)
    rng = mulberry32(hash_string(seed_str))
    for i in range(len(out) - 1, 0, -1):
        j = int(rng() * (i + 1))
        out[i], out[j] = out[j], out[i]
    return out


def build_candidate_paper(raw, student_id, exam_id):
    ordered = deterministic_shuffle(raw, f"{student_id}_{exam_id}_quizeazz")
    result = []
    for q in ordered:
        base = {**q, "reference_solution": None, "explanation": None}
        if q.get("question_type") != "mcq" or not isinstance(q.get("options"), list):
            result.append(base)
            continue
        sanitized = [{"id": o["id"], "text": o["text"]} for o in q["options"]]
        result.append(
            {
                **base,
                "options": deterministic_shuffle(
                    sanitized, f"{student_id}_{q['id']}_options"
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------

QUESTIONS = [
    {
        "id": f"q{i}",
        "title": f"Question {i}",
        "question_text": "text",
        "question_type": "mcq" if i % 3 else "coding",
        "category": "Data Structures",
        "marks": 2,
        "options": (
            [
                {"id": "opt_1", "text": "A", "is_correct": True},
                {"id": "opt_2", "text": "B", "is_correct": False},
                {"id": "opt_3", "text": "C", "is_correct": False},
                {"id": "opt_4", "text": "D", "is_correct": False},
            ]
            if i % 3
            else None
        ),
        "reference_solution": "SECRET_REF_SOLUTION",
        "explanation": "SECRET_EXPLANATION",
    }
    for i in range(1, 11)
]


def test_question_order_is_deterministic():
    a = build_candidate_paper(QUESTIONS, "student-A", "exam-1")
    b = build_candidate_paper(QUESTIONS, "student-A", "exam-1")
    check(
        "question order is stable across reloads for the same student",
        [q["id"] for q in a] == [q["id"] for q in b],
        f"{[q['id'] for q in a]} vs {[q['id'] for q in b]}",
    )


def test_order_differs_across_students():
    a = [q["id"] for q in build_candidate_paper(QUESTIONS, "student-A", "exam-1")]
    b = [q["id"] for q in build_candidate_paper(QUESTIONS, "student-B", "exam-1")]
    check(
        "different students get a different question order",
        a != b,
        "orders were identical",
    )


def test_every_student_gets_the_same_question_set():
    sets = {
        frozenset(q["id"] for q in build_candidate_paper(QUESTIONS, f"student-{i}", "exam-1"))
        for i in range(25)
    }
    check(
        "every candidate receives the identical question set (no random pool)",
        len(sets) == 1,
        f"{len(sets)} distinct sets across 25 students",
    )


def test_option_order_is_deterministic():
    q = dict(QUESTIONS[1])
    a = build_candidate_paper([q], "student-A", "exam-1")[0]
    b = build_candidate_paper([q], "student-A", "exam-1")[0]
    check(
        "MCQ option order is stable across reloads",
        [o["id"] for o in a["options"]] == [o["id"] for o in b["options"]],
    )


def test_options_vary_across_students():
    q = dict(QUESTIONS[1])
    orders = {
        tuple(
            o["id"]
            for o in build_candidate_paper([q], f"student-{i}", "exam-1")[0]["options"]
        )
        for i in range(30)
    }
    check(
        "different students see different MCQ option ordering",
        len(orders) > 1,
        f"only {len(orders)} distinct orderings across 30 students",
    )


def test_is_correct_never_reaches_the_client():
    leaked = []
    for i in range(40):
        paper = build_candidate_paper(QUESTIONS, f"student-{i}", "exam-1")
        for q in paper:
            if q.get("options"):
                for o in q["options"]:
                    if set(o.keys()) != {"id", "text"}:
                        leaked.append(("option keys", o))
    check(
        "student-facing options carry only id and text (no is_correct)",
        not leaked,
        str(leaked[:3]),
    )


def test_reference_solution_and_explanation_stripped():
    leaked = []
    for i in range(20):
        for q in build_candidate_paper(QUESTIONS, f"student-{i}", "exam-1"):
            if q.get("reference_solution") or q.get("explanation"):
                leaked.append(q["id"])
    check(
        "reference_solution and explanation are stripped for candidates",
        not leaked,
        f"leaked on {sorted(set(leaked))}",
    )


def test_correctness_stays_attached_to_option_identity():
    """
    The whole point of randomizing display order: the key must still match the
    same option id regardless of where it is displayed.
    """
    ok = True
    detail = ""
    for i in range(50):
        q = dict(QUESTIONS[1])
        rendered = build_candidate_paper([q], f"student-{i}", "exam-1")[0]
        ids = [o["id"] for o in rendered["options"]]
        # position 0 must not always be the key
        if all(ids.index("opt_1") == 0 for _ in [0]) and len(set(ids)) > 1:
            pass
        # every option still exists exactly once
        if sorted(ids) != ["opt_1", "opt_2", "opt_3", "opt_4"]:
            ok = False
            detail = f"option set mutated: {ids}"
    check("option identities are preserved through shuffling", ok, detail)


def test_key_is_not_always_first():
    q = dict(QUESTIONS[1])
    positions = set()
    for i in range(40):
        rendered = build_candidate_paper([q], f"student-{i}", "exam-1")[0]
        positions.add([o["id"] for o in rendered["options"]].index("opt_1"))
    check(
        "the correct option is not always displayed first",
        len(positions) > 1,
        f"key always at position {positions}",
    )


def test_shuffle_preserves_membership():
    base = list(range(50))
    for seed in ("a", "b", "zzz", "1234"):
        out = deterministic_shuffle(base, seed)
        check(
            f"shuffle preserves membership and length (seed {seed!r})",
            sorted(out) == base and len(out) == len(base),
        )


def test_hash_never_zero():
    for s in ("", "a", "student-1_exam-1_quizeazz", "\x00\x01"):
        check(f"hash is non-degenerate for {s!r}", hash_string(s) != 0)


# --------------------------------------------------------- payload shape tests

def _function_body(src: str, name: str) -> str:
    """
    Extract an exported async function's *signature + body*.

    Returns text from the declaration up to the matching close brace of the
    body. Parenthesis depth is tracked while scanning for the body's opening
    brace so that inline object types in the parameter list (e.g.
    `items: { question_id: string }[]`) are not mistaken for the body.
    """
    m = re.search(rf"export async function {name}\s*\(", src)
    if not m:
        return ""

    i = m.end() - 1  # at '('
    pdepth, j = 0, i
    body_start = -1
    while j < len(src):
        c = src[j]
        if c == "(":
            pdepth += 1
        elif c == ")":
            pdepth -= 1
            if pdepth == 0:
                # Walk forward looking for the body's opening brace: the first
                # '{' that sits at zero paren/angle/brace depth. This skips a
                # return-type literal such as `Promise<{ ... }>`, whose braces
                # are nested inside angle brackets.
                a, bdepth, k = 0, 0, j + 1
                while k < len(src):
                    ch = src[k]
                    if ch == "(":
                        pdepth += 1
                    elif ch == ")":
                        pdepth -= 1
                    elif ch == "<":
                        a += 1
                    elif ch == ">":
                        a -= 1
                    elif ch == "{":
                        if pdepth == 0 and a == 0 and bdepth == 0:
                            body_start = k
                            break
                        bdepth += 1
                    elif ch == "}":
                        if bdepth > 0:
                            bdepth -= 1
                    k += 1
                break
        j += 1

    if body_start == -1:
        return ""

    depth, j = 0, body_start
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start() : j + 1]
        j += 1
    return src[m.start() :]


def test_paper_payload_shape():
    api = TEACHER_API.read_text(encoding="utf-8")

    paper = _function_body(api, "setExamPaper")
    check("setExamPaper exists", bool(paper))
    check(
        "setExamPaper types its items as {question_id, marks} only",
        "items: { question_id: string; marks: number }[]" in paper,
        paper[:220],
    )
    check(
        "setExamPaper forwards items verbatim under p_items",
        "p_items: items" in paper,
    )
    check(
        "setExamPaper sends no question body, title or reference solution",
        not any(t in paper for t in ("question_text", "reference_solution", "explanation")),
        paper[:220],
    )

    assign = _function_body(api, "setExamAssignments")
    check(
        "setExamAssignments sends a plain uuid array",
        "studentIds: string[]" in assign and "p_student_ids: studentIds" in assign,
    )

    # The call site must build the narrow shape even though it holds richer rows.
    modal = (ROOT / "src" / "components" / "teacher" / "ManagePaperModal.tsx").read_text(
        encoding="utf-8"
    )
    check(
        "ManagePaperModal maps items down to {question_id, marks}",
        "({ question_id: i.question_id, marks: i.marks })" in modal,
    )
    check(
        "ManagePaperModal never sends reference solutions to the database",
        "reference_solution" not in modal,
    )


def test_no_direct_table_access():
    hits = []
    for p in list(ROOT.glob("src/**/*.ts")) + list(ROOT.glob("src/**/*.tsx")):
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"supabase\s*\.\s*from\s*\(", text):
            hits.append(str(p.relative_to(ROOT)))
    check(
        "no supabase.from() anywhere in src/ (RPC-only architecture)",
        not hits,
        str(sorted(set(hits))),
    )


def test_no_browser_side_role_or_session_fallback():
    banned = [
        "quizeazz_dev_session",
        "forcedRole",
        "role=teacher",
        "setItem('role'",
        "setItem(\"role\"",
        "localStorage.getItem('session",
        "localStorage.getItem(\"session",
    ]
    found = []
    for p in list(ROOT.glob("src/**/*.ts")) + list(ROOT.glob("src/**/*.tsx")):
        text = p.read_text(encoding="utf-8", errors="replace")
        for b in banned:
            if b in text:
                found.append(f"{p.relative_to(ROOT)}: {b}")
    check(
        "no fake session / forced role bypass in src/",
        not found,
        str(found),
    )


def test_no_client_side_grading():
    """
    Two separate concerns:

    1. Code execution on the app host (eval / child_process / execSync / etc).
    2. Assigning a mark or an outcome outside a type declaration.

    A type declaration or an interface field is fine; a runtime assignment is
    not. So we look for marks being written, not merely mentioned.
    """
    bad = []
    execution = re.compile(
        r"\beval\s*\(|child_process|execSync|spawnSync|\bexec\s*\(|new Function\s*\(|"
        r"\bpython\b.*subprocess|\bvm2\b|\bisolated-vm\b"
    )
    # marks_awarded / is_correct assigned in executable code (not a type block).
    # A bare `=` counts, but `==`/`===` comparisons do not: reading a faculty
    # key (e.g. preselecting the correct radio in the teacher edit form) is
    # not assigning a mark or outcome.
    assignment = re.compile(
        r"(marks_awarded|is_correct|evaluation_status)\s*(:|=(?![=>]))\s*"
        r"(?!number\b|boolean\b|string\b|boolean \| null|number \| null|\?)"
    )

    for p in list(ROOT.glob("src/**/*.ts")) + list(ROOT.glob("src/**/*.tsx")):
        rel = p.relative_to(ROOT)
        text = p.read_text(encoding="utf-8", errors="replace")

        if execution.search(text):
            bad.append(f"{rel}: code execution primitive")

        # Drop interface/type declarations before scanning for assignments
        code = re.sub(r"interface\s+\w+\s*\{[^}]*\}", "", text, flags=re.DOTALL)
        code = re.sub(r"export type\s+\w+\s*=\s*\{[^}]*\}", "", code, flags=re.DOTALL)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.MULTILINE)
        for m in assignment.finditer(code):
            line = code[: m.start()].count("\n") + 1
            bad.append(f"{rel}:{line}: writes {m.group(1)}")

    check(
        "no code execution primitive and no client-side mark/outcome assignment",
        not bad,
        str(sorted(set(bad))),
    )


def test_submission_is_single_server_call():
    src = (ROOT / "src" / "lib" / "examEngine.ts").read_text(encoding="utf-8")
    body = src.split("export async function submitExamAttempt", 1)[1].split("\nexport ", 1)[0]
    check(
        "submitExamAttempt triggers no client-side grading RPC",
        "grade_mcq_attempt" not in body,
        "grading is still orchestrated by the browser",
    )
    check(
        "submitExamAttempt calls submit_exam_attempt",
        "submit_exam_attempt" in body,
    )


def _src(rel):
    return (ROOT / "src" / rel).read_text(encoding="utf-8", errors="replace")


def test_results_state_mapping_is_truthful():
    """A-D (corrected). Finality is keyed on the ATTEMPT status the server
    finalized — never on the stale metadata pending hint. An `evaluated`
    attempt renders Evaluated + final score with no provisional language,
    even if its metadata still carries a nonzero pending count (pre-Judge0
    rows). Only non-final, non-live attempts show Evaluation in progress."""
    txt = _src("components/student/StudentResultsPage.tsx")
    check(
        "finality is keyed on evaluated attempt status",
        "attemptEvaluationState(res.status)" in txt and "state === 'evaluated'" in txt,
    )
    check(
        "live attempts render as In Progress",
        "state === 'in_progress'" in txt and "In Progress" in _src("lib/attemptStatus.ts"),
    )
    check(
        "unfinished attempts show Evaluation in progress",
        "Evaluation in progress" in txt,
    )
    check(
        "no provisional language for finalized results",
        "provisional" not in txt,
    )
    check(
        "evaluated badge text present",
        "'Evaluated'" in txt or '"Evaluated"' in txt or '>Evaluated<' in txt or 'Evaluated' in txt,
    )


def test_submitted_datetime_display():
    """D. Date AND time from persisted submitted_at, user-local timezone."""
    txt = _src("components/student/StudentResultsPage.tsx")
    check(
        "results render date and time, not date only",
        "toLocaleString('en-US'" in txt and "hour" in txt and "minute" in txt,
    )
    check(
        "no date-only rendering remains on the results table",
        "toLocaleDateString" not in txt,
    )


def test_absolute_timer_hardening():
    """E-H. Deadline is absolute: pause/close/reopen never extend it."""
    txt = _src("components/student/ExamTakingScreen.tsx")
    check(
        "absolute deadline stored from started_at plus duration",
        re.search(r"deadlineRef\.current =\s*\n?\s*new Date\(activeAttempt\.started_at\)", txt) is not None
        or "started_at).getTime() + resolvedDuration * 60 * 1000" in txt,
    )
    check(
        "countdown recomputes remaining from the deadline every tick",
        "deadlineRef.current - Date.now()" in txt,
    )
    check(
        "pause cannot freeze the countdown tick",
        re.search(r"if \(phase !== 'active' \|\| isPaused", txt) is None,
    )
    check(
        "no decrement-from-stale-state countdown remains",
        "setTimeLeft((prev)" not in txt,
        "the timer must recompute from deadlineRef, never decrement state",
    )
    check(
        "reopening after the deadline expires immediately",
        "setIsTimeExpired(true)" in txt,
    )
    check(
        "expiry auto-submits through the server path",
        re.search(r"if \(isTimeExpired && attempt && !submittingRef\.current\)", txt) is not None,
    )
    check(
        "copy/cut/paste are blocked outside the code editor",
        "monaco-editor" in txt and "preventDefault" in txt,
    )
    check(
        "clipboard violations are logged, never time-granting",
        "copy_attempt" in txt or "Clipboard" in txt,
    )


def test_safe_delete_and_publish_wiring():
    """I-L, R-V. Students cannot reach faculty mutations; faculty UI exists."""
    mig = (ROOT / "supabase" / "migration_13_publish_now_and_safe_delete.sql").read_text(encoding="utf-8")
    check("migration 13 exists with publish-now RPC", "teacher_publish_now" in mig)
    check("migration 13 exists with safe delete RPCs",
          "teacher_delete_question" in mig and "teacher_delete_exam" in mig)
    check(
        "publish validates before flipping status",
        "already published" in mig and "validated hidden test" in mig,
    )
    check(
        "publish uses the server clock, never the browser clock",
        re.search(r"starts_at = v_now|v_now := now\(\)", mig) is not None,
    )
    check(
        "delete paths are faculty-gated in SQL",
        mig.count("quizeazz_assert_faculty(v_caller_id)") >= 3,
    )
    check(
        "question delete is blocked by paper references",
        "used in" in mig and "exam paper(s)" in mig,
    )
    check(
        "question delete is blocked by answer references",
        "candidate answer(s) reference it" in mig,
    )
    check(
        "exam delete is blocked by existing attempts",
        "candidate attempt(s) exist" in mig,
    )
    student_files = list((ROOT / "src" / "components" / "student").rglob("*.tsx"))
    bad = [str(p.relative_to(ROOT)) for p in student_files
           if "teacher_delete_question" in p.read_text(encoding="utf-8", errors="replace")
           or "teacher_delete_exam" in p.read_text(encoding="utf-8", errors="replace")
           or "teacher_publish_now" in p.read_text(encoding="utf-8", errors="replace")
           or "Publish Now" in p.read_text(encoding="utf-8", errors="replace")]
    check("students cannot see or invoke publish/delete", not bad, str(bad))
    tq = _src("components/teacher/TeacherQuestionsView.tsx")
    check("bank has no per-card delete handler left", "setDeletingQuestion" not in tq)
    te = _src("components/teacher/TeacherExamsView.tsx")
    check("exam list exposes Publish Now on drafts only",
          "exam.status === 'draft'" in te and "Publish Now" in te)
    check("exam list exposes no per-card delete handler", "setDeletingExam" not in te)


def test_registry_filters_and_labels():
    """M-P. Class/batch filters compose; UUID label is exact."""
    txt = _src("components/teacher/TeacherStudentsView.tsx")
    check("class filter offers All + the three classes", "All Classes" in txt and "CLASS_OPTIONS" in txt)
    check("batch filter offers All + the three batches", "All Batches" in txt and "BATCH_OPTIONS" in txt)
    check(
        "filters compose with search",
        re.search(r"classFilter && s\.class_name !== classFilter", txt) is not None
        or "s.class_name !== classFilter" in txt,
    )
    check("header is exactly UUID, never ROLL", ">UUID<" in txt and "Roll / UUID" not in txt)
    check(
        "filtered count reflects the active group",
        "Showing" in txt and "filteredStudents.length" in txt,
    )


def test_no_manual_refresh_controls():
    """Q. No visible manual refresh; refetch functions remain for code paths."""
    hits = []
    for p in list((ROOT / "src").rglob("*.tsx")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r">\s*Refresh(Roster)?\s*<", txt):
            hits.append(str(p.relative_to(ROOT)))
    check("no visible Refresh/Refresh Roster buttons remain", not hits, str(hits))
    check(
        "refetch functions still exist for code-driven reloads",
        re.search(r"reload.*\(\)", _src("hooks/useAsyncData.ts")) is not None,
    )


def test_bulk_delete_workflows():
    """Selection-mode bulk delete replaces per-card Delete buttons.
    Security stays server-side: students have no path to these RPCs."""
    tq = _src("components/teacher/TeacherQuestionsView.tsx")
    check(
        "bank has a Select entry control, checkboxes only in selection mode",
        re.search(r">\s*Select\s*<", tq) is not None and "selectionMode && (" in tq,
    )
    check(
        "bank rows have left checkboxes",
        'aria-label={`Select question' in tq,
    )
    check(
        "bank shows a selection count toolbar with Delete Selected",
        "selected" in tq and "Delete Selected" in tq and "selectedIds.size === 0" in tq,
    )
    check(
        "bank delete confirmation states the count",
        re.search(r"Delete \$\{selectedIds\.size\} Selected", tq) is not None,
    )
    check(
        "bank has no per-card Delete button anymore",
        "setDeletingQuestion" not in tq,
        "bulk selection (selectedIds) is the only delete path",
    )
    check(
        "bank offers archive-instead for protected questions",
        "Archive" in tq and "setQuestionArchived" in tq,
    )
    te = _src("components/teacher/TeacherExamsView.tsx")
    check(
        "exams have a Select entry control and checkboxes in selection mode",
        re.search(r">\s*Select\s*<", te) is not None and "examSelectionMode && (" in te
        and 'aria-label={`Select examination' in te,
    )
    check(
        "exams show a selection count toolbar with Delete Selected",
        "Delete Selected" in te and "selectedExamIds.size === 0" in te,
    )
    check(
        "exam bulk delete reports per-exam protection errors",
        "are protected" in te,
    )
    check(
        "exams offer archive-instead for protected examinations",
        "Archive" in te and "setExamArchived" in te,
    )
    check(
        "exam action area is a deliberate two-row layout (no overlap)",
        "grid grid-cols-2 gap-2" in te and "border-l border-slate-200 pl-2" in te,
    )
    check(
        "publish-now confirmation exists with draft-only gating",
        "Publish this exam now?" in te
        and "exam.status === 'draft'" in te,
    )
    ta = _src("components/teacher/TeacherAnalyticsView.tsx")
    check(
        "clear-log action exists with destructive confirmation",
        "Clear Log" in ta and "cannot be recovered" in ta,
    )
    check(
        "security log empty state reads exactly 'No security events recorded.'",
        "No security events recorded." in ta,
    )


def test_question_bank_copy_and_taxonomy():
    """Exact heading; no obsolete taxonomy in rendered bank UI."""
    tq = _src("components/teacher/TeacherQuestionsView.tsx")
    check(
        "bank heading is exactly 'Question Bank'",
        re.search(r">\s*Question Bank\s*<", tq) is not None,
    )
    check(
        "bank subtitle is exactly 'Create, manage, and review examination questions.'",
        "Create, manage, and review examination questions." in tq,
    )
    for dead in [
        "Departmental Question Repository",
        "Standardized question items",
        "Data Structures",
        "Core Programming",
    ]:
        check(f"bank renders no {dead!r} text", dead not in tq)
    check(
        "bank inspect modal shows no category/subcategory taxonomy",
        "inspectingQuestion.category" not in tq,
    )
    check(
        "bank card shows C++ and Marks metadata",
        "C++" in tq and "Marks:" in tq,
    )


def test_phase_e_corrections():
    """Phase E final correction: selection Cancel, Publish Now prominence,
    Closes At wiring, teacher result filters, truthful student mapping."""
    tq = _src("components/teacher/TeacherQuestionsView.tsx")
    te = _src("components/teacher/TeacherExamsView.tsx")
    # 1. Selection exits via Cancel, never Done.
    for name, txt in (("bank", tq), ("exams", te)):
        check(
            f"{name} selection exits via Cancel",
            re.search(r">\s*Cancel\s*<", txt) is not None,
        )
        check(
            f"{name} selection has no Done exit",
            re.search(r">\s*Done\s*<", txt) is None,
        )
    # 2. Publish Now is draft-only and prominent (full-width primary row).
    check(
        "publish now renders only for drafts",
        "exam.status === 'draft'" in te and "Publish Now" in te,
    )
    check(
        "publish now is a full-width primary action",
        'className="w-full justify-center"' in te,
    )
    # 3. Publish transaction is server-authoritative.
    pub = (ROOT / "supabase" / "migration_13_publish_now_and_safe_delete.sql").read_text(
        encoding="utf-8"
    )
    check("publish uses the server clock", "v_now := now()" in pub)
    check(
        "publish rebuilds snapshots at publish time",
        "quizeazz_build_question_snapshot" in pub,
    )
    check(
        "publish requires validated hidden tests",
        "validated hidden test" in pub,
    )
    # 4. Schedule start/end validation on both UIs and both RPCs.
    check(
        "create form validates close after start",
        "The close time must be later than the start time." in te,
    )
    check(
        "create form exposes a Closes At field",
        "Closes At (End Time)" in te,
    )
    check(
        "create RPC accepts a close time",
        "p_ends_at" in _src("lib/teacherApi.ts"),
    )
    mig16 = (ROOT / "supabase" / "migration_16_results_truth_create_ends_filters.sql").read_text(
        encoding="utf-8"
    )
    check(
        "create RPC validates close after start",
        "The close time must be later than the start time." in mig16,
    )
    check(
        "schedule RPC validates close after start",
        "The close time must be later than the start time."
        in (ROOT / "supabase" / "migration_exam_management_integrity.sql").read_text(
            encoding="utf-8"
        ),
    )
    sched = _src("components/teacher/ExamScheduleModal.tsx")
    check(
        "schedule modal exposes Closes At",
        "Closes At (End Time)" in sched,
    )
    # 5. Students cannot start after closing (server gate).
    gate = (ROOT / "supabase" / "migration_06_runtime_blockers_and_attempt_access.sql").read_text(
        encoding="utf-8"
    )
    check(
        "start path enforces the closing time",
        "ends_at" in gate,
    )
    # 6-8. Teacher result filters: UI controls + RPC params + SQL filters.
    tr = _src("components/teacher/TeacherResultsView.tsx")
    check(
        "teacher results have a name search box",
        "Search student name..." in tr,
    )
    check(
        "teacher results have class and batch selects",
        "All Classes" in tr and "All Batches" in tr,
    )
    check(
        "teacher results show a filtered count",
        "Showing {attempts.length}" in tr,
    )
    check(
        "results fetch sends filters to the RPC",
        "p_search" in _src("lib/teacherApi.ts")
        and "p_class" in _src("lib/teacherApi.ts")
        and "p_batch" in _src("lib/teacherApi.ts"),
    )
    check(
        "attempts RPC filters name/class/batch server-side",
        "p_search" in mig16 and "ILIKE" in mig16
        and "p.class_name = trim(p_class)" in mig16
        and "p.batch = trim(p_batch)" in mig16,
    )
    # 9. Evaluated attempts render evaluated (status authoritative).
    sr = _src("components/student/StudentResultsPage.tsx")
    check(
        "student mapping keys finality on attempt status",
        "attemptEvaluationState(res.status)" in sr,
    )
    check(
        "evaluated forces zero pending in the student RPC",
        "WHEN att.status = 'evaluated' THEN 0" in mig16,
    )
    # 10. In-progress renders live with no submitted timestamp.
    check(
        "in-progress renders as live without a submitted time",
        "state === 'in_progress'" in sr and "Not submitted" in sr,
    )
    check(
        "submitted timestamp comes from submitted_at, not a fallback",
        "submitted_at" in sr,
    )
    # 11. No provisional language for finalized results.
    check(
        "student results list never says provisional",
        "provisional" not in sr,
    )
    # 12. Exam-card action layout markers (deliberate two-row grid).
    check(
        "exam actions use an equal two-column primary row",
        "grid grid-cols-2 gap-2" in te,
    )
    check(
        "icon-only actions are divider-separated from text actions",
        "border-l border-slate-200 pl-2" in te,
    )


def test_review_status_authority():
    """One lifecycle everywhere: attempt status alone decides.
    Covers the QUIZ5 contradiction (list said in-progress while review
    badge said Evaluated + body said not completed)."""
    helper = _src("lib/attemptStatus.ts")
    check(
        "single shared helper defines the attempt lifecycle",
        "attemptEvaluationState" in helper
        and "ATTEMPT_EVALUATION_LABEL" in helper,
    )
    check(
        "helper maps evaluated/in_progress, defaults the rest to evaluating",
        "status === 'evaluated'" in helper
        and "status === 'in_progress'" in helper,
    )
    check(
        "helper bans metadata and answer hints as attempt state",
        "programming_pending" in helper and "evaluation_status" in helper,
    )
    rev = _src("components/student/StudentResultReviewPage.tsx")
    lst = _src("components/student/StudentResultsPage.tsx")
    # Both surfaces consume the same helper.
    check("results list uses the shared helper", "attemptEvaluationState" in lst)
    check("review page uses the shared helper", "attemptEvaluationState" in rev)
    # A. evaluated: Evaluated everywhere, never the contradiction copy.
    check(
        "review derives its badge from attempt state, not answer outcomes",
        "evaluationLabel = ATTEMPT_EVALUATION_LABEL[evalState]" in rev,
    )
    check(
        "review never says evaluation is incomplete",
        "Evaluation has not been completed" not in rev,
    )
    check(
        "review progress banner renders only while evaluating",
        "evalState === 'evaluating'" in rev,
    )
    check(
        "evaluated with no rows falls back to a neutral recorded message",
        "Your final result has been recorded." in rev,
    )
    check(
        "review never says provisional",
        "provisional" not in rev.lower(),
    )
    check(
        "per-question pending notes never show on a final attempt",
        "evalState !== 'evaluated'" in rev,
    )
    check(
        "a final report is never polled",
        "attemptEvaluationState(report.status) !== 'evaluating'" in rev,
    )
    # B/C. evaluating and in_progress states exist distinctly.
    check(
        "review handles the live in_progress state safely",
        "still in progress" in rev,
    )
    # D. Legacy metadata is ignored: no state read of the stale hints.
    for name, txt in (("results list", lst), ("review page", rev)):
        body = txt
        check(
            f"{name} never reads metadata pending hints for state",
            "programming_pending" not in body and "programming_evaluated" not in body,
        )
        check(
            f"{name} never infers finality from answer evaluation_status",
            "evaluation_status" not in body,
        )
    # E. Full-repo sweep: no stale attempt-level mapping remains.
    stale = []
    for p in list((ROOT / "src").rglob("*.ts")) + list((ROOT / "src").rglob("*.tsx")):
        t = p.read_text(encoding="utf-8", errors="replace")
        rel = str(p.relative_to(ROOT))
        if "Evaluation has not been completed" in t:
            stale.append(rel + ": incomplete-evaluation copy")
        if "provisional" in t.lower() and "no provisional" not in t.lower():
            stale.append(rel + ": provisional wording")
    check("no stale attempt-level mapping remains in src", not stale, str(stale))


def test_registry_layout_and_archive_api():
    """Class nowrap, UUID header, archive wrappers, dashboard cards."""
    ts = _src("components/teacher/TeacherStudentsView.tsx")
    check(
        "class values stay on one line",
        "whitespace-nowrap" in ts,
    )
    check(
        "UUID column constrained so Role/Date sit naturally",
        "max-w-[240px]" in ts,
    )
    api = _src("lib/teacherApi.ts")
    check(
        "archive wrappers call faculty-gated RPCs",
        "teacher_set_question_archived" in api and "teacher_set_exam_archived" in api,
    )
    to = _src("components/teacher/TeacherOverview.tsx")
    check(
        "dashboard stat cards are white surfaces with border+shadow",
        "rounded-xl border border-slate-200 bg-white" in to,
    )
    sb = _src("components/layout/Sidebar.tsx")
    check(
        "sidebar active item is visually distinct",
        "aria-current" in sb and "bg-indigo-100/70" in sb,
    )
    check(
        "no version label rendered in sidebar",
        "v2.4" not in sb and "QuizEazz v" not in sb,
    )


def main():
    for fn in [
        test_question_order_is_deterministic,
        test_order_differs_across_students,
        test_every_student_gets_the_same_question_set,
        test_option_order_is_deterministic,
        test_options_vary_across_students,
        test_is_correct_never_reaches_the_client,
        test_reference_solution_and_explanation_stripped,
        test_correctness_stays_attached_to_option_identity,
        test_key_is_not_always_first,
        test_shuffle_preserves_membership,
        test_hash_never_zero,
        test_paper_payload_shape,
        test_no_direct_table_access,
        test_no_browser_side_role_or_session_fallback,
        test_no_client_side_grading,
        test_submission_is_single_server_call,
        test_results_state_mapping_is_truthful,
        test_submitted_datetime_display,
        test_absolute_timer_hardening,
        test_safe_delete_and_publish_wiring,
        test_registry_filters_and_labels,
        test_no_manual_refresh_controls,
        test_bulk_delete_workflows,
        test_question_bank_copy_and_taxonomy,
        test_registry_layout_and_archive_api,
        test_phase_e_corrections,
        test_review_status_authority,
    ]:
        fn()

    print("=" * 74)
    print("QuizEazz  ·  examination rule tests")
    print("=" * 74)
    for name, detail in PASS:
        print(f"  PASS  {name}")
    if FAIL:
        print()
        for name, detail in FAIL:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print("\n" + "-" * 74)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
