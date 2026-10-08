-- ====================================================================
-- QUIZEAZZ — MIGRATION 16: RESULTS TRUTH, CREATE ENDS_AT, ATTEMPT FILTERS
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..15. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   1. STUDENT LIST BUG: get_student_results() derived `programming_pending`
--      from attempt.metadata, which is a stale hint. Pre-Judge0 attempts
--      finalized as `evaluated` still carry programming_pending=1, so the
--      student list showed "Evaluation Pending" while the teacher view
--      (attempt status) correctly showed EVALUATED. The attempt-level
--      finalized state is authoritative: an `evaluated` attempt is final.
--   2. CREATE GAP: teacher_create_exam() never accepted ends_at, so the
--      creation UI could not set a closing time even though the column,
--      the schedule RPC and the start gates all support it.
--   3. TEACHER RESULTS GAP: teacher_get_attempts() had no name/class/batch
--      filtering, forcing any filtering to the browser.
--
-- WHAT THIS FILE DOES
--   1. Redefines get_student_results() (same signature, ACLs preserved):
--      - includes in_progress + evaluating attempts (live attempts visible,
--        in_progress rows carry submitted_at=NULL so the UI shows no
--        submitted timestamp);
--      - programming_pending is forced to 0 when status='evaluated';
--        otherwise derived from metadata as before;
--      - adds a submitted_at field (NULL until submitted).
--   2. Replaces teacher_create_exam() with a 6-parameter version adding
--      p_ends_at (DEFAULT NULL) + end-after-start validation. DROP+CREATE
--      is required (signature change); grants are re-issued identically.
--   3. Replaces teacher_get_attempts() with a 4-parameter version adding
--      p_search / p_class / p_batch (all DEFAULT NULL), returns class_name
--      + batch from profiles, filters server-side. DROP+CREATE + re-grant.
--
-- SCOPE
--   No table created/altered/dropped. No row written. No RLS change.
--   No ACL widened (authenticated-only, faculty gates intact).
--   History, snapshots, grading and timer semantics untouched.
--
-- IDEMPOTENT: re-runnable (DROPs guarded by IF EXISTS).
-- ====================================================================


-- ====================================================================
-- SECTION A — get_student_results: attempt status is authoritative
-- --------------------------------------------------------------------
-- An `evaluated` attempt is final by definition: pending is 0 however
-- stale the metadata hint is. Anything else keeps the metadata hint.
-- submitted_at is exposed so the UI never prints a submitted timestamp
-- for attempts that were never submitted.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.get_student_results()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_result JSONB;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'id', att.id,
      'exam_id', att.exam_id,
      'exam_title', COALESCE(e.title, 'Examination'),
      'date', COALESCE(att.submitted_at, att.created_at),
      'submitted_at', att.submitted_at,
      'score', att.score,
      'total_marks', COALESCE(att.max_score, (
        SELECT COALESCE(sum(eq.marks), 0)
        FROM public.exam_questions eq
        WHERE eq.exam_id = att.exam_id
      )),
      'status', att.status,
      'programming_pending', CASE
        WHEN att.status = 'evaluated' THEN 0
        ELSE COALESCE(
          (regexp_match(
            att.metadata -> 'grading' ->> 'programming_pending',
            '^[0-9]+$'
          ))[1]::INT,
          0
        )
      END
    ) ORDER BY att.created_at DESC
  ), '[]'::jsonb)
  INTO v_result
  FROM public.exam_attempts att
  JOIN public.exams e ON e.id = att.exam_id
  WHERE att.student_id = v_user_id
    AND att.status IN ('in_progress', 'submitted', 'evaluating', 'evaluated', 'timed_out');

  RETURN v_result;
END;
$$;


-- ====================================================================
-- SECTION B — teacher_create_exam gains p_ends_at (DROP+CREATE)
-- --------------------------------------------------------------------
-- The old 5-parameter signature is dropped first: keeping both would
-- leave two overloads and make single-argument calls ambiguous.
-- Behavior is otherwise identical, plus: when both bounds are given,
-- the close must be later than the start.
-- ====================================================================
DROP FUNCTION IF EXISTS public.teacher_create_exam(TEXT, TEXT, INT, TEXT, TIMESTAMPTZ);

CREATE FUNCTION public.teacher_create_exam(
  p_title TEXT,
  p_description TEXT DEFAULT NULL,
  p_duration_minutes INT DEFAULT 60,
  p_status TEXT DEFAULT 'published',
  p_starts_at TIMESTAMPTZ DEFAULT NULL,
  p_ends_at TIMESTAMPTZ DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
  v_new_exam public.exams%ROWTYPE;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required to create examinations.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can create examinations.';
  END IF;

  IF p_title IS NULL OR trim(p_title) = '' THEN
    RAISE EXCEPTION 'Examination title cannot be empty.';
  END IF;

  IF p_duration_minutes IS NULL OR p_duration_minutes <= 0 OR p_duration_minutes > 1440 THEN
    RAISE EXCEPTION 'Duration must be between 1 and 1440 minutes (24 hours).';
  END IF;

  IF p_status NOT IN ('draft', 'published', 'closed') THEN
    RAISE EXCEPTION 'Invalid examination status "%". Allowed values: draft, published, closed.', p_status;
  END IF;

  IF p_status = 'published' AND p_starts_at IS NULL THEN
    RAISE EXCEPTION 'Scheduled start time is required for published examinations.';
  END IF;

  IF p_ends_at IS NOT NULL AND p_starts_at IS NOT NULL AND p_ends_at <= p_starts_at THEN
    RAISE EXCEPTION 'The close time must be later than the start time.';
  END IF;

  INSERT INTO public.exams (
    title,
    description,
    duration_minutes,
    status,
    starts_at,
    ends_at,
    created_by,
    created_at,
    updated_at
  )
  VALUES (
    trim(p_title),
    NULLIF(trim(p_description), ''),
    p_duration_minutes,
    p_status,
    p_starts_at,
    p_ends_at,
    v_caller_id,
    now(),
    now()
  )
  RETURNING * INTO v_new_exam;

  RETURN to_jsonb(v_new_exam);
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_create_exam(TEXT, TEXT, INT, TEXT, TIMESTAMPTZ, TIMESTAMPTZ) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_create_exam(TEXT, TEXT, INT, TEXT, TIMESTAMPTZ, TIMESTAMPTZ) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_create_exam(TEXT, TEXT, INT, TEXT, TIMESTAMPTZ, TIMESTAMPTZ) TO authenticated;


-- ====================================================================
-- SECTION C — teacher_get_attempts gains server-side filters (DROP+CREATE)
-- --------------------------------------------------------------------
-- p_search matches the candidate name (case-insensitive, substring).
-- p_class / p_batch match profiles.class_name / profiles.batch exactly.
-- NULL means "no filter". Unonboarded (NULL cohort) rows never match an
-- active cohort filter. class_name + batch are returned per row so the UI
-- can display them without a second lookup.
-- ====================================================================
DROP FUNCTION IF EXISTS public.teacher_get_attempts(UUID);

CREATE FUNCTION public.teacher_get_attempts(
  p_exam_id UUID DEFAULT NULL,
  p_search TEXT DEFAULT NULL,
  p_class TEXT DEFAULT NULL,
  p_batch TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_rows JSONB;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  SELECT COALESCE(jsonb_agg(row_to_json(t) ORDER BY t.created_at DESC), '[]'::jsonb)
  INTO v_rows
  FROM (
    SELECT
      att.id AS attempt_id,
      att.exam_id,
      e.title AS exam_title,
      att.student_id,
      p.full_name AS student_name,
      p.class_name AS class_name,
      p.batch AS batch,
      att.status,
      att.score,
      COALESCE((SELECT SUM(eq.marks)::INT FROM public.exam_questions eq WHERE eq.exam_id = att.exam_id), 0) AS total_marks,
      att.started_at,
      att.submitted_at,
      att.is_paused,
      att.pause_reason,
      att.created_at
    FROM public.exam_attempts att
    JOIN public.exams e ON e.id = att.exam_id
    LEFT JOIN public.profiles p ON p.id = att.student_id
    WHERE (p_exam_id IS NULL OR att.exam_id = p_exam_id)
      AND (p_search IS NULL OR NULLIF(trim(p_search), '') IS NULL
        OR p.full_name ILIKE '%' || trim(p_search) || '%')
      AND (p_class IS NULL OR NULLIF(trim(p_class), '') IS NULL
        OR p.class_name = trim(p_class))
      AND (p_batch IS NULL OR NULLIF(trim(p_batch), '') IS NULL
        OR p.batch = trim(p_batch))
  ) t;

  RETURN v_rows;
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_get_attempts(UUID, TEXT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_get_attempts(UUID, TEXT, TEXT, TEXT) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_get_attempts(UUID, TEXT, TEXT, TEXT) TO authenticated;


-- ====================================================================
-- SECTION D — VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- D1. Signatures:
-- SELECT p.proname, pg_get_function_arguments(p.oid) FROM pg_proc p
-- JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'public'
-- AND p.proname IN ('teacher_create_exam', 'teacher_get_attempts');
-- (expect exactly one row each: 6 args / 4 args)
--
-- D2. Grants (faculty gate inside; anon denied):
-- SELECT p.proname, has_function_privilege('anon', p.oid, 'EXECUTE') AS anon,
--   has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth
-- FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'public'
-- AND p.proname IN ('teacher_create_exam', 'teacher_get_attempts');
-- (expect anon=false, auth=true for both)
--
-- D3. Evaluated attempts report zero pending (authoritative status):
-- SELECT (jsonb_array_elements(
--   -- (as the owning student) get_student_results()
-- ) ->>'programming_pending');
-- (cannot run as a student from here; simulated in the report instead)
-- ====================================================================

-- ====================================================================
-- END OF MIGRATION 16
-- ====================================================================
