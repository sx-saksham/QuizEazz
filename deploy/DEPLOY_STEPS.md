# QuizEazz — Migration 20 Production Deployment Procedure

**Status: APPLIED AND VERIFIED IN PRODUCTION.** Migration 20 is live. The
verdict writer in `pg_proc` carries `FOR UPDATE` on its guard `SELECT`, and
`python scripts/verify_applied_migration_20.py` reports 28/28 checks passing
against production using read-only `SELECT`s (counts only, no student data
read). See `deploy/EDGE_FUNCTION_DEPLOY_RECORD.md` for the deployment record.

> **This file is retained as the procedure of record, not as a to-do list.**
> The steps below describe how migration 20 was applied. Re-running Step 1 is
> harmless in principle — the statement is `CREATE OR REPLACE` and is
> idempotent — but it is **not required**, and nothing here is pending.

| | |
|---|---|
| File | `supabase/migration_20_verdict_row_lock.sql` |
| SHA-256 | `93603AAD51FA33DD99088D6DEFF17C6B06A4D05B681485CADA6779D5E1963DB9` |
| Payload | `deploy/migration_20_PAYLOAD.sql` (123 lines, one statement) |
| Rollback | `deploy/ROLLBACK_apply_judge0_verdict.sql` |
| Verify | `deploy/POST_DEPLOY_VERIFY.sql` |

---

## ⚠️ Do NOT use `supabase db push`

Evidence:

- `supabase/migrations/` **does not exist** — migrations live flat in `supabase/*.sql`
- `supabase_migrations.schema_migrations` **does not exist** in production
- `npx supabase migration list --linked` → `{"migrations":[],"message":"Migrations listed"}`

`db push` therefore applies **nothing** for migration 20. Worse: if anyone
creates `supabase/migrations/` and copies files in, the empty history table
means the CLI treats **all 19** migrations as pending and would re-apply them
against live student data. **Never relocate the migration files.**

---

## Step 0 — Pre-checks (read-only)

```bash
# Run every command below from the root of your cloned QuizEazz repository -
# the directory that contains supabase/, deploy/ and scripts/.
cd <path-to-your-quizeazz-clone>

# Confirm the file is unchanged since review
Get-FileHash supabase/migration_20_verdict_row_lock.sql -Algorithm SHA256
# EXPECT: 93603AAD...63DB9

# Confirm no attempt is mid-grade (applying a lock is safe either way, but
# avoid changing the function while grading is in flight)
python - <<'PY'
import sys; sys.path.insert(0,'scripts')
from verify_rpc_contract import _run_linked_select
rows,err=_run_linked_select("SELECT count(*) AS n FROM public.exam_attempts WHERE status='evaluating'")
print("evaluating attempts:", rows[0]['n'] if rows else err)
PY

# Verify the ALREADY-APPLIED migration 20 in production (read-only).
python scripts/verify_applied_migration_20.py
# EXPECT: 28 passed, 0 failed, including:
#   [x] live body contains FOR UPDATE
#   [x] FOR UPDATE is on the guard SELECT specifically
#   [x] exactly one executable FOR UPDATE (the other is a comment)
#
# Migration 20 IS ALREADY APPLIED. Its verdict writer carries exactly one
# executable `FOR UPDATE`, on the guard SELECT that reads
# answers.evaluation_status. There is nothing to reapply.

# ⚠️ DO NOT run scripts/prepare_migration_20_deploy.py as a "pre-check".
# That script captures the CURRENT live function into the rollback file. Now
# that migration 20 is applied, the live body already contains FOR UPDATE, so
# re-running it would overwrite deploy/ROLLBACK_apply_judge0_verdict.sql with a
# body that still carries the lock - a "rollback" that does not roll back. It
# would also overwrite deploy/migration_20_PAYLOAD.sql and
# deploy/POST_DEPLOY_VERIFY.sql. It was a pre-application authoring step only.
```

Keep `deploy/ROLLBACK_apply_judge0_verdict.sql` under version control. It is
the pre-migration-20 body (the one defined by
`migration_09_cpp_only_and_verdict_fix.sql`) and must not be regenerated.

## Step 1 — Apply (Supabase Dashboard → SQL Editor → New query)

1. Open **Project Dashboard → SQL Editor → New query**.
2. Open `deploy/migration_20_PAYLOAD.sql`, copy **everything** (it is exactly
   one `CREATE OR REPLACE FUNCTION … $$ … $$;` statement).
3. Paste it into the editor and wrap it so it is atomic:

```sql
BEGIN;
<contents of deploy/migration_20_PAYLOAD.sql>
COMMIT;
```

The explicit `BEGIN`/`COMMIT` guarantees all-or-nothing regardless of whether
the editor wraps your script itself. If the editor *does* already wrap, Postgres
emits a harmless warning (`there is already a transaction in progress`) and the
transaction still commits correctly.

4. Click **Run**.

**Expected result:** success, no error. If anything errors, the transaction
aborts and **nothing is changed** — read the error and stop.

### Alternative (strongest control, if you have psql + the DB URL)

```bash
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -1 \
     -f deploy/migration_20_PAYLOAD.sql
```
`-1` wraps the whole file in a single transaction; `ON_ERROR_STOP=1` aborts on
the first error.

## Step 2 — Post-deployment verification (read-only)

**Option A — SQL Editor:** run `deploy/POST_DEPLOY_VERIFY.sql` query by query.

| Check | Expect |
|---|---|
| V1 `has_for_update` | **t** |
| V2 `returns` | **boolean** |
| V3 `security_definer` | **t**, `search_path=public, pg_temp` |
| V4 privileges anon/authenticated/public/service_role | **f / f / f / t** |
| V5 `no_attempt_evaluating` | **true** |

**Option B — automated:**

```bash
python scripts/verify_rpc_contract.py --deployed
```
Expect:
```
ok    live quizeazz_apply_judge0_verdict guards with FOR UPDATE
ok    migration 20 source carries FOR UPDATE on the guard SELECT
ok    live verdict writer refuses a verdict on an in-progress attempt
ok    live verdict writer refuses to overwrite an existing verdict
ok    live verdict writer refuses an unknown outcome
ok    live verdict writer computes marks in SQL
ok    live verdict writer ACL is service_role only
RESULT: RPC contracts match; SQL structure balanced; LIVE database verified
```

**Stop and roll back if V4 is anything other than `f/f/f/t`,** or if V2/V3
differ from the values above.

## Step 3 — Callability check (read-only)

> **This check was already performed for the applied migration 20.** It is kept
> here as the verification a *future* deployment must repeat, not as work
> outstanding. For the current state, see
> `python scripts/verify_applied_migration_20.py` (28/28) and
> `deploy/EDGE_FUNCTION_DEPLOY_RECORD.md`. Grading itself was exercised
> end to end on 2026-10-12; see
> `deploy/archive/ZZSMOKE_RUN_2026-10-12-smoke2.manifest.json`.

For any **new** application of this migration, confirm the new function is
genuinely callable after applying it. Grading is exercised automatically by the
2-minute sweep, so:

```sql
-- EXPECT: the query runs without a permission error. Zero rows is a normal
-- result when nothing is pending; it means the function is callable.
SELECT attempt_id FROM public.quizeazz_list_attempts_needing_programming_grading();
```

(Read-only. If it errors with a permission error, the function's grants or
`SECURITY DEFINER` were damaged — roll back.)

---

## Rollback

**Rollback is a deliberate, operator-initiated, production-affecting action.
It is not automatic and is not triggered by any failure.** Nothing below runs
on its own; a human must choose to paste the SQL and click Run.

Paste the entire contents of `deploy/ROLLBACK_apply_judge0_verdict.sql` into
the SQL Editor. **It must be wrapped in an explicit transaction** — it does not
wrap itself:

```sql
BEGIN;
<contents of deploy/ROLLBACK_apply_judge0_verdict.sql>
COMMIT;
```

Without the wrapper the statement autocommits. With it, any error aborts and
**nothing is changed**.

It is a single `CREATE OR REPLACE` that restores the pre-migration-20 body —
the same body defined by `migration_09_cpp_only_and_verdict_fix.sql`.
**No data is touched and nothing needs restoring.** The statement issues no
`DROP`, `ALTER`, `GRANT` or `REVOKE`, so the existing ACL (`service_role` only;
`anon`/`authenticated`/`PUBLIC` revoked) is preserved by `CREATE OR REPLACE`
rather than being re-asserted here.

Consequence of rolling back: the concurrent-verdict race returns. Grading stays
*functionally* correct — marks are recomputed from stored rows by
`quizeazz_finalize_attempt_grading` — but two overlapping graders could silently
overwrite one verdict.

Reverting the Edge Function to v12 is a **separate, separately-authorised**
action; see `deploy/EDGE_FUNCTION_DEPLOY_RECORD.md`.

---

## Verified before approval

Proven end-to-end against a **disposable PostgreSQL 17.11** container
(`scripts/dryrun_migration_20_deploy.py`, 10/10 checks):

- PAYLOAD executes with no SQL error in a single transaction
- ACL established beforehand **survives** `CREATE OR REPLACE`
- installed body carries `FOR UPDATE`
- ROLLBACK executes and restores the original body **semantically** — the
  executable SQL is equivalent to the migration 09 body. Note the stored
  rollback file is line-ending-normalised to LF, so it is *not* claimed to be
  byte-identical to the original `pg_proc.prosrc` capture; equivalence is by
  statement content, which is what matters to PL/pgSQL.
- rollback leaves the ACL untouched

Post-apply, against production: `python scripts/verify_applied_migration_20.py`
→ **28/28**, including `FOR UPDATE` live on the guard `SELECT`, signature and
`SECURITY DEFINER` unchanged, `search_path` still pinned, and the ACL exactly
`(anon=F, authenticated=F, public=F, service_role=T)`.

Concurrency (30 rounds, opposing verdicts on one answer):
migration 09 **30/30 double-apply** · migration 20 **0/30**.