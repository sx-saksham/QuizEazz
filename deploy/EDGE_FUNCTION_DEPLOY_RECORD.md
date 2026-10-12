# QuizEazz — `judge0-grade` Edge Function Deployment Record

**Status: DEPLOYED — version 13, ACTIVE.** Migration 20 untouched.

| | |
|---|---|
| Project | `bhzuyrzlpfkjcqpafzgu` (Coding Quiz Platform, ap-southeast-1) |
| Function | `judge0-grade` |
| Previous version | **12** — digest `d4a81dfcbda3ac08a50c79c74df38874734bf6a52bba174712efe5e13a032a88` |
| **Deployed version** | **13** — digest `e14999b28ba18c6b0621c8da8317dd6d32fde63d926e5a0fecff9a0d29dadf97` |
| `verify_jwt` | `false` (unchanged) |
| Runtime | `supabase/edge-runtime:v1.77.4` |
| Scope | P2/P2b (bounded sweep, failure isolation) + P3 (`isServiceRole` scope fix) |

## Rollback

**Rollback to v12 is a deliberate, operator-initiated, production-affecting
action.** It redeploys an older function to production; it is not automatic and
is not triggered by any failure. A human must choose to run it.

The v12 source is preserved at `deploy/rollback_v12/`. Those files are
**historical snapshots, not current source** — see the warning banner at the top
of each.

**Preferred — restore the known-good v12 source, then redeploy:**
```powershell
# CONFIRM YOU ARE IN THE REPOSITORY ROOT BEFORE COPYING - these paths overwrite
# the live source tree.
Copy-Item deploy/rollback_v12/index.ts  supabase/functions/judge0-grade/index.ts  -Force
Copy-Item deploy/rollback_v12/judge0.ts supabase/functions/judge0-grade/judge0.ts -Force
npx supabase functions deploy judge0-grade --project-ref bhzuyrzlpfkjcqpafzgu
```

> ⚠️ **Do NOT use `git checkout HEAD -- supabase/functions/judge0-grade/` as a
> rollback.** That instruction was valid only while v12 was the newest commit.
> Since commit `b346589` (v13) landed, `HEAD` *is* v13, so that command would
> restore v13 and deploy it again — performing **no rollback at all** while
> appearing to succeed. To recover v12 from git, name the pre-v13 commit
> explicitly:
> ```powershell
> git checkout 5f7a138 -- supabase/functions/judge0-grade/
> npx supabase functions deploy judge0-grade --project-ref bhzuyrzlpfkjcqpafzgu
> ```
> v12 is the content of `5f7a138`; the `deploy/rollback_v12/` snapshot is the
> same code, preserved independently of git history.

Verify after rollback:
```powershell
npx supabase functions list --project-ref bhzuyrzlpfkjcqpafzgu
```

Rollback touches only the function — no schema, no migration, no cron, no data.

## Post-deployment verification performed

| Check | Result | Source |
|---|---|---|
| Version + status | `13`, `ACTIVE` | re-confirmed from the platform; matches the digest below |
| Deployed digest | `e14999b2…dadf97` | re-confirmed from the platform |
| POST no auth header | `HTTP 401` `{"error":"missing authorization"}` | **historical record** — not re-run in this review |
| POST bogus bearer | `HTTP 401` `{"error":"unauthenticated"}` | **historical record** — not re-run in this review |
| GET (method guard) | `HTTP 405` | **historical record** — not re-run in this review |
| v12 digest `d4a81dfc…` | superseded by v13 | **historical record** — the v12 artefact is no longer deployed, so it cannot be re-read |
| Cron jobid 2 | active, `*/2 * * * *` | **historical record** — not re-run in this review |
| Cron sweep after deploy | run counter 2173 → 2174, succeeded | **historical record** — counters advance continuously, so this cannot be re-observed |
| All-time cron success | 2174 / 2174 | **historical record** |
| Migration 20 | applied and verified, 28/28 | re-verified 2026-10-12 against production, read-only |

## End-to-end grading smoke test — PERFORMED 2026-10-12

An isolated, disposable fixture **was** created and used to exercise the real
grading path end to end through the browser UI, and was then removed.

- Result: `evaluated`, score `1.00` / `1.00`, Judge0 status 3, exit code 0,
  aggregate hidden tests `2/2`.
- Submission-to-verdict: ~6.46 s.
- Fixture fully retired afterwards: 12 application rows and 2 Auth identities
  deleted; the application layer verified byte-identical to its pre-fixture
  baseline.

**Run record:** `deploy/archive/ZZSMOKE_RUN_2026-10-12-smoke2.manifest.json`

> ⚠️ **Attribution caveat — read before relying on the trigger path.** The
> grading *outcome* is a verified fact. The **trigger path (frontend vs cron
> sweep) was inferred by exclusion**, not confirmed by a captured frontend
> request or a `source:'frontend'` Edge Function log line. The observed browser
> tab never left the fullscreen gate, so the submission request was not
> captured, and no Dashboard session was available to read Edge logs. The
> inference rests on the live sweep eligibility gate and cron schedule
> excluding the cron path. A short latency alone would **not** establish it.

Known unrelated drift is recorded in that archive and is **not** attributable to
this deployment: one original `auth.users` row (`a5cc4116-…`) shows a content
difference. See the archive; do not treat it as a cleanup defect.