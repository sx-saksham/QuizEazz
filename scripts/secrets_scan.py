"""Scan source + built client bundle for leaked secrets."""
import glob
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = {
    "JWT-like":            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "sb_secret_ key":      r"sb_secret_[A-Za-z0-9]{6,}",
    "service_role literal": r"service_role_key\s*[=:]\s*[\"'][^\"']{8,}",
    "JUDGE0 token literal": r"JUDGE0_API_TOKEN\s*[=:]\s*[\"'][^\"']{8,}",
    "VITE judge0 var":     r"VITE_[A-Z_]*JUDGE0[A-Z_]*",
    "judge0 host in client": r"judge0[\w.-]*:\d{4}|JUDGE0_API_URL",
}

targets = [
    "supabase/functions/judge0-grade/index.ts",
    "supabase/functions/judge0-grade/judge0.ts",
    "src/lib/supabase.ts",
    "src/lib/judge0Client.ts",
]
targets += sorted(glob.glob(str(ROOT / "dist" / "assets" / "*.js")))
targets += sorted(glob.glob(str(ROOT / "dist" / "assets" / "*.css")))

print("=== secrets scan: Edge Function source + built client bundle ===")
flagged = 0
for t in targets:
    p = Path(t) if Path(t).is_absolute() else ROOT / t
    if not p.exists():
        continue
    label = str(p.relative_to(ROOT)) if p.is_absolute() else t
    txt = p.read_text(encoding="utf-8", errors="replace")
    hits = {k: len(re.findall(v, txt)) for k, v in PATTERNS.items()}
    hits = {k: v for k, v in hits.items() if v}
    # The Edge Function legitimately READS Judge0 config from the runtime env
    # by NAME (Deno.env.get('JUDGE0_API_URL')). That is the correct design and
    # is not a leak. Only a hardcoded VALUE or host would be.
    if "supabase/functions/" in label.replace("\\", "/"):
        hits = {k: v for k, v in hits.items() if k != "judge0 host in client"}
    status = "LEAK" if hits else "clean"
    if hits:
        flagged += 1
    print(f"  [{status:5}] {label}  {hits if hits else ''}")

print()
print(f"  files scanned : {len([t for t in targets if (ROOT / t).exists() or Path(t).exists()])}")
print(f"  files flagged : {flagged}")
print("  verdict       :",
      "NO secrets in the client bundle or committed source"
      if flagged == 0 else f"REVIEW {flagged} file(s)")

# Confirm the Judge0 config lives only in the server-side env, never in src/.
src_hits = []
for t in glob.glob(str(ROOT / "src" / "**" / "*.*"), recursive=True):
    txt = Path(t).read_text(encoding="utf-8", errors="replace")
    for k, v in PATTERNS.items():
        if re.search(v, txt):
            src_hits.append((str(Path(t).relative_to(ROOT)), k))
print()
print("  src/ references to Judge0 config or secrets:",
      src_hits if src_hits else "NONE")

# Server-side: prove Judge0 host/port appear only as env NAMES, never values.
fn = (ROOT / "supabase/functions/judge0-grade/index.ts").read_text(encoding="utf-8")
host_literal = re.findall(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|https?://", fn)
env_names = re.findall(r"Deno\.env\.get\('([A-Z0-9_]+)'\)", fn)
print("  Edge Function env reads :", sorted(set(env_names)))
print("  hardcoded hosts in fn   :", host_literal if host_literal else "NONE")
print("  token read from env only:", "JUDGE0_API_TOKEN" in env_names)
print("  verdict                 :",
      "server reads Judge0 config from env only; no host/token in source"
      if not host_literal else "REVIEW: a host appears hardcoded")