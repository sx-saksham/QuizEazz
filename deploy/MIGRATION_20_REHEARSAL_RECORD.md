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

## Superseded claim — `v_marks_total`, corrected

> **This section previously asserted a defect that the rehearsal disproved. The
> earlier claim was wrong and is withdrawn.**

An earlier revision of this record stated that
`v_marks_total INT := 0` in `supabase/migration_20_verdict_row_lock.sql`
made the `IF v_marks_total IS NULL THEN RAISE` guard unreachable, on the
reasoning that PL/pgSQL leaves a `SELECT ... INTO` target unchanged when the
query returns zero rows.

**That reasoning was incomplete, and a later disposable rehearsal contradicted
it.** A second run against a disposable local PostgreSQL 17.11 cluster, using
`scripts/test_verdict_marks_guard.py` (run label `marksguard-20261012T134827Z`,
exit code 1), installed the current body verbatim and exercised three
invalid-answer cases:

| Case | CURRENT body | Corrected variant |
|---|---|---|
| coding answer with no `exam_questions` row | **refused** | refused |
| answer whose `question_type` is not `coding` | **refused** | refused |
| answer belonging to a different attempt | **refused** | refused |

and three legitimate cases:

| Case | CURRENT body | Corrected variant |
|---|---|---|
| valid coding answer, `Correct`, marks 10 | accepted, 10 | accepted, 10 |
| valid coding answer with `NULL` marks | accepted, 0 | accepted, 0 |
| valid coding answer, `Wrong Answer` | accepted, 0 | accepted, 0 |

**The current body refused every invalid case.** The negative control therefore
failed as designed — the suite reports that it "cannot demonstrate the bug it
was written to catch" and exits non-zero rather than reporting a pass.

The observed behaviour is consistent with PL/pgSQL assigning `NULL` to a
`SELECT ... INTO` target when the query matches no rows, so the `IS NULL` guard
fires on a missing row. On that reading the `:= 0` initialiser is redundant
rather than harmful.

### What this establishes, and what it does not

- It establishes that **in this rehearsal**, on this disposable cluster, with
  this minimal stand-in schema, the guard fired.
- It does **not** establish that the function is correct, in production, or in
  general. It is a single local observation against a stand-in schema.
- It does **not** validate the live deployed function or anything about the
  linked Supabase project.
- Whether any defect exists here at all is **not** established. The prior
  static diagnosis is withdrawn, and no replacement claim is made.

### Unresolved: trigger versus fixture seeding

The same rehearsal surfaced a second question it could **not** answer. The
stand-in anti-tamper trigger refuses an INSERT that supplies any of
`is_correct`, `marks_awarded`, or `evaluation_status`. The fixtures insert
answers with `evaluation_status = 'Pending Evaluation'`, which static reading
predicts should therefore be rejected. It was not rejected, and the equivalent
fixtures in the concurrency rehearsal were likewise accepted.

**The cause is unknown and is not guessed at here.** It bears on fixture
fidelity rather than on migration 20, and it should be resolved before either
rehearsal is relied on for anything beyond the properties they actually
measured.

### Status

No change is proposed to the SQL on the basis of the withdrawn claim. Whether
the `:= 0` initialiser is worth removing as a defensive measure — so the guard
cannot be defeated by a future change to the lookup — is a separate question
that this rehearsal did not address.

---

## Relationship to the other documentation

`deploy/DEPLOY_STEPS.md` and `deploy/ZZSMOKE_RUNBOOK.md` state the same 30/30 vs
0/30 figures. This file does not supersede them; it records that those figures
were reproduced once against the current checkout, and bounds what that
reproduction means.