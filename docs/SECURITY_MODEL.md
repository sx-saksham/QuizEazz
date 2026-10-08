# QuizEazz — Security Model

## The boundary

**Database authorization is the security boundary.** React route guards are a
user-experience feature only. A candidate who edits their own JavaScript can
navigate anywhere in the SPA and call any PostgREST endpoint. Every sensitive
operation is therefore a `SECURITY DEFINER` RPC that re-checks `auth.uid()` and
the authoritative `profiles.role`.

## What a candidate can reach

| Operation | Path | Enforcement |
|---|---|---|
| Read own profile | `get_my_profile()` | `auth.uid()` only |
| List assigned exams | `get_student_exam_overview()` | joined through `exam_assignments` |
| Start / resume attempt | `start_or_get_exam_attempt()` | assignment + `published` + `starts_at`/`ends_at` window |
| Fetch paper | `get_exam_questions()` | assignment + same window |
| Save an answer | `save_exam_answer()` | attempt ownership + `in_progress` + not paused + deadline |
| Read own saved answers | `get_student_exam_answers()` | attempt ownership |
| Pause / resume | `pause_exam_attempt()`, `resume_exam_attempt()` | attempt ownership |
| Log a security event | `record_security_event()` | attempt ownership + exam match |
| Submit | `submit_exam_attempt()` | attempt ownership + authoritative deadline |
| Read results | `get_student_results()` | `student_id = auth.uid()` |
| Read review | `get_student_attempt_review()` | attempt ownership, blocked while `in_progress` |

A candidate **cannot**:

- call any `teacher_*` RPC (each raises `Access denied` for non-faculty)
- `SELECT` from any sensitive table (`anon` and `authenticated` hold no grant)
- `INSERT`/`UPDATE`/`DELETE` on `answers`, `exam_attempts`,
  `exam_assignments`, `exam_questions` (grants revoked; plus triggers)
- change their own role (`protect_profile_role_escalation`)
- read `questions.reference_solution`, `questions.explanation`, or
  `options[].is_correct` during an attempt
- read the faculty answer key before their own attempt is evaluated

## Never sent to a candidate

`get_exam_questions()` projects `options` as `[{id, text}]` only. The private key
lives on the row as `options[].is_correct` and is projected only by
`teacher_get_questions()`, `teacher_get_attempt_detail()` and, after evaluation,
`get_student_attempt_review()`.

`reference_solution` is returned only by `teacher_get_questions()`.

## Anti-tamper triggers

| Trigger | Table | Blocks |
|---|---|---|
| `trg_check_answer_eligibility` | `answers` | writes after submit, writes while paused, writes past deadline, client-supplied marks, cross-candidate writes, `attempt_id`/`question_id` mutation, candidate deletes |
| `trg_protect_attempt_mutation` | `exam_attempts` | direct candidate `INSERT`/`UPDATE`/`DELETE`, `student_id`/`exam_id`/`started_at` mutation, illegal status transitions |
| `trg_protect_exam_assignments` | `exam_assignments` | all client writes |
| `trg_protect_exam_questions` | `exam_questions` | all client writes |
| `trg_protect_profile_role` | `profiles` | non-admin role assignment or change |
| `on_auth_user_created` | `auth.users` | — (provisions the profile as `student`) |

Trigger functions honour a `quizeazz.authoritative_context` session flag, which
only the platform's own `SECURITY DEFINER` functions set. It is not reachable
from a client: `set_config` is not exposed as an RPC.

## Composition integrity

`teacher_set_exam_assignments()` and `teacher_set_exam_paper()` both call
`quizeazz_assert_composition_editable()`, which refuses to change an exam that
is `closed` or that already has a finalized submission. Unique constraints on
`(exam_id, student_id)` and `(exam_id, question_id)` make both operations
idempotent and duplicate-proof.

## Grading authority

`submit_exam_attempt()` performs the freeze **and** the objective grading in one
server transaction. The browser makes a single call and holds no grading
authority: it cannot skip grading, alter a mark, or change an outcome.

MCQ grading compares the stored selection against the private key. Programming
questions are recorded as `Pending Evaluation` with 0 marks while no execution
runner is connected. No verdict is ever guessed, and no mark is ever inferred in
the browser.

## What this cannot do

Browser JavaScript cannot prevent OS-level screenshots, `Alt+Tab`, secure
desktop switching, a second device, or a phone in a pocket. `fullscreen` and
visibility APIs are best-effort signals, not enforcement. QuizEazz records those
events and pauses the attempt, but the actual controls are:

- Windows Assigned Access / Edge kiosk on a controlled account
- a restricted Windows image with no alternative browser
- a physical invigilator

Security events are recorded as objective facts for invigilator review. No
candidate is automatically labelled a violator and no automatic penalty is
applied.

### Focus signals are defeatable by extensions — stated plainly

An end-to-end test confirmed that tab switching, `Alt+Tab` and Windows desktop
switching all pause the attempt correctly. It also confirmed the opposite
limitation: a **browser extension such as "Always Active Window" keeps the exam
window reporting itself as focused and visible** while the candidate is
elsewhere. When that extension is active, neither `window.blur` nor
`visibilitychange` fires, so no pause is triggered.

This is not a bug that can be patched in the page. An extension holding host
permissions for the exam origin can suppress, reorder or spoof any event the
page is able to observe, and it runs with strictly more privilege than the page
itself. **QuizEazz does not attempt to work around this**, and specifically
does not:

- poll `document.hasFocus()` on a timer
- re-focus or re-raise the window when it believes focus was lost
- inject synthetic events to test whether real events still arrive
- detect "suspicious" extension behaviour and block the exam
- present any in-page check as proof that switching away is impossible

Each of those would be cosmetic at best. They would fight the student's own
window management, produce spurious pauses and accessibility failures, and
still be defeated. Shipping them would also create a false sense of assurance,
which is worse than a documented gap.

What remains in place, unchanged, is the honest version of the control:

| Layer | What it actually does | Strength |
|---|---|---|
| Browser fullscreen gate | the exam cannot begin until `document.fullscreenElement` is confirmed non-null | browser-enforced, requires a click |
| `fullscreenchange` exit handling | exiting mid-exam pauses the attempt and logs `fullscreen_exit` | best-effort, Escape is available to the candidate |
| `window.blur` | pauses the attempt and logs `window_blur` — this is what catches Alt+Tab and desktop switching | best-effort, defeatable by an extension |
| `visibilitychange` | pauses the attempt and logs `tab_switch` — catches tab switch and minimise | best-effort, defeatable by an extension |
| Server-side RPC checks | ownership, lifecycle, pause state, deadline | **authoritative, not defeatable from the client** |
| `security_events` table | objective record for invigilator review | authoritative, immutable from the client |

Only the last two are security controls. Fullscreen and the focus signals are
a gate and signals.

### Fullscreen: what is enforced, and what is not

- The exam **cannot start** until `document.fullscreenElement` has been
  confirmed non-null. There is no "continue without fullscreen".
- Chrome requires the request to come from a direct user gesture, so it is
  issued from a dedicated "Enter Fullscreen & Start Exam" click that appears
  once the paper has loaded. It is never issued from an effect, a timer, or
  after an `await` — those are refused with `NotAllowedError` by policy.
- Success is confirmed by reading `document.fullscreenElement`, never by the
  promise resolving. A page that merely *looks* fullscreen is not accepted.
- There is **no student-facing "Exit Fullscreen" control**. The workspace has
  one direction only.
- Leaving fullscreen mid-exam — Escape, F11, or the browser's own exit control —
  pauses the attempt, records `fullscreen_exit`, and requires Resume. Resuming
  requires re-entering fullscreen.
- Application cleanup after final submission, or navigating away, is not treated
  as a security event.

Fullscreen is still not a boundary. It hides browser chrome; it does not
prevent a second device, a phone, a screenshot, or an extension that suppresses
focus events.

### Focus signals are defeatable by extensions — stated plainly
### Required lab deployment (out of scope for this application)

Preventing this class of behaviour is an endpoint-management problem. It is
documented here rather than implemented in React, because a web application
cannot install OS policy, disable extensions, or lock a desktop.

The lab must provide, at minimum:

1. **A dedicated, non-administrator exam account.** Not a domain account with
   rights to install software.
2. **Edge in kiosk mode** (`msedge.exe --kiosk <exam-url>`, or
   `edge://settings` → *Kiosk mode*, which requires the
   `WindowsSettings.UserKioskMode` policy) so that `Ctrl+Alt+Delete`, the
   address bar and browser menus are unavailable. Kiosk alone does not block
   `Alt+Tab`.
3. **Windows Assigned Access** (Windows 10/11 Enterprise, or *Assigned Access*
   via the Windows Configuration / MDM layer) so only that one Edge window
   exists and `Alt+Tab`, `Win`, virtual desktops and `Ctrl+Alt+Delete` cannot
   reach other applications. This is the control that actually prevents the
   switch the extension was emulating.
4. **Extensions disabled by policy** — `ExtensionSettings` / `BlockExtensions`
   via Group Policy or MDM. This directly removes the "Always Active Window"
   class of helper.
5. **Firewall egress restricted** to the Supabase project host so a second
   device or a tethered phone cannot be used for lookup.
6. **A physical invigilator** as the control for anything the endpoint cannot
   enforce (a second monitor on a VM escape, a person leaning over a shoulder).

None of the above is code in this repository. It is deployment configuration
and must be applied per exam machine. QuizEazz's role is to record what the
browser honestly observed so the invigilator has something objective to review.

