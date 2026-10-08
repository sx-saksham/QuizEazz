# QuizEazz — Supabase Migration Order

Run these **in order** in the Supabase SQL Editor. Each file is idempotent, but
they build on each other, so the order matters.

| # | File | What it does |
|---|------|--------------|
| 1 | `schema_security_hardening.sql` | Pause/metadata columns, role-escalation trigger, base RPCs (`get_exam_questions`, `save_exam_answer`, `submit_exam_attempt`, grading, pause/resume, student reads) |
| 2 | `migration_exam_secure_architecture.sql` | `security_events` + RLS, dedup + unique constraints, answer/attempt tamper triggers, attempt lifecycle RPCs, student result/review RPCs |
| 3 | `migration_teacher_and_student_hardening.sql` | Faculty RPCs (`teacher_get_exams`, `teacher_create_exam`, `teacher_get_candidates`, `teacher_create_question`, `teacher_get_questions`, overview stats) |
| 4 | **`migration_exam_management_integrity.sql`** | **The fix set.** MCQ key persistence, schedule-window enforcement, atomic submit+grade, exam assignment & paper management RPCs, authoritative profile provisioning |
| 5 | **`migration_05_rpc_privilege_hardening.sql`** | **Required after migration 4.** Pins the EXECUTE ACL on every QuizEazz function: anon and PUBLIC stripped, `authenticated` on the caller-facing RPCs only, `service_role` on everything. Also narrows `ALTER DEFAULT PRIVILEGES` so future `CREATE FUNCTION` cannot silently re-grant `anon`. |
| 6 | **`migration_06_runtime_blockers_and_attempt_access.sql`** | **Required after migration 5.** Fixes two functions that installed cleanly but failed at runtime: `save_exam_answer()` wrote to a nonexistent `answers.updated_at`, and `get_student_results()` read a nonexistent `exams.total_marks`. Also separates NEW-attempt eligibility from EXISTING-attempt access, and records `exam_attempts.max_score` inside the grading transaction. |
| 7 | **`migration_07_judge0_integration.sql`** | **Judge0 CE programming grading.** Five `service_role`-only SECURITY DEFINER RPCs that apply an objective Judge0 verdict and recompute attempt totals. Marks are computed in SQL; only `Correct` ever earns any. No DDL, no RLS change. |
| 8 | `seed_bootstrap_teacher.sql` | One-off: promote a single existing account to `teacher`. Edit the email, then run. |

> **Judge0 integration is NOT deployed.** Migrations 1-7 plus the
> `judge0-grade` Edge Function must all be in place before programming answers
> can be graded. See `docs/JUDGE0_INTEGRATION.md` for the architecture, the
> exact secrets and the deployment order. Until then every programming answer
> truthfully stays `Pending Evaluation` with 0 marks.

> **Migration 4 does NOT establish the function ACLs correctly.** Its
> `REVOKE` statements omit `anon` entirely and its dynamic loop filters on
> `pronargs > 0`, which skips every zero-argument function. Migration 5 is
> what actually enforces the policy. Run both.

> **Why migration 6 exists.** PostgreSQL validates a PL/pgSQL statement the
> first time it *executes*, not when the function is created. Two functions
> created by migration 2 referenced columns that do not exist, so they
> installed without error and then failed on first call — including
> `save_exam_answer()`, which is the only write path for candidate answers.
> Migration 4 was audited against the live schema; the functions it inherited
> from migrations 1–2 were not. Migration 6 repairs exactly those, plus two
> invariants that were previously unenforced.

---

## Before you run anything

Run the read-only audit to capture the current live state:

```
supabase/DIAGNOSTIC_readonly_audit.sql
```

Paste the whole file into the Supabase SQL Editor and press Run. It contains
only SELECT statements and no psql backslash commands, so it runs as-is. The
Editor renders one result grid, so select an individual section block and run it
again when you need a specific output.

Save the output. It contains the real table list, the live `exams.status`
constraint, RLS policies, grants, function signatures, trigger definitions, row
counts, the MCQ answer-key integrity check, and a post-migration presence check
for all 36 QuizEazz RPCs. It performs no writes.

## After running migration 4

Re-run the diagnostic and diff. Specifically confirm:

- Section 5 shows `Faculty read exams` (SELECT only) on `exams`, and
  `Faculty read security events` (SELECT only) on `security_events`.
- Section 6 shows **no** `INSERT`/`UPDATE`/`DELETE` grant to `authenticated`
  on `exam_assignments`, `exam_questions`, `answers`, `exam_attempts`.
- Section 7 shows the new RPCs, all `prosecdef = true` with
  `search_path = public, pg_temp`.
- Section 8 shows `anon` has **no** execute grant on any QuizEazz RPC.
- Section 13 shows `correct_flags = 1` for every MCQ.

## After running migration 6

Migration 6 ends with its own read-only verification block, Section H. Run it
and confirm:

- **H1** — all five replaced functions exist, `prosecdef = true`, and
  `anon_has_execute` / `public_has_execute` are `false` everywhere.
- **H2** — `quizeazz_grade_objective_marks` is **not** executable by
  `authenticated`. Expect 0 rows.
- **H3** — no function in `public` is executable by `anon`. Expect 0 rows.
- **H4** — every finalized attempt carries both `score` and `max_score`.
  Expect 0 rows.
- **H5** — `max_score` equals the paper total for every finalized attempt.
  Expect 0 rows.

Diagnostic section **16f** now validates the stronger invariant: a *finalized*
attempt (not merely one that happens to have a score) must have both columns,
with `0 <= score <= max_score`. Expect 0 rows.

## Existing data

Migration 4 does **not** truncate, drop or recreate any table. The only rows it
can delete are exact duplicate `exam_assignments` / `exam_questions` pairs, and
only when duplicates actually exist. It raises a `NOTICE` when it does this.

---

## Granting faculty and admin roles

Roles cannot be escalated from any browser. Use the SQL editor:

```sql
-- promote an existing account
UPDATE public.profiles SET role = 'teacher' WHERE id = (
  SELECT id FROM auth.users WHERE lower(email) = lower('faculty@college.edu')
);

UPDATE public.profiles SET role = 'admin' WHERE id = (
  SELECT id FROM auth.users WHERE lower(email) = lower('admin@college.edu')
);
```

Public signup only ever produces `student`. The `handle_new_user` trigger
provisions the profile row server-side.

---

## Making "Scheduled Start Time" actually enforced

The UI maps `Scheduled` to `status = 'published' AND starts_at > now()`. That is
a presentation rule. The enforcement lives in the database:

- `start_or_get_exam_attempt()` refuses to create an attempt before `starts_at`
- `get_exam_questions()` refuses to serve the paper before `starts_at`

So a candidate cannot reach the paper early even by calling the RPC directly.
