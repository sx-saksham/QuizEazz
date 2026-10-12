"""
Gate 1 read-only check: is the Supabase Auth Admin API credential material
AVAILABLE, without printing, echoing, or deriving anything sensitive?

This script deliberately emits ONLY booleans, character counts and a truncated
SHA-256 digest of each secret. A digest is one-way, so it cannot be used to
reconstruct the key, but it does let the operator confirm that the same value
is present in two places without revealing either.

Never prints: the value, any substring of it, its length modulo anything that
would narrow a brute force, or any environment variable that is not a secret
in the first place.
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def probe(label, value):
    """Report presence only. Never reveals the value."""
    if value is None or value == "":
        print(f"  [absent ]  {label}")
        return None
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    print(f"  [present]  {label}  len={len(value)}  sha256[:12]={digest}")
    return digest


def probe_file(label, path):
    if not path.exists():
        print(f"  [absent ]  {label}")
        return None
    size = path.stat().st_size
    print(f"  [present]  {label}  file_size={size}")
    return size


print("=== Auth Admin API credentials (presence only, never values) ===")
digests = {}
digests["service_role"] = probe("SUPABASE_SERVICE_ROLE_KEY (env)", os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
url = probe("SUPABASE_URL (env)", os.environ.get("SUPABASE_URL"))
access = probe("SUPABASE_ACCESS_TOKEN (env)", os.environ.get("SUPABASE_ACCESS_TOKEN"))
digests["access_token"] = access

print("\n=== supabase CLI stored credentials ===")
cli = Path.home() / ".supabase" / "access-token"
probe_file("CLI access token file", cli)

print("\n=== linked project linkage ===")
ref_file = ROOT / "supabase" / ".temp" / "project-ref"
if ref_file.exists():
    ref = ref_file.read_text(encoding="utf-8").strip()
    print(f"  [present]  supabase/.temp/project-ref  = {ref}")
    print(f"             expected bhzuyrzlpfkjcqpafzgu : {ref == 'bhzuyrzlpfkjcqpafzgu'}")
else:
    print("  [absent ]  supabase/.temp/project-ref")

print("\n=== runtime dependency for the Admin API scripts ===")
try:
    import supabase  # noqa: F401
    print("  [present]  python 'supabase' module")
except ImportError:
    print("  [absent ]  python 'supabase' module (node scripts use @supabase/supabase-js instead)")

pkg = (ROOT / "node_modules" / "@supabase" / "supabase-js" / "package.json")
probe_file("node_modules/@supabase/supabase-js", pkg)

print("\n=== what the Admin API scripts require ===")
print("  scripts/zzsmoke_create_accounts.mjs  needs: SUPABASE_URL,")
print("                                      SUPABASE_SERVICE_ROLE_KEY,")
print("                                      ZZSMOKE_TEACHER_EMAIL,")
print("                                      ZZSMOKE_STUDENT_EMAIL,")
print("                                      ZZSMOKE_PASSWORD")
print("  scripts/zzsmoke_delete_auth_users.mjs needs: SUPABASE_URL,")
print("                                      SUPABASE_SERVICE_ROLE_KEY,")
print("                                      ZZSMOKE_TEACHER_UUID,")
print("                                      ZZSMOKE_STUDENT_UUID")

ready = digests["service_role"] is not None and url is not None
print(f"\n  Admin API ready to execute: {ready}")
if not ready:
    print("  -> SUPABASE_URL and/or SUPABASE_SERVICE_ROLE_KEY must be set by the")
    print("     operator before any account creation or deletion is attempted.")
    sys.exit(1)
sys.exit(0)