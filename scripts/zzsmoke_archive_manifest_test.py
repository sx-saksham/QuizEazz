"""
Tests for scripts/zzsmoke_archive_manifest.py.

Every case runs the helper as a real subprocess against temporary copies.
No test touches deploy/ZZSMOKE_MANIFEST.json, deploy/archive/, or any database.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "zzsmoke_archive_manifest.py"
BASELINE = json.loads((ROOT / "deploy" / "ZZSMOKE_BASELINE.json").read_text(encoding="utf-8"))

ACTIVE = {
    "_status": "ACTIVE_FIXTURE - these ids identify live ZZSMOKE rows.",
    "_rule": "A null id means 'not yet created'.",
    "teacher_id": "dde0c3e8-61ee-4d36-a874-3b1ef8a82413",
    "teacher_email": "zzsmoke-teacher-20261012@gmail.com",
    "student_id": "1a79277a-f727-4f55-adc3-107aa00045f5",
    "student_email": "zzsmoke-student-20261012@gmail.com",
    "exam_id": "1615124b-3645-443b-b23f-7321610bfc54",
    "question_id": "3806253d-cfae-40c5-a85c-a7b181ae3f97",
    "attempt_id": "55ff839f-39ca-44de-ae80-449a11e47b8e",
}
results = []


def check(name, cond, detail=""):
    print(f"  [{'x' if cond else ' '}] {name}" + (f"\n        {detail}" if detail else ""))
    results.append(bool(cond))


def good_baseline_text():
    lines = ["=== baseline comparison ==="]
    for t, m in BASELINE.items():
        lines.append(f"  {t:34} {m['rows']} -> {m['rows']}    identical")
    lines += ["", "PASS: production is byte-identical to the pre-fixture baseline."]
    return "\n".join(lines)


def good_audit_text():
    """A clean audit: every table present, nothing changed."""
    lines = ["=== content-hash audit ==="]
    for t, m in BASELINE.items():
        lines.append(f"  {t:34} base={m['rows']} present={m['rows']} identical={m['rows']} "
                     f"changed=0 missing=0 extra=0")
    return "\n".join(lines)


def good_facts():
    return {
        "run_summary": "Disposable grading smoke test over one hidden-test programming question.",
        "grading": {"outcome": "evaluated", "verdict": "Correct", "score": "1.00",
                    "max_score": "1.00", "submission_to_verdict_seconds": 6.46},
        "grading_attribution": {"trigger_path": "frontend", "method": "exclusion",
                                 "browser_request_captured": False,
                                 "edge_source_frontend_log_available": False,
                                 "basis": "Sweep eligibility gate excluded the cron path."},
        "per_test_case_results_persisted": False,
        "cleanup_execution_output_captured": True,
        "auth_fixture_deletion": {"observed_directly": False,
                                  "verified_by": "post-cleanup end-state inspection"},
        "restoration": {"all_tables_byte_identical": True},
    }


GOOD_CLEANUP = "ZZSMOKE cleanup: verification passed, deleting\n  answers: 1\n  exam_attempts: 1\nZZSMOKE cleanup: complete.\n"


def workspace(manifest=None, manifest_bytes=None, audit=None, facts=None):
    d = Path(tempfile.mkdtemp(prefix="zzarch_"))
    (d / "archive").mkdir()
    c = d / "ZZSMOKE_MANIFEST.json"
    if manifest_bytes is not None:
        c.write_bytes(manifest_bytes)
    else:
        c.write_bytes(json.dumps(manifest if manifest is not None else ACTIVE, indent=2).encode())
    (d / "baseline.txt").write_text(good_baseline_text(), encoding="utf-8")
    (d / "audit.txt").write_text(audit if audit is not None else good_audit_text(), encoding="utf-8")
    (d / "facts.json").write_text(
        json.dumps(facts if facts is not None else good_facts(), indent=2), encoding="utf-8")
    (d / "cleanup.txt").write_text(GOOD_CLEANUP, encoding="utf-8")
    return d, c


def run(d, c, run_id="2026-10-13", baseline="baseline.txt", cleanup="cleanup.txt",
        audit="audit.txt", facts="facts.json"):
    argv = [sys.executable, str(HELPER),
            "--baseline-evidence", str(d / baseline),
            "--content-audit", str(d / audit),
            "--run-facts", str(d / facts),
            "--run-id", run_id, "--canonical", str(c), "--archive-dir", str(d / "archive")]
    if cleanup is not None:
        argv += ["--cleanup-evidence", str(d / cleanup)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def canon(c):
    return json.loads(c.read_text(encoding="utf-8"))


print("=== T1  successful archive + reset after verified evidence ===")
d, c = workspace()
p = run(d, c)
check("exit 0", p.returncode == 0, p.stdout[-400:] if p.returncode else "")
arch = d / "archive" / "ZZSMOKE_RUN_2026-10-13.manifest.json"
check("archive created", arch.exists())
if arch.exists():
    a = json.loads(arch.read_text(encoding="utf-8"))
    check("marked CONSUMED_AND_CLEANED", a["_status"] == "CONSUMED_AND_CLEANED")
    check("all 5 ids preserved",
          all(a[k] == ACTIVE[k] for k in ("teacher_id", "student_id", "exam_id", "question_id", "attempt_id")))
    check("archive BOM-free", arch.read_bytes()[:3] != b"\xef\xbb\xbf")
    check("restoration audit recorded", "_restoration_audit" in a)
    check("audit says byte-identical here", a["_restoration_audit"]["all_tables_byte_identical"] is True)
    check("cleanup output recorded as captured", a["_cleanup_execution"]["output_captured"] is True)
    check("grading attribution recorded as inference",
          a["_grading"]["_attribution"]["method"] == "exclusion")
    check("evidence categories distinct",
          set(a["_evidence_categories"]) >= {"verified_facts", "known_discrepancies",
                                              "unavailable_evidence", "inferences"})
    check("canonical is null template",
          all(canon(c)[k] is None for k in ("teacher_id", "student_id", "exam_id", "question_id", "attempt_id")))
    check("canonical _status TEMPLATE", canon(c)["_status"].startswith("TEMPLATE"))
    check("canonical _rule restored to the canonical template text",
          "ZZSMOKE_CLEANUP.sql reads operator-pasted literals" in canon(c)["_rule"],
          canon(c)["_rule"][:70] + "...")

print("\n=== T2  cleanup evidence is OPTIONAL and recorded as unavailable ===")
d, c = workspace()
# Facts must agree that no cleanup output was supplied.
nocleanup = good_facts(); nocleanup["cleanup_execution_output_captured"] = False
(d / "facts.json").write_text(json.dumps(nocleanup, indent=2), encoding="utf-8")
p = run(d, c, cleanup=None)
check("exit 0 without --cleanup-evidence", p.returncode == 0, p.stdout[-300:] if p.returncode else "")
arch = d / "archive" / "ZZSMOKE_RUN_2026-10-13.manifest.json"
if arch.exists():
    a = json.loads(arch.read_text(encoding="utf-8"))
    check("recorded as NOT captured", a["_cleanup_execution"]["output_captured"] is False)
    check("recorded as unavailable evidence",
          any("Cleanup execution output" in u for u in a["_evidence_categories"]["unavailable_evidence"]))
    check("no synthesised NOTICE text",
          "APPLICATION RECORDS COMPLETE" not in json.dumps(a))

print("\n=== T2b refusal when an explicitly-supplied cleanup file is missing ===")
d, c = workspace()
p = run(d, c, cleanup="does_not_exist.txt")
check("refuses (non-zero)", p.returncode != 0)
check("message names the missing evidence", "cleanup-evidence file not found" in p.stdout, p.stdout.strip()[-160:])
check("canonical untouched (still ACTIVE)", canon(c)["_status"].startswith("ACTIVE_FIXTURE"))
check("no archive written", not list((d / "archive").glob("*.json")))

print("\n=== T3  refusal when baseline evidence is missing / failed / forged ===")
for label, fname, text in [
    ("missing file", "nope.txt", None),
    ("no pass sentence", "baseline.txt", "  everything looks fine to me\n"),
    ("contains DRIFT", "baseline.txt", good_baseline_text().replace("identical", "DRIFT", 1)),
    ("wrong row count", "baseline.txt", good_baseline_text().replace(" 6 -> 6", " 6 -> 8", 1)),
    ("forged success string", "baseline.txt", "PASS: production is byte-identical to the pre-fixture baseline.\n"),
]:
    d, c = workspace()
    if text is not None:
        (d / fname).write_text(text, encoding="utf-8")
    p = run(d, c, baseline=fname)
    check(f"refuses: {label}", p.returncode != 0, p.stdout.strip().splitlines()[-1][:120] if p.stdout.strip() else "")
    check(f"  ...canonical untouched", canon(c)["_status"].startswith("ACTIVE_FIXTURE"))

print("\n=== T3b refusal when the content audit is missing or unusable ===")
def _inconsistent_audit():
    # identical + changed != present
    out = ["=== content-hash audit ==="]
    for t, m in BASELINE.items():
        ident = 9 if t == "public.profiles" else m["rows"]
        out.append(f"  {t:34} base={m['rows']} present={m['rows']} identical={ident} "
                   f"changed=0 missing=0 extra=0")
    return "\n".join(out)


for label, text in [
    ("empty audit", ""),
    ("not an audit", "looks fine\n"),
    ("missing a table", "\n".join(l for l in good_audit_text().splitlines()
                                  if "public.answers" not in l)),
    ("duplicated table", good_audit_text() + "\n  public.profiles    base=6 present=6 "
     "identical=6 changed=0 missing=0 extra=0"),
    ("base count disagrees with baseline", good_audit_text().replace("base=6", "base=9", 1)),
    ("internally inconsistent", _inconsistent_audit()),
    ("claims a change but lists no id", good_audit_text() + "\n  auth.users         base=6 present=6 "
     "identical=5 changed=1 missing=0 extra=0"),
    ("lists an id for a table with changed=0", good_audit_text() +
     "\nCONTENT-CHANGED: public.profiles dde0c3e8-61ee-4d36-a874-3b1ef8a82413"),
]:
    d, c = workspace()
    (d / "audit.txt").write_text(text, encoding="utf-8")
    p = run(d, c)
    check(f"refuses: {label}", p.returncode != 0,
          p.stdout.strip().splitlines()[-1][:110] if p.stdout.strip() else "")
    check("  ...canonical untouched", canon(c)["_status"].startswith("ACTIVE_FIXTURE"))
    check("  ...no archive written", not list((d / "archive").glob("*.json")))

print("\n=== T4  invalid / missing UUIDs ===")
for field, bad in [("teacher_id", "not-a-uuid"), ("exam_id", None),
                   ("attempt_id", "111"), ("question_id", 12345), ("student_id", "")]:
    d, c = workspace()
    m = dict(ACTIVE); m[field] = bad
    c.write_bytes(json.dumps(m, indent=2).encode())
    before = c.read_bytes()
    p = run(d, c)
    check(f"refuses: {field}={bad!r}", p.returncode != 0)
    check("  ...canonical byte-identical to before the run", c.read_bytes() == before)

print("\n=== T5  teacher/student collision ===")
d, c = workspace()
m = dict(ACTIVE); m["student_id"] = m["teacher_id"]
c.write_bytes(json.dumps(m, indent=2).encode())
p = run(d, c)
check("refuses identical ids", p.returncode != 0, p.stdout.strip().splitlines()[-1][:110])
check("  ...no archive written", not list((d / "archive").glob("*.json")))

print("\n=== T6  email redaction + absence of secrets ===")
d, c = workspace()
run(d, c)
arch = next((d / "archive").glob("*.json"))
txt = arch.read_text(encoding="utf-8")
check("no '@' anywhere in the archive", "@" not in txt)
check("teacher email absent", ACTIVE["teacher_email"] not in txt)
check("student email absent", ACTIVE["student_email"] not in txt)
for needle in ("sb_secret_", "eyJ", "refresh_token", "access_token", "password"):
    check(f"  no {needle!r}", needle not in txt.lower())

print("\n=== T7  archive destination collision ===")
d, c = workspace()
(d / "archive" / "ZZSMOKE_RUN_2026-10-13.manifest.json").write_text("{}", encoding="utf-8")
p = run(d, c)
check("refuses to overwrite an existing archive", p.returncode != 0)
check("  ...existing archive byte-identical",
      (d / "archive" / "ZZSMOKE_RUN_2026-10-13.manifest.json").read_text(encoding="utf-8") == "{}")
check("  ...canonical untouched", canon(c)["_status"].startswith("ACTIVE_FIXTURE"))

print("\n=== T8  idempotency / repeated invocation ===")
d, c = workspace()
first = run(d, c)
arch = d / "archive" / "ZZSMOKE_RUN_2026-10-13.manifest.json"
digest1 = arch.read_bytes()
second = run(d, c)
check("first run succeeded", first.returncode == 0)
check("second run refuses (already reset)", second.returncode != 0)
check("  ...reason is 'not an ACTIVE fixture'", "not an ACTIVE fixture" in second.stdout)
check("  ...archive unchanged by the repeat", arch.read_bytes() == digest1)

print("\n=== T9  unexpected manifest state ===")
for label, doc in [
    ("null template", {"_status": "TEMPLATE - no active fixture.", "_rule": "r",
                       "teacher_id": None, "student_id": None, "exam_id": None,
                       "question_id": None, "attempt_id": None}),
    ("CONSUMED archive", {"_status": "CONSUMED_AND_CLEANED"}),
    ("no _status at all", {"teacher_id": ACTIVE["teacher_id"]}),
]:
    d, c = workspace(manifest=doc)
    p = run(d, c)
    check(f"refuses: {label}", p.returncode != 0)
    check("  ...nothing archived", not list((d / "archive").glob("*.json")))

print("\n=== T10  BOM input refused; no evidence lost on failure ===")
d, c = workspace(manifest_bytes=b"\xef\xbb\xbf" + json.dumps(ACTIVE).encode())
p = run(d, c)
check("refuses a BOM'd canonical manifest", p.returncode != 0)
check("  ...BOM'd file left exactly as found", c.read_bytes()[:3] == b"\xef\xbb\xbf")
check("  ...no archive written", not list((d / "archive").glob("*.json")))

print("\n=== T11  BOM-free integrity of outputs ===")
d, c = workspace()
run(d, c)
for f in (c, next((d / "archive").glob("*.json"))):
    check(f"{f.name} BOM-free", f.read_bytes()[:3] != b"\xef\xbb\xbf")
    json.loads(f.read_text(encoding="utf-8"))

print("\n=== T12  existing manifest safety behaviour still intact ===")
import importlib.util
spec = importlib.util.spec_from_file_location("bf", ROOT / "scripts" / "zzsmoke_baseline_fingerprint.py")
bf = importlib.util.module_from_spec(spec); spec.loader.exec_module(bf)
tmpl = d / "tmpl.json"; shutil.copy(c, tmpl)
bf.MANIFEST = tmpl
try:
    bf.load_manifest(); check("load_manifest refuses the reset template", False, "accepted!")
except SystemExit as e:
    check("load_manifest refuses the reset template", True, str(e))
check("cleanup SQL still has no manifest dependency",
      "ZZSMOKE_MANIFEST" not in (ROOT / "deploy" / "ZZSMOKE_CLEANUP.sql").read_text(encoding="utf-8"))
helper_src = HELPER.read_text(encoding="utf-8")
check("helper does not import subprocess", "import subprocess" not in helper_src)
check("helper does not invoke npx/psql", "npx" not in helper_src and "psql" not in helper_src)
check("helper does not use the linked-SQL runner",
      "_run_linked_select" not in helper_src and "db query" not in
      "\n".join(l for l in helper_src.splitlines() if not l.strip().startswith("#") and
                "db query" not in l))
check("helper writes only the canonical manifest and the archive", ".write_bytes(" in helper_src)

print(f"\n{'=' * 64}\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)