-- ====================================================================
-- QUIZEAZZ — TEACHER & STUDENT ARCHITECTURE HARDENING MIGRATION
-- Authoritative RPCs for Examination Scheduling, Candidate Roster,
-- Question Management, and Dashboard Overview Telemetry
-- ====================================================================

-- 1. TEACHER CREATE EXAMINATION RPC
-- Enforces caller authentication and role check against public.profiles
-- Sets created_by strictly from auth.uid()
DROP FUNCTION IF EXISTS public.teacher_create_exam(TEXT, TEXT, INT, TEXT, TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.teacher_create_exam;
CREATE OR REPLACE FUNCTION public.teacher_create_exam(
  p_title TEXT,
  p_description TEXT DEFAULT NULL,
  p_duration_minutes INT DEFAULT 60,
  p_status TEXT DEFAULT 'published',
  p_starts_at TIMESTAMPTZ DEFAULT NULL
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
  -- 1. Check authentication
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required to create examinations.';
  END IF;

  -- 2. Verify authoritative role (qualify table columns to prevent ambiguity)
  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can create examinations.';
  END IF;

  -- 3. Validate title
  IF p_title IS NULL OR trim(p_title) = '' THEN
    RAISE EXCEPTION 'Examination title cannot be empty.';
  END IF;

  -- 4. Validate duration
  IF p_duration_minutes IS NULL OR p_duration_minutes <= 0 OR p_duration_minutes > 1440 THEN
    RAISE EXCEPTION 'Duration must be between 1 and 1440 minutes (24 hours).';
  END IF;

  -- 5. Validate status strictly against live database constraint: draft, published, closed
  IF p_status NOT IN ('draft', 'published', 'closed') THEN
    RAISE EXCEPTION 'Invalid examination status "%". Allowed values: draft, published, closed.', p_status;
  END IF;

  -- 6. Validate start time for scheduled/published examinations
  IF p_status = 'published' AND p_starts_at IS NULL THEN
    RAISE EXCEPTION 'Scheduled start time is required for published examinations.';
  END IF;

  -- 7. Insert new examination record
  INSERT INTO public.exams (
    title,
    description,
    duration_minutes,
    status,
    starts_at,
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
    v_caller_id,
    now(),
    now()
  )
  RETURNING * INTO v_new_exam;

  RETURN to_jsonb(v_new_exam);
END;
$$;

-- 2. TEACHER GET EXAMINATIONS RPC
DROP FUNCTION IF EXISTS public.teacher_get_exams();
CREATE OR REPLACE FUNCTION public.teacher_get_exams()
RETURNS TABLE (
  id UUID,
  title TEXT,
  description TEXT,
  duration_minutes INT,
  status TEXT,
  starts_at TIMESTAMPTZ,
  created_by UUID,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
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
    e.created_by,
    e.created_at,
    e.updated_at
  FROM public.exams e
  ORDER BY e.created_at DESC;
END;
$$;

-- 3. TEACHER GET CANDIDATES (STUDENT REGISTRY) RPC
-- Joins public.profiles and auth.users to provide safe candidate roster without exposing password hashes or tokens
DROP FUNCTION IF EXISTS public.teacher_get_candidates();
CREATE OR REPLACE FUNCTION public.teacher_get_candidates()
RETURNS TABLE (
  id UUID,
  full_name TEXT,
  email TEXT,
  role TEXT,
  created_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, auth, pg_temp
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
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can view the candidate registry.';
  END IF;

  RETURN QUERY
  SELECT 
    p.id,
    p.full_name,
    u.email::TEXT,
    p.role,
    p.created_at
  FROM public.profiles p
  LEFT JOIN auth.users u ON u.id = p.id
  WHERE p.role = 'student'
  ORDER BY p.created_at DESC;
END;
$$;

-- 4. TEACHER OVERVIEW STATS RPC
-- Aggregates real-time question count, exam count, registered candidate count, and total submissions
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

  SELECT count(*) INTO v_total_questions FROM public.questions;
  SELECT count(*) INTO v_total_exams FROM public.exams;
  SELECT count(*) INTO v_active_exams FROM public.exams e WHERE e.status = 'published';
  SELECT count(*) INTO v_total_students FROM public.profiles p WHERE p.role = 'student';
  SELECT count(*) INTO v_total_attempts FROM public.exam_attempts;

  SELECT COALESCE(jsonb_agg(sub), '[]'::jsonb)
  INTO v_recent_exams
  FROM (
    SELECT e.id, e.title, e.duration_minutes, e.status, e.created_at
    FROM public.exams e
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

-- 5. ENSURE QUESTION MARKS COLUMN INTEGRITY
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns 
    WHERE table_schema = 'public' AND table_name = 'questions' AND column_name = 'marks'
  ) THEN
    ALTER TABLE public.questions ADD COLUMN marks INT DEFAULT 10;
  END IF;
END $$;

-- 5B. TEACHER CREATE QUESTION RPC
-- Strictly adheres to the authoritative public.questions schema
CREATE OR REPLACE FUNCTION public.teacher_create_question(
  p_title TEXT,
  p_question_text TEXT,
  p_question_type TEXT,
  p_category TEXT,
  p_subcategory TEXT DEFAULT NULL,
  p_marks INT DEFAULT 10,
  p_explanation TEXT DEFAULT NULL,
  p_language TEXT DEFAULT NULL,
  p_input_requirements TEXT DEFAULT NULL,
  p_output_requirements TEXT DEFAULT NULL,
  p_constraints TEXT DEFAULT NULL,
  p_starter_code TEXT DEFAULT NULL,
  p_reference_solution TEXT DEFAULT NULL,
  p_options JSONB DEFAULT NULL,
  p_correct_answer TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
  v_new_q RECORD;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can author questions.';
  END IF;

  IF p_title IS NULL OR trim(p_title) = '' THEN
    RAISE EXCEPTION 'Question title cannot be empty.';
  END IF;

  IF p_question_text IS NULL OR trim(p_question_text) = '' THEN
    RAISE EXCEPTION 'Question statement cannot be empty.';
  END IF;

  INSERT INTO public.questions (
    title,
    question_text,
    question_type,
    category,
    subcategory,
    marks,
    explanation,
    language,
    input_requirements,
    output_requirements,
    constraints,
    starter_code,
    reference_solution,
    options,
    difficulty,
    created_by,
    created_at
  )
  VALUES (
    trim(p_title),
    trim(p_question_text),
    p_question_type,
    p_category,
    p_subcategory,
    COALESCE(p_marks, 10),
    p_explanation,
    p_language,
    p_input_requirements,
    p_output_requirements,
    p_constraints,
    p_starter_code,
    p_reference_solution,
    p_options,
    'medium',
    v_caller_id,
    now()
  )
  RETURNING * INTO v_new_q;

  RETURN to_jsonb(v_new_q);
END;
$$;

-- 6. TEACHER GET ALL QUESTIONS RPC
-- Matches the actual public.questions schema (no updated_at column)
-- Qualifies all column references and specifies #variable_conflict use_column to avoid ambiguous "id" error
DROP FUNCTION IF EXISTS public.teacher_get_questions();
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

  -- Qualify p.id and p.role to prevent ambiguity with the "id" OUT parameter
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
    q.created_by,
    q.created_at
  FROM public.questions q
  ORDER BY q.created_at DESC;
END;
$$;

-- 7. DEFENSE-IN-DEPTH: EXAMS TABLE RLS POLICIES FOR TEACHER/ADMIN
ALTER TABLE public.exams ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Teacher manage exams" ON public.exams;
CREATE POLICY "Teacher manage exams" ON public.exams
  FOR ALL
  TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.profiles 
      WHERE id = auth.uid() AND role IN ('teacher', 'admin')
    )
  )
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM public.profiles 
      WHERE id = auth.uid() AND role IN ('teacher', 'admin')
    )
  );

-- 8. GRANT PROPER EXECUTE PERMISSIONS TO AUTHENTICATED
REVOKE EXECUTE ON FUNCTION public.teacher_create_exam(TEXT, TEXT, INT, TEXT, TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.teacher_create_exam(TEXT, TEXT, INT, TEXT, TIMESTAMPTZ) TO authenticated;

REVOKE EXECUTE ON FUNCTION public.teacher_get_exams() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.teacher_get_exams() TO authenticated;

REVOKE EXECUTE ON FUNCTION public.teacher_get_candidates() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.teacher_get_candidates() TO authenticated;

REVOKE EXECUTE ON FUNCTION public.teacher_get_overview_stats() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.teacher_get_overview_stats() TO authenticated;

REVOKE EXECUTE ON FUNCTION public.teacher_create_question(TEXT, TEXT, TEXT, TEXT, TEXT, INT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.teacher_create_question(TEXT, TEXT, TEXT, TEXT, TEXT, INT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, TEXT) TO authenticated;

REVOKE EXECUTE ON FUNCTION public.teacher_get_questions() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.teacher_get_questions() TO authenticated;
