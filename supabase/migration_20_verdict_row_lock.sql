-- ====================================================================
-- QUIZEAZZ — MIGRATION 20: VERDICT ROW LOCK (P1)
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..19. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   quizeazz_apply_judge0_verdict documents "One verdict per answer.
--   Never overwrite a stored verdict." The guard that enforces this read
--   the answer row WITHOUT a lock:
--
--       SELECT a.evaluation_status INTO v_current_status
--       FROM public.answers a
--       WHERE a.id = p_answer_id;
--
--   Under READ COMMITTED that guard is a TOCTOU check, not a guarantee.
--   Two concurrent graders can both read 'Pending Evaluation'; the second
--   UPDATE then blocks on the first's row lock, re-checks a WHERE clause
--   that still matches (id and attempt_id are unchanged), and OVERWRITES
--   the committed verdict.
--
--   Concurrent graders are reachable in production, not hypothetical:
--     * a browser-triggered invocation runs server-side to the 150s Edge
--       limit even after the browser gives up at 45s, while the sweep's
--       60s staleness guard can already have picked the same attempt up;
--     * pg_cron fires every 2 minutes and does not wait (net.http_post has
--       timeout_milliseconds := 10000), so one sweep can overlap the next;
--     * a 5xx (rather than a client-side abort) makes judge0Client retry,
--       adding further concurrent invocations for the same attempt.
--
-- WHAT THIS FILE DOES
--   Recreates quizeazz_apply_judge0_verdict with an IDENTICAL signature,
--   return type, SECURITY DEFINER setting, search_path and body, adding
--   exactly one token: FOR UPDATE on the existing guard SELECT.
--
-- WHY FOR UPDATE IS SUFFICIENT (and why no conditional UPDATE is added)
--   PostgreSQL docs, Explicit Locking, "FOR UPDATE": a SELECT FOR UPDATE
--   "will wait for a concurrent transaction that has run any of those
--   commands on the same row, and will then lock and return the updated
--   row". Under READ COMMITTED (confirmed on this database) the losing
--   grader therefore RESUMES with the committed evaluation_status, the
--   existing "<> 'Pending Evaluation'" branch fires, and it RAISEs.
--
--   Adding "AND evaluation_status = 'Pending Evaluation'" to the UPDATE
--   would be actively harmful: the function returns TRUE unconditionally,
--   so a filtered-out row would yield a silent no-op returning success
--   instead of a loud, traceable error. The lock alone is sufficient; the
--   UPDATE predicate is deliberately left unchanged.
--
-- DEADLOCK SAFETY
--   Lock order becomes: answers (FOR UPDATE) -> BEFORE-UPDATE trigger
--   check_answer_write_eligibility, which reads exam_attempts WITHOUT any
--   row lock. The submit path orders exam_attempts -> answers. Because the
--   trigger never locks exam_attempts, MVCC reads do not block and no
--   cycle can form.
--
-- NOTHING ELSE IS TOUCHED: no table, no column, no RLS policy, no other
-- function, no grant. CREATE OR REPLACE preserves the existing ACL
-- exactly (service_role only; anon/authenticated/PUBLIC remain revoked).
--
-- IDEMPOTENT: re-runnable.
-- ====================================================================


-- ====================================================================
-- SECTION A — verdict writer takes the row lock before its guard check
-- --------------------------------------------------------------------
-- Unchanged guarantees (every one of these is identical to migration 9):
--   * outcome must come from the database's own allow-list
--   * the attempt must already be finalized (never in_progress)
--   * the answer must belong to that attempt and be a coding answer
--   * the answer must still be 'Pending Evaluation'
--   * marks are computed here from the outcome, never accepted from the
--     caller: only 'Correct' earns marks, everything else is exactly 0
--   * the student's source code is never modified
--
-- New: the guard SELECT locks the answer row for the rest of this
-- transaction, so "one verdict per answer" becomes a real guarantee under
-- concurrency rather than a check that two graders can both pass.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.quizeazz_apply_judge0_verdict(
  p_attempt_id UUID,
  p_answer_id UUID,
  p_submission_token TEXT,
  p_outcome TEXT,
  p_status_id INT,
  p_exit_code INT,
  p_memory INT,
  p_time NUMERIC,
  p_stdout TEXT,
  p_stderr TEXT,
  p_compile_output TEXT,
  p_message TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_status TEXT;
  v_exam_id UUID;
  v_marks_total INT := 0;
  v_marks_awarded INT := 0;
  v_is_correct BOOLEAN := FALSE;
  v_current_status TEXT;
BEGIN
  IF p_outcome IS NULL OR p_outcome NOT IN (
    'Correct', 'Wrong Answer', 'Compilation Error', 'Runtime Error',
    'Time Limit Exceeded', 'Memory Limit Exceeded', 'System Error',
    'Not Attempted'
  ) THEN
    RAISE EXCEPTION 'Refusing to apply an unknown outcome: %', p_outcome;
  END IF;

  SELECT att.status, att.exam_id
  INTO v_status, v_exam_id
  FROM public.exam_attempts att
  WHERE att.id = p_attempt_id;

  IF v_status IS NULL THEN
    RAISE EXCEPTION 'Attempt % does not exist.', p_attempt_id;
  END IF;

  IF v_status = 'in_progress' THEN
    RAISE EXCEPTION
      'Refusing to apply a verdict to attempt %: the examination is still in progress.', p_attempt_id;
  END IF;

  SELECT COALESCE(eq.marks, 0)
  INTO v_marks_total
  FROM public.answers a
  JOIN public.exam_questions eq
    ON eq.question_id = a.question_id
   AND eq.exam_id = v_exam_id
  WHERE a.id = p_answer_id
    AND a.attempt_id = p_attempt_id
    AND EXISTS (SELECT 1 FROM public.questions q
                 WHERE q.id = a.question_id AND q.question_type = 'coding');

  IF v_marks_total IS NULL THEN
    RAISE EXCEPTION
      'Answer % is not a coding answer of attempt %.', p_answer_id, p_attempt_id;
  END IF;

  -- One verdict per answer. Never overwrite a stored verdict.
  --
  -- FOR UPDATE serializes concurrent graders on this row: under READ
  -- COMMITTED a loser waits here, then reads the committed value and falls
  -- into the RAISE below. The UPDATE predicate is deliberately unchanged;
  -- see the header for why adding a status predicate would turn a loud
  -- error into a silent success.
  SELECT a.evaluation_status INTO v_current_status
  FROM public.answers a
  WHERE a.id = p_answer_id
    FOR UPDATE;

  IF v_current_status IS NULL THEN
    RAISE EXCEPTION 'Answer % does not exist.', p_answer_id;
  END IF;

  IF v_current_status <> 'Pending Evaluation' THEN
    RAISE EXCEPTION
      'Answer % already has a verdict (%) and will not be overwritten.', p_answer_id, v_current_status;
  END IF;

  -- Marks are decided here. 'Correct' earns the full marks; every other
  -- objective verdict earns exactly zero. There is no partial credit.
  v_marks_awarded := public.quizeazz_marks_for_outcome(p_outcome, v_marks_total);
  v_is_correct := (p_outcome = 'Correct');

  -- The anti-tamper triggers permit this write only under the authoritative
  -- flag, which is not reachable from any client.
  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.answers a
  SET
    is_correct = v_is_correct,
    marks_awarded = v_marks_awarded,
    evaluation_status = p_outcome,
    -- The Judge0 result is recorded for invigilator review. The student's
    -- own source code and language are left exactly as submitted.
    evaluation_details = jsonb_build_object(
      'judge0_token', p_submission_token,
      'judge0_status_id', p_status_id,
      'judge0_status', p_outcome,
      'exit_code', p_exit_code,
      'memory_kb', p_memory,
      'time', p_time,
      'stdout', left(COALESCE(p_stdout, ''), 8000),
      'stderr', left(COALESCE(p_stderr, ''), 8000),
      'compile_output', left(COALESCE(p_compile_output, ''), 8000),
      'message', left(COALESCE(p_message, ''), 2000),
      'graded_by', 'judge0-ce',
      'graded_at', now()
    ),
    saved_at = now()
  WHERE a.id = p_answer_id
    AND a.attempt_id = p_attempt_id;

  RETURN TRUE;
END;
$$;


-- ====================================================================
-- SECTION B — VERIFICATION (read-only; run AFTER applying)
-- --------------------------------------------------------------------
-- B1. The guard SELECT in the LIVE function carries FOR UPDATE:
-- SELECT p.prosrc FROM pg_proc p
--   JOIN pg_namespace n ON n.oid = p.pronamespace
--  WHERE n.nspname = 'public'
--    AND p.proname = 'quizeazz_apply_judge0_verdict';
-- (expect "WHERE a.id = p_answer_id" followed by "FOR UPDATE")
--
-- B2. The verdict UPDATE did NOT gain a status predicate (it must remain
--     keyed only on id + attempt_id, so it cannot silently match 0 rows
--     while the function still returns TRUE):
-- SELECT prosrc ~ 'AND\s+a\.evaluation_status\s*=\s*''Pending Evaluation'''
--   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--  WHERE n.nspname = 'public'
--    AND p.proname = 'quizeazz_apply_judge0_verdict';
-- (expect: false)
--
-- B3. The ACL is unchanged - CREATE OR REPLACE must not have widened it:
-- SELECT has_function_privilege('anon', p.oid, 'EXECUTE')       AS anon,
--        has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth,
--        has_function_privilege('service_role', p.oid, 'EXECUTE') AS svc
--   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--  WHERE n.nspname = 'public'
--    AND p.proname = 'quizeazz_apply_judge0_verdict';
-- (expect: anon=false, auth=false, svc=true)
--
-- B4. The signature and return type are unchanged:
-- SELECT pg_get_function_result(p.oid)
--   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--  WHERE n.nspname = 'public'
--    AND p.proname = 'quizeazz_apply_judge0_verdict';
-- (expect: boolean)
-- ====================================================================


-- ====================================================================
-- END OF MIGRATION 20
-- ====================================================================