"""
P3 behavioural test: the grading error handler must never itself throw.

Compiles and runs the REAL Deno.serve handler from index.ts against a stubbed
Deno runtime, forcing a failure inside the try block.

The historical bug: the catch block read `isServiceRole`, declared with
`const` INSIDE `try`, which is invisible to the sibling `catch`. Any failure
therefore raised a ReferenceError from inside the error handler and the
intended JSON 500 never returned.

No database, no network, no production access.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IDX = ROOT / "supabase" / "functions" / "judge0-grade" / "index.ts"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  [{'x' if cond else ' '}] {name}" + (f"   {detail}" if detail else ""))


def match_brace(text, open_idx):
    d = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            d += 1
        elif text[i] == "}":
            d -= 1
            if d == 0:
                return i
    return -1


def extract_handler():
    src = IDX.read_text(encoding="utf-8")
    start = src.index("Deno.serve(async (req: Request)")
    open_idx = src.index("{", src.index("=>", start))
    close = match_brace(src, open_idx)
    return src[start:close + 1] + ")"


TEMPLATE = """
type GradingSource = 'frontend' | 'sweep'
type PendingAttemptRow = { attempt_id: string }

export const __LOGS: any[] = []
function logGrading(fields: any): void {
  try { __LOGS.push(fields) } catch { /* logging must never fail a request */ }
}

const ENV: Record<string, string> = {
  JUDGE0_API_URL: 'https://judge0.invalid',
  JUDGE0_API_TOKEN: 'stub-token-not-a-secret',
  SUPABASE_URL: 'https://project.invalid',
  SUPABASE_ANON_KEY: 'stub-anon-key',
  SUPABASE_SERVICE_ROLE_KEY: 'stub-svc-key',
}
;(globalThis as any).Deno = {
  env: { get: (k: string) => ENV[k] ?? '' },
  serve: (fn: any) => { (globalThis as any).__handler = fn },
}

let __FAIL_CONFIG = true
function getConfig(): { baseUrl: string; token: string } {
  if (__FAIL_CONFIG) throw new Error('Judge0 is not configured')
  return { baseUrl: 'https://judge0.invalid', token: 'stub-token-not-a-secret' }
}
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status })
}
async function gradeAttempt(): Promise<Record<string, unknown>> {
  return { ok: true }
}

__HANDLER_SRC__

function __req(auth: string | null): any {
  return {
    method: 'POST',
    headers: { get: (h: string) => (h.toLowerCase() === 'authorization' ? auth : null) },
    json: async () => ({ attempt_id: 'x' }),
  }
}

export default async function main(): Promise<any[]> {
  const out: any[] = []
  for (const [label, mode] of [
    ['getConfig throws (Judge0 env missing)', 'config'],
    ['missing Authorization -> 401', 'noauth'],
  ] as [string, string][]) {
    __LOGS.length = 0
    __FAIL_CONFIG = mode === 'config'
    let result: any = null, threw: string | null = null
    try {
      const resp = await (globalThis as any).__handler(__req(mode === 'noauth' ? null : 'Bearer user-jwt'))
      const body = await resp.json()
      result = { status: resp.status, body }
    } catch (ex: any) {
      threw = ex && ex.constructor ? ex.constructor.name + ': ' + ex.message : String(ex)
    }
    out.push({ label, mode, result, threw, logs: __LOGS.slice() })
  }
  return out
}
"""

RUNNER = r"""
import { readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import ts from 'typescript'
const src = readFileSync(process.argv[2], 'utf8')
const js = ts.transpileModule(src, { compilerOptions: {
  module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, strict: false },
  fileName: 'h.ts' }).outputText
const f = join(tmpdir(), `p3_${process.pid}.mjs`)
writeFileSync(f, js)
const m = await import('file://' + f.replace(/\\/g, '/'))
process.stdout.write(JSON.stringify(await m.default()))
"""


def main():
    handler = extract_handler()
    print(f"  extracted handler: {len(handler)} chars")
    check("handler extraction is balanced",
          handler.count("{") == handler.count("}"))
    check("isServiceRole declared BEFORE the try block",
          handler.index("let isServiceRole = false") < handler.index("try {"))
    check("no `const isServiceRole` still shadows it",
          "const isServiceRole" not in handler)

    ts_src = TEMPLATE.replace("__HANDLER_SRC__", handler)
    tmp = Path(tempfile.mkdtemp())
    (tmp / "h.ts").write_text(ts_src, encoding="utf-8")
    runner = Path(__file__).resolve().parent / "_p3_runner.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    try:
        proc = subprocess.run(["node", str(runner), str(tmp / "h.ts")],
                              capture_output=True, text=True, timeout=600,
                              cwd=str(ROOT))
    finally:
        runner.unlink(missing_ok=True)
    if proc.returncode != 0:
        print("  DRIVER FAILED:\n", (proc.stderr or proc.stdout)[-2500:])
        return 2
    out = json.loads(proc.stdout)

    print("\n== results ==")
    for r in out:
        print(f"  {r['label']}")
        print(f"     threw  = {r['threw']}")
        print(f"     result = {json.dumps(r['result'])}")
        print(f"     logs   = {json.dumps(r['logs'])}")

    print("\n== assertions ==")
    g = next(r for r in out if r["mode"] == "config")
    check("failure inside try does NOT throw out of the handler",
          g["threw"] is None, str(g["threw"]))
    check("handler returns a real JSON 500 instead of crashing",
          g["result"] is not None and g["result"]["status"] == 500,
          json.dumps(g["result"]))
    check("the 500 body preserves the original error message",
          bool(g["result"]) and g["result"]["body"].get("error")
          == "Judge0 is not configured", json.dumps(g["result"]))
    check("exactly one grading_failed log entry emitted", len(g["logs"]) == 1,
          json.dumps(g["logs"]))
    check("log entry tagged source='frontend' (isServiceRole readable)",
          bool(g["logs"]) and g["logs"][0].get("source") == "frontend",
          json.dumps(g["logs"]))
    check("log entry records the failing error",
          bool(g["logs"]) and g["logs"][0].get("error") == "Judge0 is not configured")
    check("no ReferenceError anywhere in the failure path",
          "ReferenceError" not in str(g["threw"]))

    o = next(r for r in out if r["mode"] == "noauth")
    check("missing Authorization still returns 401, no throw",
          o["threw"] is None and bool(o["result"]) and o["result"]["status"] == 401,
          json.dumps(o["result"]))

    print("\n" + "=" * 72)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for n, d in FAIL:
        print(f"  FAIL {n} {d}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())