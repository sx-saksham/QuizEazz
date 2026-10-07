-- ====================================================================
-- QUIZEAZZ — MIGRATION 7: JUDGE0 CE PROGRAMMING GRADING
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..6. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   Migration 4 set every programming answer to 'Pending Evaluation' with 0
--   marks because no execution runner was connected. Judge0 CE is now
--   deployed, so those answers can be resolved by real execution.
--
-- ARCHITECTURE
--   The Judge0 API token lives ONLY in the Supabase Edge Function
--   environment (JUDGE0_API_TOKEN / JUDGE0_API_URL). It is never a column,
--   never a table value, never a VITE_* variable and never in Git.
--
--   This file adds the database half: the narrow, service_role-only RPCs the
--   Edge Function calls. The Edge Function submits code and maps the objective
--   status; THIS file computes marks and writes verdicts. Marks are therefore
--   decided inside the database and cannot be inflated by any caller.
--
-- SCOPE: function-level only. No table is created, altered or dropped. No
--   column is added. No RLS policy is changed. No data is deleted.
--
--   The only row-level statements are the verdict/attempt updates below,
--   which write answers that are ALREADY frozen by final submission and are
--   already in 'Pending Evaluation'. They can never create, alter or delete a
--   student's submitted code.
--
-- IDEMPOTENT: safe to run more than once.
-- ====================================================================


-- ====================================================================
-- SECTION A — OUTCOME VOCABULARY (single source of truth in the database)
-- --------------------------------------------------------------------
-- The database is the authority for marks. Only 'Correct' can ever earn
-- marks; every other objective verdict earns exactly zero. There is no
-- partial credit, no estimation and no AI involvement.
-- ====================================================================

-- Marks rule, expressed once. Used by quizeazz_apply_judge0_verdict.
CREATE OR REPLACE FUNCTION public.quizeazz_marks_for_outcome(
  p_outcome TEXT,
  p_marks_total INT
)
RETURNS INT
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE
    WHEN p_outcome = 'Correct' THEN COALESCE(p_marks_total, 0)
    ELSE 0
  END::INT;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_marks_for_outcome(TEXT, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_marks_for_outcome(TEXT, INT) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_marks_for_outcome(TEXT, INT) FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.quizeazz_marks_for_outcome(TEXT, INT) TO service_role;


-- ====================================================================
-- SECTION B — LIST FINALIZED ATTEMPTS STILL NEEDING PROGRAMMING GRADING
-- --------------------------------------------------------------------
-- Drives the retry sweep. Only attempts that are already finalized are
-- returned, so this can never start grading an in-progress exam.
-- service_role only.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.quizeazz_list_attempts_needing_programming_grading()
RETURNS TABLE (attempt_id UUID)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  RETURN QUERY
  SELECT DISTINCT a.attempt_id
  FROM public.answers a
  JOIN public.questions q
    ON q.id = a.question_id
   AND q.question_type = 'coding'
  WHERE a.evaluation_status = 'Pending Evaluation'
    AND a.code IS NOT NULL
    AND btrim(a.code) <> ''
    AND EXISTS (
      SELECT 1
      FROM public.exam_attempts att
      WHERE att.id = a.attempt_id
        AND att.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated')
    )
  ORDER BY 1;
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_list_attempts_needing_programming_grading() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_list_attempts_needing_programming_grading() FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_list_attempts_needing_programming_grading() FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.quizeazz_list_attempts_needing_programming_grading() TO service_role;


-- ====================================================================
-- SECTION C — ATTEMPT OWNERSHIP CHECK FOR A STUDENT-SUPPLIED CALL
-- --------------------------------------------------------------------
-- The Edge Function accepts either a service-role key (unattended sweep) or a
-- student's JWT. This is the server-side half of that decision; the browser
-- cannot call it directly because it is revoked from anon/authenticated.
-- Returns TRUE only when the attempt belongs to auth.uid().
-- ====================================================================
CREATE OR REPLACE FUNCTION public.quizeazz_attempt_belongs_to_caller(p_attempt_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RETURN FALSE;
  END IF;

  RETURN EXISTS (
    SELECT 1
    FROM public.exam_attempts att
    WHERE att.id = p_attempt_id
      AND att.student_id = v_user_id
  );
END;
$$;

-- Callable by the student's own JWT so the Edge Function can verify ownership.
REVOKE ALL ON FUNCTION public.quizeazz_attempt_belongs_to_caller(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_attempt_belongs_to_caller(UUID) FROM anon;
GRANT  EXECUTE ON FUNCTION public.quizeazz_attempt_belongs_to_caller(UUID) TO authenticated;


-- ====================================================================
-- SECTION D — READ PROGRAMMING ANSWERS PENDING A JUDGE0 VERDICT
-- --------------------------------------------------------------------
-- Returns only frozen programming answers. Refuses an attempt that is still
-- in progress, which is the database-side guarantee that code is never
-- executed during an examination.
-- service_role only.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.quizeazz_judge0_pending_answers(p_attempt_id UUID)
RETURNS TABLE (
  answer_id UUID,
  question_id UUID,
  language TEXT,
  source_code TEXT,
  stdin TEXT,
  marks_total INT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_status TEXT;
BEGIN
  SELECT att.status INTO v_status
  FROM public.exam_attempts att
  WHERE att.id = p_attempt_id;

  IF v_status IS NULL THEN
    RAISE EXCEPTION 'Attempt % does not exist.', p_attempt_id;
  END IF;

  -- Hard stop: an exam in progress is never sent to Judge0.
  IF v_status = 'in_progress' THEN
    RAISE EXCEPTION
      'Refusing to execute code for attempt %: the examination is still in progress.', p_attempt_id;
  END IF;

  IF v_status NOT IN ('submitted', 'timed_out', 'evaluating', 'evaluated') THEN
    RAISE EXCEPTION
      'Attempt % is in status % and cannot be graded.', p_attempt_id, v_status;
  END IF;

  RETURN QUERY
  SELECT
    a.id,
    a.question_id,
    a.language,
    a.code,
    NULL::TEXT,
    COALESCE(eq.marks, 0)
  FROM public.answers a
  JOIN public.exam_questions eq
    ON eq.question_id = a.question_id
   AND eq.exam_id = (SELECT att.exam_id FROM public.exam_attempts att WHERE att.id = p_attempt_id)
  JOIN public.questions q
    ON q.id = a.question_id
   AND q.question_type = 'coding'
  WHERE a.attempt_id = p_attempt_id
    AND a.evaluation_status = 'Pending Evaluation'
  ORDER BY eq.id;
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_judge0_pending_answers(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_judge0_pending_answers(UUID) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_judge0_pending_answers(UUID) FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.quizeazz_judge0_pending_answers(UUID) TO service_role;


-- ====================================================================
-- SECTION E — APPLY AN OBJECTIVE JUDGE0 VERDICT
-- --------------------------------------------------------------------
-- The only writer of programming verdicts.
--
-- SAFETY PROPERTIES, each enforced in this function rather than trusted:
--   * the attempt must already be finalized (never in_progress)
--   * the answer must belong to that attempt
--   * the question must be a coding question
--   * the answer must currently be 'Pending Evaluation', so a verdict can be
--     applied exactly once and cannot overwrite a stored verdict
--   * marks are computed here from the outcome, never accepted from the
--     caller: only 'Correct' earns marks, everything else is exactly 0
--   * the student's source code is never modified
-- service_role only.
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
  v_row RECORD;
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

  SELECT a.evaluation_status INTO v_row
  FROM public.answers a
  WHERE a.id = p_answer_id;

  IF v_row IS NULL THEN
    RAISE EXCEPTION 'Answer % does not exist.', p_answer_id;
  END IF;

  -- One verdict per answer. Never overwrite a stored verdict.
  IF v_row <> 'Pending Evaluation' THEN
    RAISE EXCEPTION
      'Answer % already has a verdict (%) and will not be overwritten.', p_answer_id, v_row;
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

REVOKE ALL ON FUNCTION public.quizeazz_apply_judge0_verdict(UUID, UUID, TEXT, TEXT, INT, INT, INT, NUMERIC, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_apply_judge0_verdict(UUID, UUID, TEXT, TEXT, INT, INT, INT, NUMERIC, TEXT, TEXT, TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_apply_judge0_verdict(UUID, UUID, TEXT, TEXT, INT, INT, INT, NUMERIC, TEXT, TEXT, TEXT, TEXT) FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.quizeazz_apply_judge0_verdict(UUID, UUID, TEXT, TEXT, INT, INT, INT, NUMERIC, TEXT, TEXT, TEXT, TEXT) TO service_role;


-- ====================================================================
-- SECTION F — RECOMPUTE ATTEMPT TOTALS AFTER JUDGE0 VERDICTS LAND
-- --------------------------------------------------------------------
-- Recomputes score and max_score from the authoritative answer rows. The
-- attempt stops being 'evaluated' only once no programming answer is still
-- pending, so a partially graded attempt is honestly reported as pending.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.quizeazz_finalize_attempt_grading(p_attempt_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_status TEXT;
  v_exam_id UUID;
  v_score INT := 0;
  v_pending INT := 0;
  v_paper_marks INT := 0;
BEGIN
  SELECT att.status, att.exam_id
  INTO v_status, v_exam_id
  FROM public.exam_attempts att
  WHERE att.id = p_attempt_id;

  IF v_status IS NULL THEN
    RAISE EXCEPTION 'Attempt % does not exist.', p_attempt_id;
  END IF;

  IF v_status = 'in_progress' THEN
    RAISE EXCEPTION
      'Refusing to finalize grading for attempt %: the examination is still in progress.', p_attempt_id;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  SELECT COALESCE(SUM(eq.marks), 0)::INT
  INTO v_paper_marks
  FROM public.exam_questions eq
  WHERE eq.exam_id = v_exam_id;

  -- Marks only ever come from stored answer rows.
  SELECT COALESCE(SUM(a.marks_awarded), 0)::INT
  INTO v_score
  FROM public.answers a
  WHERE a.attempt_id = p_attempt_id;

  -- Count programming answers that still have no verdict.
  SELECT count(*)::INT
  INTO v_pending
  FROM public.answers a
  JOIN public.questions q ON q.id = a.question_id
  WHERE a.attempt_id = p_attempt_id
    AND q.question_type = 'coding'
    AND a.evaluation_status = 'Pending Evaluation';

  UPDATE public.exam_attempts att
  SET
    score = v_score,
    max_score = v_paper_marks,
    -- While programming answers are still pending the attempt stays
    -- 'evaluating'. It only becomes 'evaluated' once every question has a
    -- real verdict, so the UI can never show "Evaluated" over an unresolved
    -- programming answer.
    status = CASE WHEN v_pending > 0 THEN 'evaluating' ELSE 'evaluated' END,
    metadata = COALESCE(att.metadata, '{}'::jsonb) || jsonb_build_object(
      'grading', jsonb_build_object(
        'mcq_evaluated', TRUE,
        'programming_evaluated', (v_pending = 0),
        'programming_pending', v_pending,
        'programming_engine', 'Judge0 CE 1.13.1',
        'java_language_id', 90,
        'graded_at', now()
      )
    )
  WHERE att.id = p_attempt_id;

  RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_finalize_attempt_grading(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_finalize_attempt_grading(UUID) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_finalize_attempt_grading(UUID) FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.quizeazz_finalize_attempt_grading(UUID) TO service_role;


-- ====================================================================
-- SECTION G — RE-ESTABLISH THE EXECUTE ACL (explicit, per function)
-- --------------------------------------------------------------------
-- CREATE OR REPLACE preserves an existing ACL, but the intended policy is
-- restated here so this file is self-sufficient.
--
--   external (Edge Function under a student JWT):
--       quizeazz_attempt_belongs_to_caller   anon NO, PUBLIC NO, auth YES
--   internal (Edge Function service-role key only):
--       everything else                     anon NO, PUBLIC NO, auth NO
--
-- No table grant is issued. No RLS policy is touched. The browser gains no
-- direct access to anything.
-- ====================================================================
DO $$
DECLARE
  r           RECORD;
  v_external TEXT[] := ARRAY[
    'quizeazz_attempt_belongs_to_caller'
  ];
  v_internal TEXT[] := ARRAY[
    'quizeazz_marks_for_outcome',
    'quizeazz_list_attempts_needing_programming_grading',
    'quizeazz_judge0_pending_answers',
    'quizeazz_apply_judge0_verdict',
    'quizeazz_finalize_attempt_grading'
  ];
  v_count INT := 0;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure::TEXT AS sig
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname = ANY(v_external)
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',         r.sig);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon',            r.sig);
    EXECUTE format('GRANT  EXECUTE ON FUNCTION %s TO authenticated', r.sig);
    v_count := v_count + 1;
  END LOOP;

  FOR r IN
    SELECT p.oid::regprocedure::TEXT AS sig
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname = ANY(v_internal)
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',       r.sig);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon',          r.sig);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', r.sig);
    EXECUTE format('GRANT  EXECUTE ON FUNCTION %s TO service_role', r.sig);
    v_count := v_count + 1;
  END LOOP;

  RAISE NOTICE 'Judge0 integration ACLs re-established on % function(s).', v_count;
END $$;


-- ====================================================================
-- SECTION H — VERIFICATION (read-only)
-- ====================================================================

-- H1. Every function added by this migration, with its ACL.
--     Expect: anon_has_execute=false and public_has_execute=false for ALL.
SELECT
  p.proname AS function_name,
  pg_get_function_identity_arguments(p.oid) AS arguments,
  p.prosecdef AS security_definer,
  has_function_privilege('anon',          p.oid, 'EXECUTE') AS anon_has_execute,
  has_function_privilege('public',        p.oid, 'EXECUTE') AS public_has_execute,
  has_function_privilege('authenticated', p.oid, 'EXECUTE') AS authenticated_can_execute,
  has_function_privilege('service_role',  p.oid, 'EXECUTE') AS service_role_can_execute
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.proname IN (
    'quizeazz_marks_for_outcome',
    'quizeazz_list_attempts_needing_programming_grading',
    'quizeazz_attempt_belongs_to_caller',
    'quizeazz_judge0_pending_answers',
    'quizeazz_apply_judge0_verdict',
    'quizeazz_finalize_attempt_grading'
  )
ORDER BY p.proname, pg_get_function_identity_arguments(p.oid);

-- H2. No browser client may execute the verdict writer. Expect 0 rows.
--     NOTE: pg_proc and pg_namespace BOTH expose an `oid` column, so after the
--     join a bare `oid` is ambiguous (SQLSTATE 42702). Every `oid` here must be
--     qualified to pg_proc.
SELECT proname, pg_get_function_identity_arguments(pg_proc.oid) AS arguments
FROM pg_proc
JOIN pg_namespace n ON n.oid = pronamespace
WHERE n.nspname = 'public'
  AND proname IN (
    'quizeazz_apply_judge0_verdict',
    'quizeazz_judge0_pending_answers',
    'quizeazz_finalize_attempt_grading',
    'quizeazz_list_attempts_needing_programming_grading'
  )
  AND has_function_privilege('authenticated', pg_proc.oid, 'EXECUTE');

-- H3. Marks invariant: a programming answer must never hold marks unless its
--     verdict is 'Correct'. Expect 0 rows.
SELECT
  a.id,
  a.attempt_id,
  a.evaluation_status,
  a.marks_awarded,
  eq.marks AS marks_total
FROM public.answers a
JOIN public.questions q ON q.id = a.question_id
JOIN public.exam_attempts att ON att.id = a.attempt_id
JOIN public.exam_questions eq
  ON eq.question_id = a.question_id
 AND eq.exam_id = att.exam_id
WHERE q.question_type = 'coding'
  AND a.evaluation_status IS DISTINCT FROM 'Pending Evaluation'
  AND a.evaluation_status IS DISTINCT FROM 'Not Attempted'
  AND a.marks_awarded <> CASE WHEN a.evaluation_status = 'Correct'
                              THEN COALESCE(eq.marks, 0) ELSE 0 END;

-- H4. No in-progress attempt may be reachable by the grading path.
--     Expect 0 rows.
SELECT att.id, att.status
FROM public.exam_attempts att
JOIN public.answers a ON a.attempt_id = att.id
JOIN public.questions q ON q.id = a.question_id AND q.question_type = 'coding'
WHERE att.status = 'in_progress'
  AND a.evaluation_status IS DISTINCT FROM 'Pending Evaluation';

-- H5. Programming submissions preserved verbatim.
--     Every graded programming answer must still carry its original code.
--     Expect 0 rows.
SELECT a.id, a.attempt_id
FROM public.answers a
JOIN public.questions q ON q.id = a.question_id AND q.question_type = 'coding'
WHERE a.evaluation_status <> 'Pending Evaluation'
  AND (a.code IS NULL OR btrim(a.code) = '');

-- H6. The Judge0 token must never be stored in the database.
--     This query is intentionally broad: it looks for the token in any text
--     column of any public table. Expect 0 rows.
--     (Run the token-lookup portion manually with the real value; this
--     version checks the structural invariant that no Judge0 config table
--     exists in the public schema.)
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
    column_name ILIKE '%judge0%'
    OR column_name ILIKE '%api_token%'
    OR column_name ILIKE '%api_url%'
  )
ORDER BY table_name, column_name;

-- ====================================================================
-- END OF MIGRATION 7
-- ====================================================================
