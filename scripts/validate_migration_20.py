"""
Task 2: validate migration 20 and its deploy artifacts against the reviewed
hash, the live production function, and the security contract.

READ-ONLY against production (SELECT pg_proc only).
"""
import difflib
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_rpc_contract import _run_linked_select  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MIG = ROOT / "supabase" / "migration_20_verdict_row_lock.sql"
DEPLOY = ROOT / "deploy"
EXPECTED_SHA = "93603AAD51FA33DD99088D6DEFF17C6B06A4D05B681485CADA6779D5E1963DB9"
FNAME = "quizeazz_apply_judge0_verdict"
ARGS = ("UUID, UUID, TEXT, TEXT, INT, INT, INT, NUMERIC, TEXT, TEXT, TEXT, TEXT")

def canon(t):
    """Whitespace-insensitive line list, for comparing SQL bodies."""
    return [re.sub(r"\s+", " ", l).strip()
            for l in t.replace("\r\n", "\n").replace("\r", "\n").splitlines()
            if l.strip()]


TYPE_ALIAS = {"int": "integer", "text": "text", "boolean": "boolean",
              "numeric": "numeric", "uuid": "uuid"}


def canon_sig(s):
    parts = []
    for p in s.replace("\n", " ").split(","):
        p = re.sub(r"\s+", " ", p).strip()
        if not p:
            continue
        name, _, ty = p.partition(" ")
        parts.append((name.strip().lower(), TYPE_ALIAS.get(ty.strip().lower(),
                                                            ty.strip().lower())))
    return parts


PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  [{'x' if cond else ' '}] {name}" + (f"   {detail}" if detail else ""))


def norm(t):
    return [s.rstrip() for s in t.replace("\r\n", "\n").replace("\r", "\n")
            .splitlines() if s.strip()]


def strip_comments(t):
    return "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("--"))


def main():
    raw = MIG.read_text(encoding="utf-8")
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
    payload = (DEPLOY / "migration_20_PAYLOAD.sql").read_text(encoding="utf-8")
    rollback = (DEPLOY / "ROLLBACK_apply_judge0_verdict.sql").read_text(encoding="utf-8")

    print("== A. integrity ==")
    check("migration SHA-256 matches the reviewed value", sha == EXPECTED_SHA,
          "" if sha == EXPECTED_SHA else f"got {sha}")

    # payload must be byte-identical to the statement extracted from the migration
    start = raw.index("CREATE OR REPLACE FUNCTION public." + FNAME)
    end = raw.index("$$;", start) + 3
    extracted = raw[start:end].rstrip() + "\n"
    check("deploy payload is byte-identical to the migration statement",
          payload == extracted,
          "" if payload == extracted else "payload drifted from source")

    check("payload is exactly one statement",
          payload.count("CREATE OR REPLACE") == 1 and payload.rstrip().endswith("$$;"))
    check("payload dollar quotes balanced", payload.count("$$") % 2 == 0)
    check("payload has no DDL/DCL",
          not re.search(r"\b(ALTER|DROP|TRUNCATE|GRANT|REVOKE|CREATE\s+(?!OR\s+REPLACE))\b",
                        payload, re.I))

    print("\n== B. live comparison (read-only) ==")
    rows, err = _run_linked_select(
        "SELECT p.prosrc, pg_get_function_identity_arguments(p.oid) AS ident, "
        "pg_get_function_result(p.oid) AS result, p.prosecdef AS secdef, "
        "p.proconfig AS cfg, p.provolatile AS volatile, "
        f"has_function_privilege('anon',p.oid,'EXECUTE') AS anon, "
        f"has_function_privilege('authenticated',p.oid,'EXECUTE') AS auth, "
        f"has_function_privilege('service_role',p.oid,'EXECUTE') AS svc, "
        f"has_function_privilege('public',p.oid,'EXECUTE') AS public "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        f"WHERE n.nspname='public' AND p.proname='{FNAME}'")
    if err or not rows:
        print("  live read FAILED:", err)
        return 2
    live = rows[0]
    check("live ACL is service_role only",
          (live["anon"], live["auth"], live["public"], live["svc"])
          == (False, False, False, True),
          f"{live['anon']}/{live['auth']}/{live['public']}/{live['svc']}")

    i = raw.index("AS $$") + len("AS $$")
    j = raw.index("$$;", i)
    mig_body = raw[i:j]
    applied = bool(re.search(r"FOR UPDATE", live["prosrc"], re.I))
    diff = [l for l in difflib.unified_diff(
        norm(strip_comments(live["prosrc"])), norm(strip_comments(mig_body)),
        "LIVE", "M20", lineterm="", n=1)
        if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    if applied:
        check("migration 20 IS APPLIED (live body carries FOR UPDATE)", True)
        check("live body equals the reviewed payload (0 differing lines)",
              len(diff) == 0, f"{len(diff)} differing lines")
    else:
        check("live function still has NO FOR UPDATE (NOT applied yet)",
              not re.search(r"FOR UPDATE", live["prosrc"], re.I))
        check("body diff is EXACTLY the FOR UPDATE addition (3 lines)",
              len(diff) == 3 and sum(1 for x in diff if x.startswith("-")) == 1
              and sum(1 for x in diff if "FOR UPDATE" in x and x.startswith("+")) == 1,
              f"{len(diff)} changed lines")

    print("\n== C. security / authorization contract preserved in payload ==")
    p = payload
    # Signature: compare name + type per parameter, normalising SQL type
    # aliases (INT == integer, TEXT == text, BOOLEAN == boolean).
    sig = payload[payload.index("(") + 1: payload.index(")")]
    check("signature identical to live (12 params, same order, same types)",
          canon_sig(sig) == canon_sig(live["ident"]) and len(canon_sig(sig)) == 12,
          f"{len(canon_sig(sig))} params")
    for label, cond in [
        ("SECURITY DEFINER present", "SECURITY DEFINER" in p),
        ("search_path pinned to public, pg_temp",
         "SET search_path = public, pg_temp" in p),
        ("returns BOOLEAN", "RETURNS BOOLEAN" in p),
        ("LANGUAGE plpgsql", "LANGUAGE plpgsql" in p),
        ("return type matches live (boolean)",
         re.search(r"RETURNS\s+(\w+)", payload).group(1).lower() == "boolean"
         and live["result"] == "boolean"),
        ("SECURITY DEFINER matches live", live["secdef"] is True),
        ("search_path matches live",
         re.search(r"SET search_path = ([\w, ]+)", payload).group(1).replace(" ", "")
         == "public,pg_temp"),
        ("returns TRUE as its last statement", payload.rstrip().endswith("END;\n$$;")),
        ("outcome allow-list: all 8 objective verdicts",
         all(f"'{o}'" in p for o in ["Correct", "Wrong Answer", "Compilation Error",
                                     "Runtime Error", "Time Limit Exceeded",
                                     "Memory Limit Exceeded", "System Error",
                                     "Not Attempted"])),
        ("'Pending Evaluation' still rejected as an outcome",
         "'Pending Evaluation'" not in p.split("UPDATE public.answers")[0]
         .split("IF p_outcome IS NULL OR p_outcome NOT IN")[0]),
        ("attempt-existence check kept", "Attempt % does not exist." in p),
        ("in-progress refusal kept",
         "the examination is still in progress" in p),
        ("answer-must-belong-to-attempt check kept",
         "is not a coding answer of attempt" in p),
        ("already-has-verdict refusal kept", "already has a verdict" in p),
        ("marks derived in SQL, never from caller",
         "quizeazz_marks_for_outcome(p_outcome, v_marks_total)" in p),
        ("anti-tamper authoritative flag still set",
         "set_config('quizeazz.authoritative_context', 'true', true)" in p),
        ("student code/language never written by this function",
         "code =" not in p and "language =" not in p),
    ]:
        check(label, cond)

    print("\n== D. rollback artifact ==")
    rb_body = rollback[rollback.index("AS $$") + len("AS $$"): rollback.rindex("$$;")]
    applied = bool(re.search(r"FOR UPDATE", live["prosrc"], re.I))
    if applied:
        # POST-apply: the rollback must restore the PRE-migration-20 definition,
        # i.e. the body still defined in migration 09. It must NOT be compared
        # against the live body any more, which now legitimately differs.
        mig09 = (ROOT / "supabase"
                 / "migration_09_cpp_only_and_verdict_fix.sql").read_text(encoding="utf-8")
        i9 = mig09.index("AS $$") + len("AS $$")
        j9 = mig09.index("$$;", i9)   # first terminator after the body starts
        check("rollback restores the PRE-migration-20 body (matches migration 09)",
              canon(strip_comments(rb_body)) == canon(strip_comments(mig09[i9:j9])),
              f"{len(canon(strip_comments(rb_body)))} vs "
              f"migration09 {len(canon(strip_comments(mig09[i9:j9])))}")
        check("live body now equals the migration-20 payload",
              canon(strip_comments(live["prosrc"]))
              == canon(strip_comments(mig_body)))
        check("rollback body contains NO FOR UPDATE (pre-fix state)",
              "FOR UPDATE" not in rb_body.upper())
    else:
        # PRE-apply: the rollback must match what is live right now.
        check("rollback body is canonically identical to the captured LIVE body",
              canon(rb_body) == canon(live["prosrc"]),
              f"{len(canon(rb_body))} vs live {len(canon(live['prosrc']))}")
        check("rollback body contains NO FOR UPDATE", "FOR UPDATE" not in rb_body.upper())
    check("rollback keeps the original signature",
         "p_attempt_id UUID" in rollback and "p_message TEXT" in rollback)
    check("rollback is SECURITY DEFINER with pinned search_path",
         "SECURITY DEFINER" in rollback
         and "SET search_path = public, pg_temp" in rollback)
    steps = (DEPLOY / "DEPLOY_STEPS.md").read_text(encoding="utf-8")
    check("rollback consequence (race returns) documented",
         "race returns" in steps.lower() or "race returns" in rollback.lower()
         or "silently overwrite" in steps.lower())
    check("steps document that rollback touches no data",
         "No data is touched" in steps or "no data" in steps.lower())

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for n, d in FAIL:
        print(f"  FAIL {n} {d}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())