-- ====================================================================
-- QUIZEAZZ — MIGRATION 8: JUDGE0 PENDING-ANSWERS MARKS TYPE FIX
-- --------------------------------------------------------------------
-- Fixes production ERROR 42804 on:
--   SELECT * FROM public.quizeazz_judge0_pending_answers('<attempt>');
-- Root cause: function declared column 6 as INT, but
--   COALESCE(eq.marks, 0) returns NUMERIC because
--   public.exam_questions.marks is NUMERIC.
-- Fix: re-create the function with column 6 as plain NUMERIC,
--   matching the underlying column. No truncation, no data change.
--
-- PostgreSQL rule: CREATE OR REPLACE cannot change a function's return
-- type (42P13), and ALTER FUNCTION cannot alter RETURNS TABLE.
-- Hence DROP + CREATE is required. The signature
--   quizeazz_judge0_pending_answers(UUID)
-- is unchanged, so all callers keep working.
--
-- Preserves: SECURITY DEFINER, search_path, body, service-role-only ACL.
-- Modifies no data, no other RPC, no exam_questions.marks.
-- Safe to re-run (DROP IF EXISTS + CREATE + explicit ACL).
-- ====================================================================

DROP FUNCTION IF EXISTS public.quizeazz_judge0_pending_answers(UUID);

CREATE FUNCTION public.quizeazz_judge0_pending_answers(p_attempt_id UUID)
RETURNS TABLE (
  answer_id UUID,
  question_id UUID,
  language TEXT,
  source_code TEXT,
  stdin TEXT,
  marks_total NUMERIC
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
-- VERIFICATION (read-only; run after applying)
-- ====================================================================

-- V1. Function now returns the pending C++ answer (was 42804 before fix).
--     Expect 1 row for attempt 92abe954-1e92-48cf-b865-1696a7df0256.
-- SELECT * FROM public.quizeazz_judge0_pending_answers(
--   '92abe954-1e92-48cf-b865-1696a7df0256');

-- V2. Column 6 is NUMERIC.
-- SELECT p.proname, pg_get_function_result(p.oid) AS result_type
-- FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'public' AND p.proname = 'quizeazz_judge0_pending_answers';

-- V3. SECURITY DEFINER still enabled (expect prosecdef = true).
-- SELECT p.proname, p.prosecdef AS security_definer
-- FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'public' AND p.proname = 'quizeazz_judge0_pending_answers';

-- V4. search_path still public, pg_temp.
-- SELECT p.proname, p.proconfig AS search_path_config
-- FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'public' AND p.proname = 'quizeazz_judge0_pending_answers';

-- V5. anon/PUBLIC denied, service_role allowed; authenticated denied.
-- SELECT has_function_privilege('anon', p.oid, 'EXECUTE') AS anon_exec,
--        has_function_privilege('public', p.oid, 'EXECUTE') AS public_exec,
--        has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth_exec,
--        has_function_privilege('service_role', p.oid, 'EXECUTE') AS svc_exec
-- FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'public' AND p.proname = 'quizeazz_judge0_pending_answers';
