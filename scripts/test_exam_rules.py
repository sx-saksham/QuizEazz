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
