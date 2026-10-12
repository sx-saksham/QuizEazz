"""
Cross-check: every supabase.rpc(name, {params}) in src/ must exist in the
SQL migrations with a matching parameter name set. Catches the exact class of
bug where the frontend and the database disagree on a contract.

Also runs structural SQL checks: dollar-quote pairing, statement-level
parenthesis balance, and plpgsql block balance (END IF / END LOOP / END CASE
are distinguished from a bare block END).

Read-only. Run:  python scripts/verify_rpc_contract.py

The default run is fully offline: it compares src/ against the migration
FILES. That is not sufficient on its own, because a migration file can be
committed and even merge-tested without ever being applied to the live
database -- the suite would go green while production still ran the old
body.

Pass --deployed to additionally read the LIVE function definitions from
pg_proc and check the deployed state directly:

    python scripts/verify_rpc_contract.py --deployed

That mode issues read-only SELECTs only. It never writes, never migrates
and never touches production data.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SQL = ROOT / "supabase"

MIGRATION_ORDER = [
    "schema_security_hardening.sql",
    "migration_exam_secure_architecture.sql",
    "migration_teacher_and_student_hardening.sql",
    "migration_exam_management_integrity.sql",
    "migration_05_rpc_privilege_hardening.sql",
    "migration_06_runtime_blockers_and_attempt_access.sql",
    "migration_07_judge0_integration.sql",
    "migration_08_judge0_marks_type_fix.sql",
    "migration_09_cpp_only_and_verdict_fix.sql",
    "migration_10_class_batch_segregation.sql",
    "migration_11_hidden_tests_semantic_grading.sql",
    "migration_12_exam_question_snapshots.sql",
    "migration_13_publish_now_and_safe_delete.sql",
    "migration_14_faculty_clear_security_log.sql",
    "migration_15_archive_flags.sql",
    "migration_16_results_truth_create_ends_filters.sql",
    "migration_17_sweep_staleness_threshold.sql",
    "migration_18_realtime_security_log.sql",
    "migration_19_clear_security_events_explicit_where.sql",
    "migration_20_verdict_row_lock.sql",
]

CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+"
    r"(?:(?P<schema>\w+)\.)?(?P<name>\w+)\s*\(",
    re.IGNORECASE,
)


# ------------------------------------------------------------------- helpers

def strip_line_comments(text: str) -> str:
    """Remove -- comments while respecting single-quoted strings."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append(text[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def match_paren(text: str, open_idx: int) -> int:
    """Index of the paren matching text[open_idx] == '('. -1 if unbalanced."""
    depth = 0
    i, n = open_idx, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def split_top_level(body: str):
    """Split on commas at depth 0, respecting quotes and nesting."""
    parts, depth, cur = [], 0, []
    i, n = 0, len(body)
    in_str = False
    while i < n:
        c = body[i]
        if in_str:
            cur.append(c)
            if c == "'":
                if i + 1 < n and body[i + 1] == "'":
                    cur.append(body[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            cur.append(c)
            i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


# -------------------------------------------------------------- SQL functions

def load_sql_functions():
    """name -> set(p_ params). Later files win, matching migration order."""
    fns = {}
    for fname in MIGRATION_ORDER:
        path = SQL / fname
        if not path.exists():
            continue
        text = strip_line_comments(path.read_text(encoding="utf-8", errors="replace"))
        for m in CREATE_RE.finditer(text):
            close = match_paren(text, m.end() - 1)
            if close == -1:
                continue
            params = set()
            for p in split_top_level(text[m.end() : close]):
                toks = p.split()
                if toks and re.fullmatch(r"p_[a-z0-9_]+", toks[0]):
                    params.add(toks[0])
            fns[m.group("name")] = params
    return fns


# --------------------------------------------------------- structural checks

def check_dollar_quotes(path: Path, text: str):
    problems = []
    if text.count("$$") % 2 != 0:
        problems.append(f"{path.name}: unbalanced $$ quoting ({text.count('$$')} markers)")
    return problems


def check_plpgsql_blocks(path: Path, text: str):
    """
    Count block nesting inside plpgsql bodies.

    Three different constructs emit the token END:
      - BEGIN ... END              (the function's main block)
      - IF ... END IF              (not a bare END)
      - LOOP ... END LOOP          (not a bare END)
      - CASE ... WHEN ... END      (procedural, uses "END CASE")
      - CASE ... WHEN ... END      (SQL expression, uses a bare "END")

    The last two both contain the word CASE, so a SQL CASE can be identified as
    a CASE token that is NOT preceded by END.
    """
    problems = []
    for idx, body in enumerate(re.findall(r"\$\$(.*?)\$\$", text, re.DOTALL), start=1):
        begins = len(re.findall(r"\bBEGIN\b", body, re.IGNORECASE))

        bare_ends = len(
            re.findall(r"\bEND\b(?!\s+(?:IF|LOOP|CASE)\b)", body, re.IGNORECASE)
        )

        # English prose inside string literals (e.g. '...hidden test case...')
        # must not count as a SQL CASE expression. Blank literals first; the
        # BEGIN/END tallies above are intentionally left literal-aware so this
        # change can only remove false CASE positives, never mask a real one.
        # (Postgres itself is the final arbiter: every migration here has
        # executed cleanly against the live database.)
        body_no_strings = re.sub(r"'(?:[^']|'')*'", "''", body)
        sql_cases = len(
            re.findall(r"(?<!END )\bCASE\b", body_no_strings, re.IGNORECASE)
        )

        closes_block = bare_ends - sql_cases
        if begins != closes_block:
            snippet = body.strip().splitlines()[0][:70] if body.strip() else ""
            problems.append(
                f"{path.name}: plpgsql body #{idx} ({snippet!r}) has {begins} BEGIN "
                f"vs {closes_block} block-closing END"
            )
    return problems


def check_statement_parens(path: Path, text: str):
    """Parens must balance outside of dollar-quoted bodies."""
    outside = re.sub(r"\$\$.*?\$\$", "", text, flags=re.DOTALL)
    problems = []
    if outside.count("(") != outside.count(")"):
        problems.append(
            f"{path.name}: unbalanced parens outside function bodies "
            f"({outside.count('(')} vs {outside.count(')')})"
        )
    # every top-level statement must terminate
    stripped = "\n".join(
        ln for ln in outside.splitlines() if ln.strip() and not ln.strip().startswith("\\")
    )
    if stripped.strip() and not stripped.rstrip().endswith(";"):
        problems.append(f"{path.name}: file does not end with ';'")
    return problems


def structural_sql_checks():
    problems = []
    for path in sorted(SQL.glob("*.sql")):
        text = strip_line_comments(path.read_text(encoding="utf-8", errors="replace"))
        problems += check_dollar_quotes(path, text)
        problems += check_plpgsql_blocks(path, text)
        problems += check_statement_parens(path, text)
    return problems


# -------------------------------------------------------- frontend rpc calls

RPC_RE = re.compile(r"\.rpc\(\s*'([a-z0-9_]+)'\s*,\s*\{", re.IGNORECASE)
RPC_BARE_RE = re.compile(r"\.rpc\(\s*'([a-z0-9_]+)'\s*\)", re.IGNORECASE)


def load_frontend_calls():
    calls = []
    paths = sorted(SRC.rglob("*.ts")) + sorted(SRC.rglob("*.tsx"))
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in RPC_RE.finditer(text):
            start = m.end() - 1
            depth, i = 0, start
            while i < len(text):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            obj = text[start : i + 1]
            calls.append(
                (m.group(1), set(re.findall(r"\b(p_[a-z0-9_]+)\s*:", obj)), str(path.relative_to(ROOT)))
            )
        for m in RPC_BARE_RE.finditer(text):
            calls.append((m.group(1), set(), str(path.relative_to(ROOT))))
    return calls


# ------------------------------------------------ deployed (live) functions

def _run_linked_select(sql: str):
    """Run ONE read-only SELECT against the linked project. Returns rows."""
    # On Windows npx is a .cmd shim, so resolve it rather than assuming.
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if npx is None:
        return None, "npx not found on PATH"
    try:
        proc = subprocess.run(
            [npx, "supabase", "db", "query", "--linked", sql],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(ROOT),
            shell=os.name == "nt",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "").strip()
    payload = proc.stdout[proc.stdout.find("{") :] if "{" in proc.stdout else proc.stdout
    try:
        import json

        return json.loads(payload).get("rows", []), None
    except (ValueError, AttributeError) as exc:
        return None, f"unparseable response: {exc}"


def deployed_function_checks():
    """
    Verify the LIVE database, not the migration files.

    Everything the offline pass checks is derived from source text. This
    pass asks the database itself what it is actually running, which is the
    only way to catch "the migration was written but never applied".
    """
    print("\n--- deployed state (--deployed) ---")
    failures = 0

    rows, err = _run_linked_select(
        "SELECT p.proname, p.prosrc FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname IN "
        "('quizeazz_apply_judge0_verdict',"
        "'quizeazz_list_attempts_needing_programming_grading');"
    )
    if err or rows is None:
        print("  FAIL  could not read live function definitions")
        print(f"        {err}")
        return 1
    live = {r["proname"]: r["prosrc"] for r in rows}

    mig20 = SQL / "migration_20_verdict_row_lock.sql"
    mig20_exists = mig20.exists()
    mig20_text = mig20.read_text(encoding="utf-8") if mig20_exists else ""

    src = live.get("quizeazz_apply_judge0_verdict")
    if src is None:
        print("  FAIL  quizeazz_apply_judge0_verdict is absent from the live database")
        return 1

    # --- P1: is the row lock actually live? ---------------------------------
    live_locked = bool(
        re.search(
            r"SELECT a\.evaluation_status INTO \w+.*?FOR UPDATE",
            src,
            re.S | re.I,
        )
    )
    if live_locked:
        print("  ok    live quizeazz_apply_judge0_verdict guards with FOR UPDATE")
    elif mig20_exists:
        print("  FAIL  live verdict writer has NO row lock, but")
        print("        migration_20_verdict_row_lock.sql exists in the repo")
        print("        -> the safeguard is NOT deployed. Apply migration 20.")
        failures += 1
    else:
        print("  FAIL  live verdict writer has NO row lock and no migration defines one")
        failures += 1

    if mig20_exists:
        guard = re.search(
            r"SELECT a\.evaluation_status INTO \w+(.*?);",
            strip_line_comments(mig20_text),
            re.S,
        )
        if guard and re.search(r"FOR\s+UPDATE\s*$", guard.group(1).strip(), re.I):
            print("  ok    migration 20 source carries FOR UPDATE on the guard SELECT")
        else:
            print("  FAIL  migration 20 source no longer carries FOR UPDATE")
            failures += 1

    # --- the trap migration 20 must not fall into ---------------------------
    upd = re.search(r"UPDATE public\.answers a(.*?);", src, re.S)
    if upd and re.search(r"evaluation_status\s*=\s*'Pending Evaluation'", upd.group(1)):
        print("  FAIL  live verdict UPDATE carries an evaluation_status predicate")
        print("        without a ROW_COUNT check it returns TRUE while updating 0 rows")
        failures += 1

    # --- behaviour that must not drift -------------------------------------
    for label, needle in [
        ("refuses a verdict on an in-progress attempt", "still in progress"),
        ("refuses to overwrite an existing verdict", "already has a verdict"),
        ("refuses an unknown outcome", "Refusing to apply an unknown outcome"),
        ("computes marks in SQL", "quizeazz_marks_for_outcome"),
    ]:
        if needle in src:
            print(f"  ok    live verdict writer {label}")
        else:
            print(f"  FAIL  live verdict writer no longer {label}")
            failures += 1

    # --- the ACL must never have widened ------------------------------------
    rows, err = _run_linked_select(
        "SELECT has_function_privilege('anon', p.oid, 'EXECUTE') AS anon, "
        "has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth, "
        "has_function_privilege('service_role', p.oid, 'EXECUTE') AS svc "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'quizeazz_apply_judge0_verdict';"
    )
    if err or not rows:
        print("  FAIL  could not read the live verdict-writer ACL")
        print(f"        {err}")
        failures += 1
    else:
        a = rows[0]
        if a["anon"] is False and a["auth"] is False and a["svc"] is True:
            print("  ok    live verdict writer ACL is service_role only")
        else:
            print(f"  FAIL  live verdict writer ACL changed: {a}")
            failures += 1

    return failures


# ---------------------------------------------------------------------- main

def main():
    check_deployed = "--deployed" in sys.argv
    print("=" * 74)
    print("QuizEazz  ·  RPC contract + SQL structure verification")
    print("=" * 74)

    fns = load_sql_functions()
    calls = load_frontend_calls()
    sql_problems = structural_sql_checks()

    print(f"\nSQL functions parsed from migrations : {len(fns)}")
    print(f"Frontend .rpc() call sites          : {len(calls)}")

    print("\n--- RPC contract: frontend vs database ---")
    failures = 0
    seen = set()
    for name, params, path in calls:
        key = (name, frozenset(params))
        if key in seen:
            continue
        seen.add(key)
        if name not in fns:
            print(f"  FAIL  {name}  ({path})")
            print("        not defined in any migration")
            failures += 1
            continue
        expected = fns[name]
        if params != expected:
            extra = sorted(params - expected)
            missing = sorted(expected - params)
            print(f"  FAIL  {name}  ({path})")
            if extra:
                print(f"        sent but not a parameter: {extra}")
            if missing:
                print(f"        parameter never sent      : {missing}")
            failures += 1
        else:
            sig = ", ".join(sorted(params))
            print(f"  ok    {name}({sig})")

    print("\n--- SQL structure ---")
    if sql_problems:
        for p in sql_problems:
            print(f"  FAIL  {p}")
        failures += len(sql_problems)
    else:
        print("  ok    dollar quoting balanced")
        print("  ok    plpgsql BEGIN/bare-END balanced in every function body")
        print("  ok    parentheses balanced outside function bodies")

    print("\n--- reachability ---")
    called = {n for n, _, _ in calls}
    internal_prefixes = ("quizeazz_", "protect_", "handle_")
    internal = {
        n
        for n in fns
        if n.startswith(internal_prefixes)
        or n in ("check_answer_write_eligibility",)
    }
    for n in sorted(internal & called):
        print(f"  note  {n}  (internal helper, must NOT be called from a browser)")
    for n in sorted(internal - called):
        print(f"  note  {n}  (trigger/definer-only, not exposed)")
    for n in sorted(set(fns) - called - internal):
        print(f"  note  {n}  (defined in SQL, not called from src/)")

    deployed_failures = 0
    if check_deployed:
        deployed_failures = deployed_function_checks()
        failures += deployed_failures
    else:
        print("\n--- deployed state ---")
        print("  skip  not requested; re-run with --deployed to verify the LIVE database")

    print("\n" + "=" * 74)
    if failures:
        print(f"RESULT: {failures} problem(s) found")
        return 1
    if check_deployed:
        print("RESULT: RPC contracts match; SQL structure balanced; LIVE database verified")
    else:
        print("RESULT: all RPC contracts match; SQL structure is balanced")
        print("        (migration files only - pass --deployed to check the live database)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
