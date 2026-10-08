-- ====================================================================
-- QUIZEAZZ — MIGRATION 15: ARCHIVE FLAGS FOR QUESTIONS AND EXAMS
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..14. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   Deleting bank questions or exams with historical dependencies would
--   destroy attempts, reviews and snapshots. Instead, archiving hides rows
--   from every ACTIVE list while leaving all historical data intact.
--   Archived rows remain fully readable through attempt/review/grading
--   paths, which never filter on the flag.
--
-- WHAT THIS FILE DOES
--   1. Adds questions.is_archived / exams.is_archived (NOT NULL DEFAULT
--      false). Existing rows default to visible; nothing is hidden by this
--      migration itself.
--   2. Adds teacher_set_question_archived / teacher_set_exam_archived
--      (faculty-only, authenticated): the only writers of the flags.
--   3. Filters archived rows out of the ACTIVE lists only:
--      teacher_get_questions, teacher_get_exams, get_student_exam_overview,
--      and the overview-stats counts. History paths (results, reviews,
--      attempt detail, grading, snapshots) are untouched.
--
-- SCOPE
--   No table dropped. No column dropped. No row deleted or converted.
--   No RLS change. No ACL widened. Return shapes unchanged, so no
--   CREATE/DROP dance is needed (CREATE OR REPLACE throughout).
--
-- IDEMPOTENT: safe to run more than once.
-- ====================================================================


-- ====================================================================
-- SECTION A — archive flags (visible by default; nothing hidden yet)
-- ====================================================================

ALTER TABLE public.questions
  ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE public.exams
  ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT false;


-- ====================================================================
-- SECTION B — archive writers (faculty only)
-- --------------------------------------------------------------------
-- Archiving hides; it never deletes. Unarchiving restores. Both directions
-- go through the same faculty gate. Students have no path to these RPCs.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.teacher_set_question_archived(
  p_question_id UUID,
  p_archived BOOLEAN
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can archive questions.';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.questions q WHERE q.id = p_question_id) THEN
    RAISE EXCEPTION 'Question % does not exist.', p_question_id;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.questions
  SET is_archived = COALESCE(p_archived, false)
  WHERE id = p_question_id;

  RETURN jsonb_build_object(
    'question_id', p_question_id,
    'is_archived', COALESCE(p_archived, false)
  );
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_set_question_archived(UUID, BOOLEAN) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_set_question_archived(UUID, BOOLEAN) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_set_question_archived(UUID, BOOLEAN) TO authenticated;


CREATE OR REPLACE FUNCTION public.teacher_set_exam_archived(
  p_exam_id UUID,
  p_archived BOOLEAN
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can archive examinations.';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.exams e WHERE e.id = p_exam_id) THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.exams
  SET is_archived = COALESCE(p_archived, false),
      updated_at = now()
  WHERE id = p_exam_id;

  RETURN jsonb_build_object(
    'exam_id', p_exam_id,
    'is_archived', COALESCE(p_archived, false)
  );
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_set_exam_archived(UUID, BOOLEAN) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_set_exam_archived(UUID, BOOLEAN) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_set_exam_archived(UUID, BOOLEAN) TO authenticated;


-- ====================================================================
-- SECTION C — active lists hide archived rows (history paths untouched)
-- --------------------------------------------------------------------
-- teacher_get_questions: bank shows active questions only.
-- teacher_get_exams: examination list shows active exams only.
-- get_student_exam_overview: My Exams shows active assigned exams only.
-- Results, reviews, attempt detail, grading and snapshots never filter on
-- the flag, so all history remains reproducible.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.teacher_get_questions()
RETURNS TABLE (
  id UUID,
  title TEXT,
  question_text TEXT,
  question_type TEXT,
  category TEXT,
  subcategory TEXT,
  difficulty TEXT,
  marks INT,
  explanation TEXT,
  language TEXT,
  input_requirements TEXT,
  output_requirements TEXT,
  constraints TEXT,
  starter_code TEXT,
  reference_solution TEXT,
  options JSONB,
  correct_option_id TEXT,
  created_by UUID,
  created_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
#variable_conflict use_column
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can view question bank details.';
  END IF;

  RETURN QUERY
  SELECT
    q.id,
    q.title,
    q.question_text,
    q.question_type::TEXT,
    q.category,
    q.subcategory,
    q.difficulty::TEXT,
    COALESCE(q.marks, 10)::INT,
    q.explanation,
    q.language,
    q.input_requirements,
    q.output_requirements,
    q.constraints,
    q.starter_code,
    q.reference_solution,
    q.options,
    key.correct_option_id,
    q.created_by,
    q.created_at
  FROM public.questions q
  LEFT JOIN LATERAL (
    SELECT elem->>'id' AS correct_option_id
    FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(q.options) = 'array' THEN q.options ELSE '[]'::jsonb END
    ) AS elem
    WHERE COALESCE((elem->>'is_correct')::boolean, false) = true
    LIMIT 1
  ) key ON TRUE
  WHERE NOT COALESCE(q.is_archived, false)
  ORDER BY q.created_at DESC;
END;
$$;


CREATE OR REPLACE FUNCTION public.teacher_get_exams()
RETURNS TABLE (
  id UUID,
  title TEXT,
  description TEXT,
  duration_minutes INT,
  status TEXT,
  starts_at TIMESTAMPTZ,
  ends_at TIMESTAMPTZ,
  total_marks INT,
  created_by UUID,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ,
  total_questions INT,
  assigned_count INT,
  submitted_count INT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
#variable_conflict use_column
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can list examinations.';
  END IF;

  RETURN QUERY
  SELECT
    e.id,
    e.title,
    e.description,
    e.duration_minutes,
    e.status,
    e.starts_at,
    e.ends_at,
    COALESCE((
      SELECT SUM(eq.marks)::INT FROM public.exam_questions eq WHERE eq.exam_id = e.id
    ), 0),
    e.created_by,
    e.created_at,
    e.updated_at,
    COALESCE((
      SELECT COUNT(*)::INT FROM public.exam_questions eq WHERE eq.exam_id = e.id
    ), 0),
    COALESCE((
      SELECT COUNT(*)::INT FROM public.exam_assignments ea WHERE ea.exam_id = e.id
    ), 0),
    COALESCE((
      SELECT COUNT(*)::INT FROM public.exam_attempts att
      WHERE att.exam_id = e.id
        AND att.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated')
    ), 0)
  FROM public.exams e
  WHERE NOT COALESCE(e.is_archived, false)
  ORDER BY e.created_at DESC;
END;
$$;


CREATE OR REPLACE FUNCTION public.get_student_exam_overview()
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
      'id', e.id,
      'title', e.title,
      'description', e.description,
      'duration_minutes', COALESCE(e.duration_minutes, 60),
      'status', e.status,
      'starts_at', e.starts_at,
      'ends_at', e.ends_at,
      'assignment_id', ea.id,
      'assigned_at', ea.assigned_at,
      'total_questions', COALESCE((
        SELECT count(*) FROM public.exam_questions eq WHERE eq.exam_id = e.id
      ), 0),
      'total_marks', COALESCE((
        SELECT COALESCE(sum(eq.marks), 0) FROM public.exam_questions eq WHERE eq.exam_id = e.id
      ), 0),
      'opens_at', e.starts_at,
      'closes_at', e.ends_at,
      'attempt', (
        SELECT jsonb_build_object(
          'id', att.id,
          'exam_id', att.exam_id,
          'student_id', att.student_id,
          'status', att.status,
          'started_at', att.started_at,
          'submitted_at', att.submitted_at,
          'score', att.score,
          'is_paused', att.is_paused,
          'created_at', att.created_at
        )
        FROM public.exam_attempts att
        WHERE att.exam_id = e.id AND att.student_id = v_user_id
        ORDER BY att.created_at DESC
        LIMIT 1
      )
    )
  ), '[]'::jsonb)
  INTO v_result
  FROM public.exam_assignments ea
  JOIN public.exams e ON e.id = ea.exam_id
  WHERE ea.student_id = v_user_id
    AND e.status IN ('published', 'closed')
    AND NOT COALESCE(e.is_archived, false);

  RETURN v_result;
END;
$$;


CREATE OR REPLACE FUNCTION public.teacher_get_overview_stats()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
  v_total_questions INT;
  v_total_exams INT;
  v_active_exams INT;
  v_total_students INT;
  v_total_attempts INT;
  v_recent_exams JSONB;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied.';
  END IF;

  SELECT count(*) INTO v_total_questions FROM public.questions WHERE NOT COALESCE(is_archived, false);
  SELECT count(*) INTO v_total_exams FROM public.exams WHERE NOT COALESCE(is_archived, false);
  SELECT count(*) INTO v_active_exams FROM public.exams e WHERE e.status = 'published' AND NOT COALESCE(e.is_archived, false);
  SELECT count(*) INTO v_total_students FROM public.profiles p WHERE p.role = 'student';
  SELECT count(*) INTO v_total_attempts FROM public.exam_attempts;

  SELECT COALESCE(jsonb_agg(sub), '[]'::jsonb)
  INTO v_recent_exams
  FROM (
    SELECT e.id, e.title, e.duration_minutes, e.status, e.created_at
    FROM public.exams e
    WHERE NOT COALESCE(e.is_archived, false)
    ORDER BY e.created_at DESC
    LIMIT 5
  ) sub;

  RETURN jsonb_build_object(
    'total_questions', v_total_questions,
    'total_exams', v_total_exams,
    'active_exams', v_active_exams,
    'total_students', v_total_students,
    'total_attempts', v_total_attempts,
    'recent_exams', v_recent_exams
  );
END;
$$;


-- ====================================================================
-- SECTION D — VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- D1. Flags exist, default visible:
-- SELECT column_name FROM information_schema.columns
-- WHERE table_schema='public' AND table_name IN ('questions','exams')
-- AND column_name='is_archived';
--
-- D2. New RPCs exist, faculty-gated, authenticated-executable:
-- SELECT p.proname,
--   has_function_privilege('anon',p.oid,'EXECUTE') AS anon,
--   has_function_privilege('authenticated',p.oid,'EXECUTE') AS auth
-- FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
-- WHERE n.nspname='public'
-- AND p.proname IN ('teacher_set_question_archived','teacher_set_exam_archived');
-- (expect anon=false, auth=true for both)
--
-- D3. Nothing hidden yet (migration only adds the mechanism):
-- SELECT count(*) FROM public.questions WHERE is_archived;
-- SELECT count(*) FROM public.exams WHERE is_archived;
-- (expect 0 and 0 until faculty archives explicitly)
-- ====================================================================

-- ====================================================================
-- END OF MIGRATION 15
-- ====================================================================
