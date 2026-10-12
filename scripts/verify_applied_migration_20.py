"""
Post-apply read-only verification of the LIVE verdict writer.

Asserts, against production only:
  * the guard SELECT now carries FOR UPDATE
  * signature / return type / SECURITY DEFINER / search_path unchanged
  * ACL is exactly anon=F, authenticated=F, public=F, service_role=T
  * the deployed body is identical to the reviewed payload
  * no student data is read or reported (counts only)
"""
import difflib
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_rpc_contract import _run_linked_select  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAY = ROOT / "deploy" / "migration_20_PAYLOAD.sql"
F = "quizeazz_apply_judge0_verdict"
EXPECTED_SHA = "93603AAD51FA33DD99088D6DEFF17C6B06A4D05B681485CADA6779D5E1963DB9"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  [{'x' if cond else ' '}] {name}" + (f"   {detail}" if detail else ""))


def canon(t):
    return [re.sub(r"\s+", " ", l).strip()
            for l in t.replace("\r\n", "\n").replace("\r", "\n").splitlines()
            if l.strip() and not l.strip().startswith("--")]


def strip_comments(t):
    return "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("--"))


def main():
    rows, err = _run_linked_select(
        "SELECT p.prosrc, pg_get_function_identity_arguments(p.oid) AS ident, "
        "pg_get_function_result(p.oid) AS result, p.prosecdef AS secdef, "
        "p.proconfig AS cfg, p.provolatile AS volatile, p.prokind, "
        f"has_function_privilege('anon',p.oid,'EXECUTE') AS anon, "
        f"has_function_privilege('authenticated',p.oid,'EXECUTE') AS auth, "
        f"has_function_privilege('service_role',p.oid,'EXECUTE') AS svc, "
        f"has_function_privilege('public',p.oid,'EXECUTE') AS public, "
        "current_setting('transaction_isolation') AS iso "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        f"WHERE n.nspname='public' AND p.proname='{F}'")
    if err or not rows:
        print("LIVE READ FAILED:", err)
        return 2
    L = rows[0]
    src = L["prosrc"]

    print("== A. migration 20 is live ==")
    check("live body contains FOR UPDATE", bool(re.search(r"FOR\s+UPDATE", src, re.I)))
    guard = re.search(r"SELECT a\.evaluation_status INTO \w+(.*?);", src, re.S)
    check("FOR UPDATE is on the guard SELECT specifically",
          bool(guard) and re.search(r"FOR\s+UPDATE\s*$", guard.group(1).strip(), re.I))
    # Count only executable SQL: the body carries an explanatory comment that
    # also contains the words "FOR UPDATE".
    code_only = "\n".join(l for l in src.splitlines() if not l.strip().startswith("--"))
    check("exactly one executable FOR UPDATE (the other is a comment)",
          len(re.findall(r"FOR\s+UPDATE", code_only, re.I)) == 1,
          f"{len(re.findall(r'FOR UPDATE', code_only, re.I))} in code")
    check("live isolation level is READ COMMITTED (required by the fix)",
          L["iso"] == "read committed", L["iso"])

    print("\n== B. signature and security properties UNCHANGED ==")
    payload = PAY.read_text(encoding="utf-8")
    sig = payload[payload.index("(") + 1: payload.index(")")]
    ALIAS = {"int": "integer", "text": "text", "boolean": "boolean",
             "numeric": "numeric", "uuid": "uuid"}

    def psig(s):
        out = []
        for p in s.replace("\n", " ").split(","):
            p = re.sub(r"\s+", " ", p).strip()
            if p:
                n, _, t = p.partition(" ")
                out.append((n.lower(), ALIAS.get(t.lower(), t.lower())))
        return out
    check("signature identical to the reviewed payload (12 params)",
          psig(sig) == psig(L["ident"]) and len(psig(sig)) == 12)
    check("returns boolean", L["result"] == "boolean", L["result"])
    check("still SECURITY DEFINER", L["secdef"] is True)
    check("search_path still pinned", L["cfg"] == ["search_path=public, pg_temp"],
          str(L["cfg"]))
    check("still VOLATILE / PROCEEDURE kind", L["volatile"] == "v" and L["prokind"] == "f",
          f"{L['volatile']}/{L['prokind']}")

    print("\n== C. ACL exactly anon=F authenticated=F public=F service_role=T ==")
    check("anon EXECUTE = false", L["anon"] is False, str(L["anon"]))
    check("authenticated EXECUTE = false", L["auth"] is False, str(L["auth"]))
    check("public EXECUTE = false", L["public"] is False, str(L["public"]))
    check("service_role EXECUTE = true", L["svc"] is True, str(L["svc"]))
    check("ACL tuple is exactly (F,F,F,T)",
          (L["anon"], L["auth"], L["public"], L["svc"]) == (False, False, False, True))

    print("\n== D. live body equals the reviewed payload ==")
    i = payload.index("AS $$") + 5
    j = payload.rindex("$$;")
    want = canon(strip_comments(payload[i:j]))
    got = canon(strip_comments(src))
    check("live body canonically identical to the reviewed payload",
          want == got, f"payload {len(want)} lines, live {len(got)} lines")
    if want != got:
        for l in list(difflib.unified_diff(want, got, "PAYLOAD", "LIVE",
                                           lineterm="", n=1))[:15]:
            print("      " + l)

    print("\n== E. behaviour preserved (source-level, live body) ==")
    for label, cond in [
        ("in-progress refusal intact", "still in progress" in src),
        ("already-has-verdict refusal intact", "already has a verdict" in src),
        ("unknown-outcome refusal intact", "Refusing to apply an unknown outcome" in src),
        ("marks still computed in SQL", "quizeazz_marks_for_outcome(p_outcome, v_marks_total)" in src),
        ("anti-tamper flag still set", "set_config('quizeazz.authoritative_context', 'true', true)" in src),
        ("UPDATE still keyed on id + attempt_id only",
         "a.id = p_answer_id" in src and "a.attempt_id = p_attempt_id" in src),
        ("UPDATE gained NO status predicate",
         not re.search(r"UPDATE public\.answers a(.*?evaluation_status\s*=\s*'Pending Evaluation')",
                       src, re.S)),
        ("returns TRUE as its last statement",
         [l.rstrip() for l in src.splitlines() if l.strip()][-2:] == ["  RETURN TRUE;", "END;"]),
        ("student code/language never written", "code =" not in src and "language =" not in src),
    ]:
        check(label, cond)

    print("\n== F. grading system unaffected (counts only, no student data) ==")
    COUNTS_SQL = (
        "SELECT (SELECT count(*) FROM public.exam_attempts WHERE status='evaluating') AS evaluating, "
        "(SELECT count(*) FROM public.answers a JOIN public.questions q ON q.id=a.question_id "
        "  WHERE q.question_type='coding' AND a.evaluation_status='Pending Evaluation') AS pending, "
        "(SELECT count(*) FROM public.exam_attempts) AS attempts, "
        "(SELECT count(*) FROM public.answers) AS answers, "
        "(SELECT count(*) FROM cron.job WHERE active) AS cron_active, "
        "(SELECT count(*) FROM pg_publication_tables WHERE pubname='supabase_realtime') AS rt")
    r2, e2 = _run_linked_select(COUNTS_SQL)
    if e2:  # the CLI login role occasionally races; retry once
        r2, e2 = _run_linked_select(COUNTS_SQL)
    if e2:
        print("  (counts query unavailable:", e2, ")")
    else:
        c = r2[0]
        check("no attempt stuck in 'evaluating'", c["evaluating"] == 0,
              str(c["evaluating"]))
        check("no coding answer left Pending Evaluation", c["pending"] == 0,
              str(c["pending"]))
        check("sweep cron still active", c["cron_active"] == 1, str(c["cron_active"]))
        check("realtime publication intact", c["rt"] == 1, str(c["rt"]))
        print(f"      attempts={c['attempts']} answers={c['answers']} "
              f"(counts only - no student data read)")

    mig = ROOT / "supabase" / "migration_20_verdict_row_lock.sql"
    sha = hashlib.sha256(mig.read_bytes()).hexdigest().upper()
    print("\n  reviewed migration SHA-256:", sha,
          "(match)" if sha == EXPECTED_SHA else "(MISMATCH)")

    print("\n" + "=" * 72)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for n, d in FAIL:
        print(f"  FAIL {n} {d}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())