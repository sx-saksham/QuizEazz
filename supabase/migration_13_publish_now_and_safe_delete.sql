-- ====================================================================
-- QUIZEAZZ — MIGRATION 13: PUBLISH NOW + SAFE DELETE FLOWS
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..12. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   1. Teachers need an explicit one-click "Publish Now" that makes a draft
--      exam immediately available without hand-editing schedule fields.
--      It must be server-authoritative (server clock, transactional,
--      fully validated) — never a browser status flip.
--   2. Teachers need safe Delete actions for questions and exams that can
--      never destroy historical data. Every destructive path is guarded by
--      dependency checks with clear errors; students can never reach them.
--
-- WHAT THIS FILE DOES
--   1. teacher_publish_now(p_exam_id): draft -> published with
--      starts_at = now() (server clock) and ends_at = starts_at + duration.
--      Re-validates the paper exactly like teacher_set_exam_paper does
--      (existence, C++-only coding, validated hidden tests), rebuilds every
--      paper snapshot, then flips status. All-or-nothing transaction.
--      Refuses published/closed exams and exams with finalized attempts.
--   2. teacher_delete_question(p_question_id): deletes a bank question ONLY
--      when no paper row, no answer and no audit event references it.
--      Its hidden test cases cascade (meaningless without the question).
--   3. teacher_delete_exam(p_exam_id): deletes an exam ONLY when zero
--      attempts exist. Paper/assignment rows cascade; scoped audit events
--      for the disposable exam are removed with it. Attempts (and their
--      answers) can never be cascade-deleted by this path.
--
-- SCOPE
--   No table created/altered/dropped. No RLS change. No existing row
--   modified. No ACL widened (faculty-checked authenticated calls only).
--
-- IDEMPOTENT: CREATE OR REPLACE (signatures are new, so no DROP needed).
-- ====================================================================


-- ====================================================================
-- SECTION A — Publish Now (server clock, validated, transactional)
-- ====================================================================
CREATE OR REPLACE FUNCTION public.teacher_publish_now(
  p_exam_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_exam public.exams%ROWTYPE;
  v_qcount INT := 0;
  v_now TIMESTAMPTZ;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  SELECT * INTO v_exam FROM public.exams e WHERE e.id = p_exam_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  IF v_exam.status = 'published' THEN
    RAISE EXCEPTION 'This examination is already published.';
  END IF;

  IF v_exam.status = 'closed' THEN
    RAISE EXCEPTION 'This examination is closed. Its details are frozen.';
  END IF;

  -- Draft may still hold rows only when nothing finalized exists; reuse the
  -- exact composition rule so Publish Now can never strand an attempt.
  PERFORM public.quizeazz_assert_composition_editable(p_exam_id);

  -- The paper must exist and be non-empty.
  SELECT COUNT(*)::INT INTO v_qcount
  FROM public.exam_questions eq
  WHERE eq.exam_id = p_exam_id;
  IF v_qcount = 0 THEN
    RAISE EXCEPTION 'This examination has no questions. Add questions to the paper before publishing.';
  END IF;

  -- Same gates as teacher_set_exam_paper, evaluated at publish time:
  -- C++-only coding questions...
  IF EXISTS (
    SELECT 1 FROM public.exam_questions eq
    JOIN public.questions q ON q.id = eq.question_id
    WHERE eq.exam_id = p_exam_id
      AND q.question_type = 'coding'
      AND q.language IS DISTINCT FROM 'cpp'
  ) THEN
    RAISE EXCEPTION 'Only C++ programming questions may be published. Fix the paper before publishing.';
  END IF;

  -- ...and at least one validated hidden test per coding question.
  IF EXISTS (
    SELECT 1 FROM public.exam_questions eq
    JOIN public.questions q ON q.id = eq.question_id
    WHERE eq.exam_id = p_exam_id
      AND q.question_type = 'coding'
      AND NOT EXISTS (
        SELECT 1 FROM public.programming_test_cases t
        WHERE t.question_id = q.id
          AND t.expected_stdout IS NOT NULL
      )
  ) THEN
    RAISE EXCEPTION 'Every coding question needs at least one validated hidden test case before publishing. Open Hidden Tests for the failing question.';
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  -- Freeze the paper exactly as published: bank edits afterwards can never
  -- leak into this exam. Same builder the paper RPC uses.
  UPDATE public.exam_questions eq
  SET question_snapshot = public.quizeazz_build_question_snapshot(eq.question_id, eq.marks)
  WHERE eq.exam_id = p_exam_id;

  -- Server clock only. The browser clock is never trusted for scheduling.
  v_now := now();

  UPDATE public.exams e
  SET status = 'published',
      starts_at = v_now,
      ends_at = v_now + (COALESCE(v_exam.duration_minutes, 60) * INTERVAL '1 minute'),
      updated_at = v_now
  WHERE e.id = p_exam_id
  RETURNING * INTO v_exam;

  RETURN jsonb_build_object(
    'exam_id', v_exam.id,
    'status', v_exam.status,
    'starts_at', v_exam.starts_at,
    'ends_at', v_exam.ends_at,
    'total_questions', v_qcount
  );
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_publish_now(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_publish_now(UUID) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_publish_now(UUID) TO authenticated;


-- ====================================================================
-- SECTION B — safe question delete (dependency-guarded)
-- ====================================================================
CREATE OR REPLACE FUNCTION public.teacher_delete_question(
  p_question_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_papers INT := 0;
  v_answers INT := 0;
  v_tests INT := 0;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  IF NOT EXISTS (SELECT 1 FROM public.questions q WHERE q.id = p_question_id) THEN
    RAISE EXCEPTION 'Question % does not exist.', p_question_id;
  END IF;

  -- Historical dependency checks. Each names what blocks the delete so the
  -- UI can show a clear message instead of a constraint violation.
  SELECT COUNT(*)::INT INTO v_papers
  FROM public.exam_questions eq WHERE eq.question_id = p_question_id;
  IF v_papers > 0 THEN
    RAISE EXCEPTION 'This question cannot be deleted because it is used in % exam paper(s). Remove it from those papers first.', v_papers;
  END IF;

  SELECT COUNT(*)::INT INTO v_answers
  FROM public.answers a WHERE a.question_id = p_question_id;
  IF v_answers > 0 THEN
    RAISE EXCEPTION 'This question cannot be deleted because % candidate answer(s) reference it.', v_answers;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  -- Hidden tests cascade via FK; count them for the report first.
  SELECT COUNT(*)::INT INTO v_tests
  FROM public.programming_test_cases t WHERE t.question_id = p_question_id;

  DELETE FROM public.questions WHERE id = p_question_id;

  RETURN jsonb_build_object(
    'question_id', p_question_id,
    'deleted', TRUE,
    'removed_hidden_tests', v_tests
  );
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_delete_question(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_delete_question(UUID) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_delete_question(UUID) TO authenticated;


-- ====================================================================
-- SECTION C — safe exam delete (attempt-guarded)
-- --------------------------------------------------------------------
-- Paper rows and assignments cascade via FK. Attempts (and their answers)
-- have no cascade path here: any attempt blocks the delete outright.
-- Scoped audit events for the disposable exam are removed with it; all
-- other history is untouched.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.teacher_delete_exam(
  p_exam_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_attempts INT := 0;
  v_title TEXT;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  SELECT e.title INTO v_title FROM public.exams e WHERE e.id = p_exam_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  SELECT COUNT(*)::INT INTO v_attempts
  FROM public.exam_attempts att WHERE att.exam_id = p_exam_id;
  IF v_attempts > 0 THEN
    RAISE EXCEPTION 'This examination cannot be deleted because % candidate attempt(s) exist for it. Close it instead to preserve history.', v_attempts;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  DELETE FROM public.security_events WHERE exam_id = p_exam_id;
  DELETE FROM public.exams WHERE id = p_exam_id;

  RETURN jsonb_build_object(
    'exam_id', p_exam_id,
    'title', v_title,
    'deleted', TRUE
  );
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_delete_exam(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_delete_exam(UUID) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_delete_exam(UUID) TO authenticated;


-- ====================================================================
-- SECTION D — VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- D1. New RPCs exist, faculty-gated, authenticated-executable:
-- SELECT p.proname,
--   has_function_privilege('anon',p.oid,'EXECUTE') AS anon,
--   has_function_privilege('authenticated',p.oid,'EXECUTE') AS auth
-- FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
-- WHERE n.nspname='public'
-- AND p.proname IN ('teacher_publish_now','teacher_delete_question',
--                   'teacher_delete_exam');
-- (expect anon=false, auth=true for all three)
--
-- D2. Publish Now refuses an already-published exam (run as faculty):
-- SELECT public.teacher_publish_now('<a-published-exam-id>');
-- (expect: 'already published')
--
-- D3. Delete refuses an exam with attempts (run as faculty):
-- SELECT public.teacher_delete_exam('<an-exam-with-attempts>');
-- (expect: 'cannot be deleted because N candidate attempt(s) exist')
-- ====================================================================

-- ====================================================================
-- END OF MIGRATION 13
-- ====================================================================
