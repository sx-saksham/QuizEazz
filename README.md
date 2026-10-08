# QuizEazz

A college-controlled coding quiz and examination platform for supervised Windows
laboratory sessions.

React 19 · TypeScript · Vite · Tailwind CSS v4 · Monaco Editor · Supabase
(PostgreSQL + Auth + RLS + `SECURITY DEFINER` RPCs)

## Status

| Area | Status |
|---|---|
| Supabase Auth (email/password) | Working |
| Role-based access (DB-enforced) | Working |
| Question bank (create / inspect / search) | Working |
| Examination scheduling | Working |
| Assign Students | Working |
| Manage Paper (exact fixed question set) | Working |
| Candidate exam session, autosave, navigation | Working |
| MCQ objective grading | Working |
| Pause / resume / security events | Working |
| Results and question review | Working |
| **Programming code execution (Judge0 CE)** | **Not implemented.** See below. |

## Local setup

```bash
npm install
```

Create `.env.local`:

```
VITE_SUPABASE_URL=https://<project-ref>.supabase.co
VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
```

The app throws immediately at startup if either value is missing, rather than
failing later inside an examination.

```bash
npm run dev      # dev server
npm run build    # typecheck + production build
npm run preview  # serve the build
```

## Database setup

1. Run `supabase/DIAGNOSTIC_readonly_audit.sql` first and keep the output. It
   only reads.
2. Apply the migrations in the order given in
   `docs/SUPABASE_MIGRATION_ORDER.md`. **Migration 4
   (`migration_exam_management_integrity.sql`) is the one that adds exam
   assignment and paper management.**
3. Re-run the diagnostic and diff the output.
4. Promote faculty/admin accounts with the SQL in the migration order doc.

None of the migrations truncate, drop or recreate a table.

## Architecture

```
React SPA
  │  supabase.rpc(...)  — every sensitive call
  ▼
PostgREST → SECURITY DEFINER PL/pgSQL function
              │  auth.uid() + profiles.role check
              ▼
            tables (RLS enabled, no client grants)
```

The SPA never calls `supabase.from(...)`. There is no direct-table fallback
anywhere, so an RPC failure surfaces as a visible error instead of a silently
empty screen.

Key directories:

- `src/lib/teacherApi.ts` — faculty RPC wrappers
- `src/lib/studentApi.ts` — candidate RPC wrappers
- `src/lib/examEngine.ts` — deterministic randomization, autosave, submit
- `src/context/AuthContext.tsx` — session + authoritative profile read
- `supabase/` — migrations, read-only diagnostic, teacher bootstrap seed
- `docs/` — security model, migration order

## Examination rules

- Every question is compulsory. There is no random question pool: faculty
  select the exact set and all candidates receive it.
- Only the on-screen order and MCQ option order vary, seeded from
  `(studentId, examId)`. A reload cannot reshuffle a paper.
- MCQ correctness is attached to option identity, never to display position.
- One question at a time, free navigation, no per-question submit.
- Coding questions have no Run / Compile / Test button and no correctness
  feedback during the attempt.
- Autosave is continuous and the indicator reflects the real server result.
- Final Submit is the only submission action and it freezes the attempt.

## Programming evaluation — NOT IMPLEMENTED

Objective code execution is not available. Submitted code is stored and shown
back to both faculty and the candidate, but programming questions are recorded as
**`Pending Evaluation` with zero marks**, and the UI says so plainly. Nothing is
faked, and the browser never awards programming marks.

To implement it:

1. Deploy Judge0 CE (self-hosted, Docker, on a separate host from the app).
2. Put a trusted runner behind a Supabase Edge Function. It must:
   - accept a submission only from the platform, never from a browser
   - hold the Judge0 API key server-side
   - enforce CPU, wall-clock and memory limits per submission
   - run with no network egress
   - verify the faculty reference solution against derived cases before the
     question can be published
3. Generate hidden test vectors from the input/output specification and
   constraints. They must never be sent to a candidate.
4. Call the runner from `quizeazz_grade_objective_marks()` and map Judge0 status
   ids to `Correct` / `Wrong Answer` / `Compilation Error` / `Runtime Error` /
   `TLE` / `MLE` / `System Error`.

Candidate code must never execute in the web app process. No `eval()`, no
`child_process`, no `subprocess`.

## Deployment on college Windows PCs

QuizEazz is an ordinary web application. It cannot enforce OS-level lockdown on
its own. For a supervised examination, the workstation configuration does that
work:

**Windows Assigned Access (kiosk)**

1. `Settings → Accounts → Other users → Set up a work or school account`, or a
   dedicated local exam account.
2. `Settings → Accounts → Family & other users → Assign someone a user account`
   → set **Set up as a kiosk device**.
3. Add the platform URL as an allowed app.

**Edge kiosk mode (fallback)**

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --kiosk "https://<your-deployment>/student/exams" `
  --no-first-run --disable-features=Translate
```

**Also do**

- Remove or disable every other browser and any developer tooling on the image.
- Block the Windows Store and software installation for the exam account.
- Disable clipboard sharing between the exam machine and any other device.
- Ensure the exam account cannot reach the college network share or a personal
  home directory.
- Keep a physical invigilator present. The platform records objective browser
  events; it cannot detect an OS screenshot or a phone.

**Deploying the app**

Any static host works — Vercel, Netlify, or a local web server on the lab LAN.
The SPA needs SPA fallback (rewrite unknown paths to `index.html`) because
routing is client-side. Vercel does this by default for Vite projects.

## Security

See `docs/SECURITY_MODEL.md` for the full boundary, the RPC authorization table,
the anti-tamper trigger list, and an explicit statement of what browser
JavaScript cannot protect against.
