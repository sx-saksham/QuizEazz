# Migration 20 — verdict row lock: rehearsal record

**This is a record of one observed local run. It is not production validation,
not verification of the live deployed function, and not evidence that migration
20 was ever applied to production.**

---

## Run identity

| Field | Value |
|---|---|
| Date | 2026-10-12 |
| Run label | `legb-20261012T122800Z` |
| Command | `python scripts/test_verdict_concurrency.py` |
| Target | disposable local PostgreSQL 17.11 container, loopback-only publish |
| Target address | `127.0.0.1` on an ephemeral port — **not** the linked Supabase project |
| Schema | minimal stand-in created by the rehearsal itself |
| Exit code | `0` |
| Verdict line | `RESULT: all behavioural concurrency assertions hold` |

The container was created for this rehearsal, used only by it, and destroyed
afterwards along with its temporary credential file. It no longer exists and
this run is not reproducible from this document alone.

---

## Result

Both function bodies were extracted verbatim from the migration files and
installed into separate throwaway databases, then driven through the identical
30-round concurrent workload — two threads per round, each on its own database
connection, firing opposing verdicts (`Correct` vs `Wrong Answer`) at one answer.

| Variant | Concurrent double-apply | `FOR UPDATE` present |
|---|---|---|
| `migration_09_cpp_only_and_verdict_fix.sql` (no row lock) | **30 / 30 rounds** | no |
| `migration_20_verdict_row_lock.sql` (`FOR UPDATE`) | **0 / 30 rounds** | yes |

Contract checks, reported identically for both installed variants:

| Property | Observed | Expected |
|---|---|---|
| Return type | `boolean` | unchanged |
| `SECURITY DEFINER` | `true` | preserved |
| `search_path` | `public, pg_temp` | preserved |
| ACL — anon / authenticated / public / service_role | `False / False / False / True` | unchanged by `CREATE OR REPLACE` |
| Anti-tamper trigger blocks unprivileged writes | `true` | preserved |

---

## What this demonstrates

On PostgreSQL 17.11, using genuinely independent concurrent sessions and the
real migration-20 function body, `FOR UPDATE` on the guard `SELECT` makes
"one verdict per answer" an enforced guarantee rather than a TOCTOU check.
Without the lock, every one of the 30 rounds double-applied.

The migration-09 run is a **negative control**, and the rehearsal **fails** if
that variant fails to reproduce the double-apply. A green result therefore
cannot come from a test that is insensitive to the defect it claims to detect.

---

## Limitations — what this does NOT show

- **Not production.** The rehearsal used a minimal stand-in schema in a
  throwaway database, not production tables, RLS policies, data volumes, or the
  production PostgreSQL version. Nothing here was run against the linked Supabase
  project.
- **Not verification of the live deployed function.** This exercised the SQL text
  in `supabase/migration_20_verdict_row_lock.sql`. It did not read `pg_proc` on any
  real database, so it says nothing about what function is currently deployed or
  whether migration 20 has been applied anywhere.
- **Not a CI gate and not a recurring result.** This is a single manually
  invoked run. There is no scheduled job, no committed artefact, and no pipeline
  that enforces this. One observation is not a pattern.
- **Not a universal production guarantee.** A result on one version of one
  engine under one workload does not establish the property for every version,
  every configuration, or every workload. Concurrency behaviour is
  configuration-sensitive.
- **Not reproducible from this record.** The cluster and credential were deleted.
  Re-running requires recreating a disposable PostgreSQL 17 instance.
- **No coverage of the marks path.** The rehearsal races the verdict writer. It
  does not test marks computation — see the open issue below.
- **No coverage of grading itself.** No C++ was compiled, no program was
  executed, and no Judge0 instance was contacted.

---

## Open issue — unrelated to this race, still unresolved

`supabase/migration_20_verdict_row_lock.sql` declares

```sql
v_marks_total INT := 0;
```

and later guards with

```sql
IF v_marks_total IS NULL THEN
  RAISE EXCEPTION 'Answer % is not a coding answer of attempt %.', ...;
```

Because the variable is **initialised to `0` rather than left `NULL`**, a marks
lookup that matches no row leaves it at `0`, so the `IS NULL` guard can never
fire. Grading then proceeds with a marks total of zero instead of refusing. The
same initialisation is inherited from the earlier migration and is **not**
introduced or fixed by migration 20.

This defect is orthogonal to the row-lock race proved above, is **not** covered
by this rehearsal, and remains **unfixed**. It should be tracked separately.

---

## Relationship to the other documentation

`deploy/DEPLOY_STEPS.md` and `deploy/ZZSMOKE_RUNBOOK.md` state the same 30/30 vs
0/30 figures. This file does not supersede them; it records that those figures
were reproduced once against the current checkout, and bounds what that
reproduction means.