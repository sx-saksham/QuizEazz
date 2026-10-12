# ZZSMOKE execution runbook — NOT YET AUTHORIZED

Production writes in this document are **not approved**. Every step marked
**[AUTH]** requires explicit authorization before it is run. Steps marked
[read-only] are safe and are already green.

Project ref `bhzuyrzlpfkjcqpafzgu` · host `bhzuyrzlpfkjcqpafzgu.supabase.co`
· edge function `judge0-grade` v13.

## Artifact integrity (verified)

| File | SHA-256 |
|---|---|
| `deploy/ZZSMOKE_BOOTSTRAP.sql` | `2EBBC5F7ABC95E077552580F8C6B5BFBF4E693FC6471A313459DBF53EBC60C88` |
| `deploy/ZZSMOKE_CLEANUP.sql` | `23566C8DD378A3BE78774D71F82D49440562C6F974893D94593975D8433DFCF2` |
| `deploy/ZZSMOKE_BASELINE.json` | `2BD0EDCEFFADF05E21B049B2F34F8C4BB6E02D91704841734E4E3DA1FE01D666` |
| `scripts/zzsmoke_delete_auth_users.mjs` | `AE697A921A60C953A5C27AEB3EAAA50FA13FB59CEEF33DFD0D81DE030C5349A8` |

## Stage 0 — read-only preflight (no authorization needed)

```powershell
python scripts/zzsmoke_baseline_fingerprint.py compare   # must PASS, exit 0
python scripts/zzsmoke_check_rpcs.py                      # must be 10/10
python scripts/verify_applied_migration_20.py             # must be 28/28
npx supabase functions list --project-ref bhzuyrzlpfkjcqpafzgu   # judge0-grade v13
```

## Stage 1 — [AUTH] create the two accounts

Auth **Admin API** only (`admin.createUser`). No direct writes to `auth.*`.
`handle_new_user()` builds the profiles itself, so the fixture is not special.

```powershell
$env:ZZSMOKE_TEACHER_EMAIL  = 'zzsmoke-teacher@<domain>'
$env:ZZSMOKE_STUDENT_EMAIL  = 'zzsmoke-student@<domain>'
$env:ZZSMOKE_PASSWORD       = '<strong throwaway password, >=12 chars>'
$env:SUPABASE_URL           = 'https://bhzuyrzlpfkjcqpafzgu.supabase.co'
$env:SUPABASE_SERVICE_ROLE_KEY = '<service-role key>'
node scripts/zzsmoke_create_accounts.mjs
```

The script refuses if either address is already registered, then prints
`teacher_id` and `student_id`. The password is never printed or written.

## Stage 2 — [read-only] record the manifest

```powershell
python scripts/zzsmoke_capture_manifest.py verify
```

Resolves both UUIDs **by exact email**, proves `full_name` starts with
`ZZSMOKE`, rejects a manifest whose recorded UUIDs changed, and writes
`deploy/ZZSMOKE_MANIFEST.json`:

```json
{ "teacher_id": "<uuid>", "teacher_email": "...", "student_id": "<uuid>",
  "student_email": "...", "exam_id": null, "question_id": null, "attempt_id": null }
```

A UUID is never inferred or guessed. `record` re-verifies ownership first:

```powershell
$env:ZZSMOKE_NEW_ID = '<uuid>'
python scripts/zzsmoke_capture_manifest.py record exam_id
python scripts/zzsmoke_capture_manifest.py record question_id
python scripts/zzsmoke_capture_manifest.py record attempt_id
```

## Stage 3 — [AUTH] promote the fixture teacher

Edit **both** literals at the top of `deploy/ZZSMOKE_BOOTSTRAP.sql`
(`v_teacher_uuid`, `v_expected_email`), then run it in the SQL Editor.

No fallback selection exists: targeting is by exact UUID only. Five gates —
auth user exists under that id; email matches the expected value **and**
`%zzsmoke%`; profile named `ZZSMOKE%`; role exactly `student`; account owns
**zero** exams/questions/attempts/assignments/security_events. Then one
`UPDATE`, `ROW_COUNT = 1`, re-read verified, with the authoritative context set
transaction-locally (`is_local => true`).

Verify [read-only]:

```sql
SELECT p.id, u.email, p.full_name, p.role, u.raw_app_meta_data->>'role' AS jwt_role
  FROM public.profiles p JOIN auth.users u ON u.id = p.id
 WHERE p.id = '<teacher_uuid>'::uuid;
-- expect exactly one row: role = teacher, jwt_role = teacher
```

## Stage 4 — [AUTH] author the fixture (teacher RPCs)

Sign in as the teacher to obtain a user JWT, then call the RPCs. All parameter
names below were read from the live database.

| # | RPC | Exact arguments |
|---|---|---|
| 1 | `teacher_create_question` | `p_title` `p_question_type='programming'` `p_marks=1` `p_language='cpp'` `p_reference_solution` (+ explanation/requirements/starter code) |
| 2 | `teacher_set_test_cases` | `p_question_id`, `p_cases=[{"stdin":"5\n"},{"stdin":"7\n"}]` |

Note: `teacher_set_test_cases` reads **only** `stdin`; `case_order` is derived
from array position. `expected_stdout` is **not** set here.

Then, as a separate authoring step:

| # | RPC | Exact arguments |
|---|---|---|
| 1 | `teacher_create_exam` | `p_title` starting `ZZSMOKE` `p_duration_minutes=30` `p_status='draft'` |
| 2 | `teacher_set_exam_paper` | `p_exam_id`, `p_items=[{"question_id":"<qid>","marks":1}]` (marks must be 1..100) |
| 3 | `teacher_set_exam_assignments` | `p_exam_id`, `p_student_ids=[<student_uuid>]`, **`p_class=NULL`**, **`p_batch=NULL`** |
| 4 | `teacher_publish_now` | `p_exam_id` |

> **`p_class`/`p_batch` must be NULL.** Supplying either expands the assignment
> to every real student in `SY CSDS A|B|C` / `Batch 1|2|3`. Passing a class
> alongside an explicit id list aborts on the membership check instead — which
> is fail-closed, but wastes a run.

Record each id as it is produced (Stage 2).

## Stage 5 — [AUTH] real-Judge0 reference validation

`validate_reference` is a **mode of `judge0-grade`**, not a separate function.
It submits the stored reference solution to real Judge0 once per hidden case
and persists the outputs via `quizeazz_apply_reference_outputs`.

```powershell
Invoke-RestMethod -Method Post `
  -Uri "$env:SUPABASE_URL/functions/v1/judge0-grade" `
  -Headers @{ apikey = $env:TEACHER_JWT; Authorization = "Bearer $env:TEACHER_JWT" } `
  -Body (@{ validate_reference = $true; question_id = '<question_uuid>' } | ConvertTo-Json)
```

Success: `{ ok: true, validated: 2, total: 2, results: [ {ok:true,status_id:3}, ... ] }`.
Any `ok:false`, or `status_id <> 3`, **stops the run** — grading has no expected
output without it.

## Stage 6 — [AUTH] student attempt and submit

| # | RPC | Exact arguments |
|---|---|---|
| 1 | `start_or_get_exam_attempt` | `p_exam_id` |
| 2 | `save_exam_answer` | `p_attempt_id`, `p_question_id`, `p_content` (correct C++), `p_language='cpp'` |
| 3 | `submit_exam_attempt` | `p_attempt_id` |

Record the attempt id (Stage 2). The fixture student is the **only**
`exam_assignments` row, so no real student can reach this exam.

## Stage 7 — [read-only] prove the verdict

```sql
SELECT a.id, a.evaluation_status, a.marks_awarded,
       a.evaluation_details->>'graded_by'        AS graded_by,
       a.evaluation_details->>'judge0_status_id' AS status_id,
       a.evaluation_details->>'graded_at'        AS graded_at,
       EXTRACT(EPOCH FROM ((a.evaluation_details->>'graded_at')::timestamptz
                           - e.submitted_at))     AS grading_seconds
  FROM public.answers a JOIN public.exam_attempts e ON e.id = a.attempt_id
 WHERE a.attempt_id = '<attempt_uuid>'::uuid;
```

Exactly one row. `evaluation_status='Correct'`, `marks_awarded=1`,
`graded_by='judge0-ce'`, one `graded_at`. Migration 20's `FOR UPDATE` makes a
second commit structurally impossible (measured 0/30 concurrent vs 30/30
without).

## Stage 8 — [AUTH] cleanup

**Order is mandatory: application SQL first, Auth Admin API last.**

```powershell
# 8.1 application records (single DO block = one transaction)
#     paste the manifest UUIDs into deploy/ZZSMOKE_CLEANUP.sql, run in SQL Editor
# 8.2 the two auth identities
node scripts/zzsmoke_delete_auth_users.mjs
```

8.2 uses `admin.deleteUser(id, false)` — hard delete, because a soft delete
leaves the `auth.users` row and fails the byte-exact comparison below.

**8.3 verification:**

```powershell
python scripts/zzsmoke_baseline_fingerprint.py compare
```

Expect every table `identical`, exit 0. This proves fixture ids are absent
**and** every original row is byte-for-byte unchanged.

If the fingerprint moved, the tool classifies each difference as
`FIXTURE-owned` or `FOREIGN`. **FOREIGN drift means concurrent real activity**,
not fixture fallout: stop, report, do **not** widen any delete predicate.

## Abort procedure

At any failed assertion: stop, do not continue to the next stage, and run
Stage 8 for the state actually reached. Cleanup is presence-aware, so it
handles a teacher created but never promoted, a question with no exam, or an
exam with no attempt. **Never broaden a delete predicate, never delete auth
rows by hand, and never re-trigger grading manually.** If cleanup itself
aborts, report the exact message and stop — the transaction rolled back and
left production untouched.

Clear the secret afterwards:

```powershell
$env:SUPABASE_SERVICE_ROLE_KEY = ''; $env:ZZSMOKE_PASSWORD = ''
```