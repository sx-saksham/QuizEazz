# QuizEazz — Judge0 CE Programming Grading

Judge0 CE **v1.13.1** runs the submitted programming answers inside an
`isolate` sandbox on an EC2 host. This document records the integration
architecture, the exact secrets required, and the deployment steps.

**Status: implemented and verified locally/statically. NOT deployed.**

---

## 1. Architecture and why

```
Student browser                     Supabase                    EC2 (3.107.47.4)
──────────────                      ────────                    ────────────────
  final submit
  submit_exam_attempt()  ────────▶  Postgres RPC
  (attempt frozen)                  SEC DEF, sets
                                    authoritative_context
                                        │
                                        │ attempt is finalized
                                        ▼
  judge0Client.ts  ─────────────▶  Edge Function  ──────────▶  Judge0 API :2358
  { attempt_id } only               judge0-grade                X-Auth-Token
  NO token, NO code                 JUDGE0_API_TOKEN  ◀──secret──┘
                                    JUDGE0_API_URL
                                        │
                                        │ writes via service_role-only
                                        │ SECURITY DEFINER RPCs
                                        ▼
  review page  ◀───────────────     answers.evaluation_status
  (refetch)                         answers.marks_awarded  ◀── marks decided in SQL
                                   exam_attempts.score / max_score
```

### Why an Edge Function

QuizEazz's only server-side component today is Postgres. Calling Judge0 from
Postgres would mean enabling `pg_net` plus `vault` and building a polling
scheduler inside the database — more moving parts, harder error handling, and
it would put an outbound-HTTP capability into the database tier.

A **Supabase Edge Function** is the same platform QuizEazz already uses, has a
first-class encrypted secret store, and is the only place the token ever
exists. It is not a new service on another cloud.

The browser gains **nothing**: it sends only `{ attempt_id }` plus its own
Supabase session JWT.

### Division of authority

| Decision | Owner |
|---|---|
| When code may run (only after final submission) | Postgres (`quizeazz_judge0_pending_answers` refuses `in_progress`) |
| What the code actually did | Judge0 (sole authority) |
| Status → QuizEazz vocabulary | Edge Function (`judge0.ts`, unit-tested) |
| **How many marks** | **Postgres** (`quizeazz_marks_for_outcome`) |
| Any AI involvement | **None.** No model is called anywhere in this path. |

Marks are computed in SQL precisely so that no caller — Edge Function
included — can inflate a mark.

---

## 2. Files

| File | Role |
|---|---|
| `supabase/functions/judge0-grade/judge0.ts` | Pure Judge0 client: language map, limits, status mapping, submit/poll. No Deno globals except `fetch`. |
| `supabase/functions/judge0-grade/index.ts` | Edge Function entry point. Reads the secrets, authorises the caller, drives grading. |
| `supabase/migration_07_judge0_integration.sql` | Five `service_role`-only SECURITY DEFINER RPCs + one ownership check. No DDL, no RLS change. |
| `src/lib/judge0Client.ts` | Browser helper. Sends `{ attempt_id }`. Holds **no** token. |
| `src/components/student/ExamTakingScreen.tsx` | Calls the helper once, immediately after `submitExamAttempt()` returns. |
| `scripts/test_judge0_integration.py` | 85 automated checks. |

`src/lib/languages.ts` is **unchanged**. QuizEazz's canonical identifiers stay
`python` / `cpp` / `java` / `c`.

---

## 3. Language mapping

| QuizEazz id | Judge0 `language_id` | Runtime |
|---|---|---|
| `python` | 71 | Python 3.8.1 |
| `cpp` | 54 | C++ (GCC 9.2.0) |
| `java` | **90** | **Java (OpenJDK 17)** |
| `c` | 50 | C (GCC 9.2.0) |

Judge0's own id **62 (`Java (OpenJDK 13.0.1)`) is not used by QuizEazz** and is
left active and untouched on the server.

---

## 4. Limits sent on every submission

```
cpu_time_limit               3
cpu_extra_time               1
wall_time_limit              10
memory_limit                 512000   (KB)
max_processes_and_or_threads 60
max_file_size                1024     (KB)
enable_network               false
```

Never sent: `compiler_options`, `command_line_arguments`, `additional_files`,
`callback_url`. Judge0 also refuses all four server-side.

> **`max_processes_and_or_threads` must never be lowered below 60 for Java.**
> A JVM needs far more processes/threads than a Python or C program to boot. At
> `p=10` the JVM fails with `pthread_create failed (EAGAIN)` before it can run
> a single line.

---

## 5. Status mapping

| Judge0 status | QuizEazz outcome | Marks |
|---|---|---|
| 3 Accepted | `Correct` | full marks |
| 4 Wrong Answer | `Wrong Answer` | 0 |
| 5 Time Limit Exceeded | `Time Limit Exceeded` | 0 |
| 6 Compilation Error | `Compilation Error` | 0 |
| 7–12, 14 runtime/exec errors | `Runtime Error` | 0 |
| 13 Internal Error | `System Error` | 0 |
| 1, 2 In Queue / Processing | *not a verdict* — stays pending | — |
| unknown / missing | `System Error` | 0 |

**Memory Limit Exceeded:** Judge0 CE has **no dedicated MLE status**. An
over-limit allocation returns status `11` with `exit_code 137` and `memory`
pinned at exactly the limit. The integration detects that signature
(`exit_code === 137 && memory >= memory_limit`) and reports
`Memory Limit Exceeded`. Without this a memory kill is indistinguishable from
an ordinary crash.

**Judge0 unavailable / timeout:** the answer is left exactly as it was —
`Pending Evaluation`. A connection failure, a timeout, or a missing verdict
can **never** become `Wrong Answer`. `security_events` and the attempt totals
record the truth; nothing is fabricated. A service-role sweep retries later.

---

## 6. Integrity model preserved

- Final submission freezes the attempt (`submit_exam_attempt`).
- Both grading RPCs **hard-fail** on `status = 'in_progress'`.
- A verdict can be applied **once**: the answer must still be
  `Pending Evaluation`, and an existing verdict is never overwritten.
- The student's `code` and `language` are **never** updated by the verdict path.
- All writes run under `quizeazz.authoritative_context`, which only
  SECURITY DEFINER functions can set.
- The attempt stays `evaluating` while any programming answer is pending, so
  the UI can never show "Evaluated" over an unresolved answer.
- `max_score` stays the paper total from `SUM(exam_questions.marks)`.

---

## 7. Secrets required — set these yourself in the Supabase dashboard

**Component that holds the secrets: the Supabase Edge Function
`judge0-grade`. Nothing else needs any secret.**

| Secret name | Value | Where to set it |
|---|---|---|
| `JUDGE0_API_TOKEN` | the Judge0 `AUTHN_TOKEN` | Supabase → Edge Functions → `judge0-grade` → Secrets |
| `JUDGE0_API_URL` | `http://3.107.47.4:2358` | same |

`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY` and
`SUPABASE_JWT_SECRET` are provisioned automatically by Supabase when the
function is deployed. **Do not override them.**

**Never add the token to:**

- `.env.local`
- any `VITE_*` variable (those are inlined into the browser bundle)
- `src/` — a test asserts this
- GitHub, Vercel, or any committed file

The token currently exists only on the EC2 host at
`/opt/judge0/.secrets.env` (mode 600). I did not read or move it.

---

## 8. Deployment steps (not performed)

1. **Apply migration 7** in the Supabase SQL Editor. Run the Section H
   verification block: expect 0 rows for H2, H3, H4 and H5.
2. **Set the two secrets** in the Edge Function secrets panel.
3. **Deploy the function**:
   ```
   supabase functions deploy judge0-grade --no-verify-jwt
   ```
   (`--no-verify-jwt` is required because the function does its own
   `Authorization` handling and per-attempt ownership check.)
4. **Optional recovery sweep.** Schedule `judge0-grade` with `{ "sweep": true }`
   using the service-role key, e.g. pg_cron every 5 minutes, to pick up
   anything left pending by a Judge0 outage.

---

## 9. Verification performed (local/static only)

| Check | Result |
|---|---|
| `npm run lint` | 0 errors |
| `npx tsc -b --noEmit` | exit 0 |
| `npm run build` | ✓ 414.34 kB / 99.44 kB gzip |
| `scripts/test_exam_rules.py` | 29 passed |
| `scripts/verify_rpc_contract.py` | all RPC contracts match |
| `scripts/test_judge0_integration.py` | **85 passed** |
| migration 7 column audit vs live schema | 24 valid refs, **0 invalid**, 0 unresolved |
| migration 7 paren balance | 7 bodies, 0 unbalanced |
| DDL / RLS / non-function GRANT / DELETE in migration 7 | all absent |

Judge0 itself was verified live against the real server: Java 17 correct,
compile error, runtime error, TLE, memory kill (exit 137), network blocked,
filesystem isolation and process limits all behave correctly, and
`language_id 62` still works.

**Not verified locally** — these need the live Supabase project: Edge Function
typecheck by the Supabase CLI, migration 7 execution, and an end-to-end
submit → Judge0 → verdict round trip.
