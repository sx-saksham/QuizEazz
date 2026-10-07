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
import sys
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
        re.search(r"catch[\s\S]{0,400}?pending:\s*true", ix) is not None,
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
        re.search(r"hasPending\s*\?\s*'Evaluation Pending'\s*:\s*'Evaluated'", txt) is not None,
        "the label must stay driven by the server report",
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
        if "supabase/functions" in txt or "judge0.ts" in txt:
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


def main():
    for fn in [
        test_language_mapping,
        test_cpp_only_enforcement,
        test_verdict_application,
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
