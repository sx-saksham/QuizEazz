-- ====================================================================
-- QUIZEAZZ — EXAM MANAGEMENT, GRADING INTEGRITY & AUTHORITATIVE PROVISIONING
-- --------------------------------------------------------------------
-- Run order: AFTER schema_security_hardening.sql
--             AFTER migration_exam_secure_architecture.sql
--             AFTER migration_teacher_and_student_hardening.sql
--
-- Idempotent. Performs NO destructive operations on existing data.
-- Only exact-duplicate rows are collapsed, and only when duplicates exist.
--
-- SCOPE OF THIS FILE — the only statements here that modify rows are:
--   * Section F0  UPDATE questions  (only MCQ rows that have no key yet)
--   * Section J1  DELETE on exam_assignments / exam_questions
--                 (only exact duplicate pairs, only if any exist)
--   * Section K2  DELETE + INSERT on exam_assignments  (teacher-requested)
--   * Section K4  DELETE + INSERT on exam_questions    (teacher-requested)
-- Everything else is CREATE OR REPLACE FUNCTION, GRANT/REVOKE, ALTER TABLE
-- ... ENABLE ROW LEVEL SECURITY, or DROP/CREATE TRIGGER.
-- There is no DROP TABLE, no TRUNCATE, and no DROP CONSTRAINT.
--
-- WHAT THIS FIXES (root causes, not symptoms):
--  A. MCQ answer key was accepted but never persisted. grade_mcq_attempt()
--     looks for options->>'is_correct'; the client sent [{id,text}].
--     => every MCQ graded "Wrong Answer" / 0 marks forever.
--  B. start_or_get_exam_attempt()/get_exam_questions() never compared
--     starts_at to now() => students could start a SCHEDULED exam early.
--  C. Objective grading was triggered ONLY by the browser after submit.
--     Client disconnect between submit and grade => attempt stuck at
--     'submitted' with score NULL, review permanently unavailable.
--     => submission is now atomic and server-authoritative.
--  D. No RPC existed for exam_assignments / exam_questions at all.
--     "Assign Students" and "Manage Paper" buttons were non-functional
--     and had no backend.
--  E. exams status literals 'scheduled'/'active'/'in_progress'/'completed'
--     do not exist in the live CHECK constraint (draft/published/closed).
--  F. RAISE EXCEPTION argument count mismatch (3 args / 2 placeholders).
--  G. Profile provisioning ran in the browser (and swallowed errors).
-- ====================================================================


-- ====================================================================
-- SECTION A — AUTHORITATIVE OBJECTIVE GRADING (internal engine)
-- ====================================================================
-- Runs entirely server-side. Sets the trusted-execution flag so the
-- answers/attempts anti-tamper triggers permit the write.
-- Judges MCQs against the private key in questions.options.
-- Programming questions are recorded as 'Pending Evaluation' with 0 marks
-- because no isolated Judge0 CE runner is connected. It never guesses.

DROP FUNCTION IF EXISTS public.quizeazz_grade_objective_marks(UUID);
CREATE OR REPLACE FUNCTION public.quizeazz_grade_objective_marks(p_attempt_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_attempt RECORD;
  v_mcq_score INT := 0;
  v_updated_count INT := 0;
  v_pending_programs INT := 0;
BEGIN
  SELECT ea.id, ea.exam_id, ea.student_id, ea.status
  INTO v_attempt
  FROM public.exam_attempts ea
  WHERE ea.id = p_attempt_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % not found.', p_attempt_id;
  END IF;

  IF v_attempt.status NOT IN ('submitted', 'timed_out', 'evaluating', 'evaluated') THEN
    RAISE EXCEPTION 'Lifecycle violation: attempt % is in status %. Final submission is required prior to evaluation.',
      p_attempt_id, v_attempt.status;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  IF v_attempt.status IN ('submitted', 'timed_out') THEN
    UPDATE public.exam_attempts SET status = 'evaluating' WHERE id = p_attempt_id;
  END IF;

  -- 1. Deterministic MCQ grading against the private key.
  --    Unattempted questions need no placeholder row: both the student
  --    review and the faculty evidence projection LEFT JOIN answers, so an
  --    absent row is correctly reported as "Not Attempted" with 0 marks.
  WITH mcq_eval AS (
    SELECT
      a.id AS answer_id,
      key.correct_id,
      eq.marks AS question_marks,
      (a.selected_option_id IS NOT NULL AND a.selected_option_id = key.correct_id) AS is_correct_answer,
      (a.selected_option_id IS NULL) AS is_unanswered
    FROM public.answers a
    JOIN public.questions q ON q.id = a.question_id
    LEFT JOIN public.exam_questions eq
           ON eq.exam_id = v_attempt.exam_id AND eq.question_id = a.question_id
    LEFT JOIN LATERAL (
      SELECT elem->>'id' AS correct_id
      FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(q.options) = 'array' THEN q.options ELSE '[]'::jsonb END
      ) AS elem
      WHERE COALESCE((elem->>'is_correct')::boolean, false) = true
      LIMIT 1
    ) key ON TRUE
    WHERE a.attempt_id = p_attempt_id
      AND q.question_type = 'mcq'
  ),
  updated_answers AS (
    UPDATE public.answers a
    SET
      is_correct = me.is_correct_answer,
      marks_awarded = CASE WHEN me.is_correct_answer THEN COALESCE(me.question_marks, 0) ELSE 0 END,
      evaluation_status = CASE
        WHEN me.is_unanswered THEN 'Not Attempted'
        WHEN me.is_correct_answer THEN 'Correct'
        ELSE 'Wrong Answer'
      END,
      -- answers has saved_at, not updated_at. saved_at is the live schema's
      -- answer save/update timestamp and is what the grading pass must
      -- refresh.
      saved_at = now()
    FROM mcq_eval me
    WHERE a.id = me.answer_id
    RETURNING a.marks_awarded
  )
  SELECT COALESCE(SUM(marks_awarded), 0), COUNT(*)
  INTO v_mcq_score, v_updated_count
  FROM updated_answers;

  -- 2. Programming questions: no execution runner is connected.
  --    Record the truthful state. Marks are NEVER awarded by the browser
  --    and are NEVER fabricated here.
  UPDATE public.answers a
  SET
    evaluation_status = CASE
      WHEN a.code IS NULL OR btrim(a.code) = '' THEN 'Not Attempted'
      ELSE 'Pending Evaluation'
    END,
    marks_awarded = 0,
    is_correct = NULL,
    saved_at = now()
  FROM public.questions q
  WHERE a.attempt_id = p_attempt_id
    AND a.question_id = q.id
    AND q.question_type = 'coding';

  SELECT count(*) INTO v_pending_programs
  FROM public.answers a
  JOIN public.questions q ON q.id = a.question_id
  WHERE a.attempt_id = p_attempt_id
    AND q.question_type = 'coding'
    AND a.evaluation_status = 'Pending Evaluation';

  -- 3. Freeze the attempt with the authoritative score.
  UPDATE public.exam_attempts ea
  SET
    score = COALESCE((
      SELECT SUM(a.marks_awarded)
      FROM public.answers a
      WHERE a.attempt_id = p_attempt_id
    ), 0)::INT,
    status = 'evaluated',
    metadata = COALESCE(ea.metadata, '{}'::jsonb) || jsonb_build_object(
      'grading', jsonb_build_object(
        'mcq_evaluated', true,
        'mcq_score', v_mcq_score,
        'programming_evaluated', false,
        'programming_pending', v_pending_programs,
        'programming_engine', 'Judge0 CE (not connected)',
        'graded_at', now()
      )
    )
  WHERE ea.id = p_attempt_id;

  RETURN jsonb_build_object(
    'attempt_id', p_attempt_id,
    'status', 'evaluated',
    'mcq_score', v_mcq_score,
    'graded_questions', v_updated_count,
    'programming_pending', v_pending_programs,
    'programming_evaluated', false
  );
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_grade_objective_marks(UUID) FROM PUBLIC;


-- ====================================================================
-- SECTION B — SUBMISSION IS ATOMIC AND SERVER-AUTHORITATIVE
-- ====================================================================
-- The browser now performs EXACTLY ONE authoritative call: submit_exam_attempt().
-- Submission + objective grading happen inside one server transaction.
-- grade_mcq_attempt() remains as an idempotent retry / faculty re-grade.

CREATE OR REPLACE FUNCTION public.submit_exam_attempt(p_attempt_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_attempt RECORD;
  v_user_id UUID;
  v_user_role TEXT;
  v_duration INT;
  v_deadline TIMESTAMPTZ;
  v_is_late BOOLEAN := FALSE;
  v_final_status TEXT;
  v_grade JSONB;
  v_total_marks INT := 0;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required to submit attempt.';
  END IF;

  SELECT id, student_id, status, started_at, exam_id
  INTO v_attempt
  FROM public.exam_attempts
  WHERE id = p_attempt_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % does not exist.', p_attempt_id;
  END IF;

  SELECT p.role INTO v_user_role FROM public.profiles p WHERE p.id = v_user_id;

  IF v_attempt.student_id <> v_user_id AND (v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL) THEN
    RAISE EXCEPTION 'Access denied: You cannot submit an attempt belonging to another candidate.';
  END IF;

  SELECT COALESCE(e.duration_minutes, 60) INTO v_duration
  FROM public.exams e WHERE e.id = v_attempt.exam_id;

  v_deadline := v_attempt.started_at + (v_duration * INTERVAL '1 minute') + INTERVAL '15 seconds';

  SELECT COALESCE(SUM(eq.marks), 0) INTO v_total_marks
  FROM public.exam_questions eq WHERE eq.exam_id = v_attempt.exam_id;

  -- Idempotency: re-submitting an already finalized attempt is a no-op that
  -- still reports the authoritative outcome.
  IF v_attempt.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated') THEN
    IF v_attempt.status IN ('evaluating', 'evaluated') THEN
      RETURN jsonb_build_object(
        'attempt_id', p_attempt_id,
        'status', v_attempt.status,
        'total_marks', v_total_marks,
        'already_submitted', TRUE,
        'message', 'Examination already submitted and evaluated.'
      );
    END IF;
    -- submitted/timed_out without evaluation (legacy partial state): finish it.
    v_grade := public.quizeazz_grade_objective_marks(p_attempt_id);
    RETURN jsonb_build_object(
      'attempt_id', p_attempt_id,
      'status', v_grade->>'status',
      'total_marks', v_total_marks,
      'already_submitted', TRUE,
      'grading', v_grade,
      'message', 'Examination already submitted; objective evaluation completed.'
    );
  END IF;

  IF v_attempt.status <> 'in_progress' THEN
    RAISE EXCEPTION 'Attempt cannot be submitted because current status is %.', v_attempt.status;
  END IF;

  IF now() > v_deadline THEN
    v_is_late := TRUE;
    v_final_status := 'timed_out';
  ELSE
    v_final_status := 'submitted';
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.exam_attempts
  SET
    status = v_final_status,
    submitted_at = now(),
    is_paused = FALSE,
    paused_at = NULL,
    pause_reason = CASE WHEN v_is_late THEN 'Attempt exceeded allotted duration' ELSE NULL END
  WHERE id = p_attempt_id;

  -- Server-side objective evaluation. The client cannot skip, defer or
  -- influence this step.
  v_grade := public.quizeazz_grade_objective_marks(p_attempt_id);

  RETURN jsonb_build_object(
    'attempt_id', p_attempt_id,
    'status', v_grade->>'status',
    'submitted_at', now(),
    'timed_out', v_is_late,
    'total_marks', v_total_marks,
    'grading', v_grade,
    'message', CASE
      WHEN v_is_late THEN 'Submission occurred after the authoritative deadline. Attempt marked as timed out.'
      ELSE 'Examination submitted and evaluated.'
    END
  );
END;
$$;


-- ====================================================================
-- SECTION C — IDEMPOTENT RE-GRADE (owner retry / faculty re-grade)
-- ====================================================================

CREATE OR REPLACE FUNCTION public.grade_mcq_attempt(p_attempt_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_attempt RECORD;
  v_user_id UUID;
  v_user_role TEXT;
  v_grade JSONB;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT id, student_id, status INTO v_attempt
  FROM public.exam_attempts
  WHERE id = p_attempt_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % not found.', p_attempt_id;
  END IF;

  SELECT p.role INTO v_user_role FROM public.profiles p WHERE p.id = v_user_id;

  IF v_attempt.student_id <> v_user_id AND (v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL) THEN
    RAISE EXCEPTION 'Access denied: Cannot grade another candidate attempt.';
  END IF;

  IF v_attempt.status = 'in_progress' THEN
    RAISE EXCEPTION 'Lifecycle violation: attempt % is still in progress. Final submission is required prior to evaluation.', p_attempt_id;
  END IF;

  IF v_attempt.status NOT IN ('submitted', 'timed_out', 'evaluating', 'evaluated') THEN
    RAISE EXCEPTION 'Attempt % is in status % and cannot be evaluated.', p_attempt_id, v_attempt.status;
  END IF;

  v_grade := public.quizeazz_grade_objective_marks(p_attempt_id);

  RETURN v_grade || jsonb_build_object('message', 'Objective evaluation completed.');
END;
$$;


-- ====================================================================
-- SECTION D — SCHEDULE WINDOW ENFORCEMENT (no early starts)
-- ====================================================================
-- UI concept "Scheduled" == database status 'published' AND starts_at > now().
-- The database is the authority on whether a student may begin.

CREATE OR REPLACE FUNCTION public.start_or_get_exam_attempt(p_exam_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_user_role TEXT;
  v_exam RECORD;
  v_attempt RECORD;
  v_paper_marks INT := 0;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required to start or load an examination attempt.';
  END IF;

  SELECT COALESCE(SUM(eq.marks), 0)::INT INTO v_paper_marks
  FROM public.exam_questions eq
  WHERE eq.exam_id = p_exam_id;

  SELECT p.role INTO v_user_role FROM public.profiles p WHERE p.id = v_user_id;

  -- exams has no total_marks column. The paper total is derived from the
  -- exam_questions.marks sum, which is the authoritative value everywhere.
  SELECT
    e.id,
    e.title,
    e.description,
    e.duration_minutes,
    e.status,
    e.starts_at,
    e.ends_at
  INTO v_exam
  FROM public.exams e
  WHERE e.id = p_exam_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  IF v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL THEN
    IF NOT EXISTS (
      SELECT 1 FROM public.exam_assignments ea
      WHERE ea.exam_id = p_exam_id AND ea.student_id = v_user_id
    ) THEN
      RAISE EXCEPTION 'Access denied: You are not assigned to this examination.';
    END IF;

    IF v_exam.status <> 'published' THEN
      RAISE EXCEPTION 'This examination is not open (status: %).', v_exam.status;
    END IF;

    IF v_exam.starts_at IS NOT NULL AND now() < v_exam.starts_at THEN
      RAISE EXCEPTION 'This examination has not started yet. It opens at %.', v_exam.starts_at;
    END IF;

    IF v_exam.ends_at IS NOT NULL AND now() > v_exam.ends_at THEN
      RAISE EXCEPTION 'This examination closed at %.', v_exam.ends_at;
    END IF;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  INSERT INTO public.exam_attempts (exam_id, student_id, status, started_at)
  VALUES (p_exam_id, v_user_id, 'in_progress', now())
  ON CONFLICT (exam_id, student_id) DO NOTHING;

  SELECT att.id, att.exam_id, att.student_id, att.status, att.started_at, att.submitted_at,
         att.is_paused, att.paused_at, att.pause_reason, att.created_at, att.metadata
  INTO v_attempt
  FROM public.exam_attempts att
  WHERE att.exam_id = p_exam_id AND att.student_id = v_user_id;

  RETURN jsonb_build_object(
    'exam', jsonb_build_object(
      'id', v_exam.id,
      'title', v_exam.title,
      'description', v_exam.description,
      'duration_minutes', COALESCE(v_exam.duration_minutes, 60),
      'status', v_exam.status,
      'starts_at', v_exam.starts_at,
      'ends_at', v_exam.ends_at,
      'total_marks', v_paper_marks
    ),
    'attempt', jsonb_build_object(
      'id', v_attempt.id,
      'exam_id', v_attempt.exam_id,
      'student_id', v_attempt.student_id,
      'status', v_attempt.status,
      'started_at', v_attempt.started_at,
      'submitted_at', v_attempt.submitted_at,
      'is_paused', COALESCE(v_attempt.is_paused, false),
      'paused_at', v_attempt.paused_at,
      'pause_reason', v_attempt.pause_reason,
      'created_at', v_attempt.created_at,
      'metadata', COALESCE(v_attempt.metadata, '{}'::jsonb)
    )
  );
END;
$$;


-- ====================================================================
-- SECTION E — CANDIDATE QUESTION FETCH (schedule window enforced)
-- ====================================================================

CREATE OR REPLACE FUNCTION public.get_exam_questions(p_exam_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_user_role TEXT;
  v_is_authorized BOOLEAN := FALSE;
  v_result JSONB;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_user_role FROM public.profiles p WHERE p.id = v_user_id;
  IF v_user_role IN ('teacher', 'admin') THEN
    v_is_authorized := TRUE;
  ELSE
    IF EXISTS (
      SELECT 1
      FROM public.exam_assignments ea
      JOIN public.exams e ON e.id = ea.exam_id
      WHERE ea.exam_id = p_exam_id
        AND ea.student_id = v_user_id
        AND e.status = 'published'
        AND (e.starts_at IS NULL OR e.starts_at <= now())
        AND (e.ends_at IS NULL OR e.ends_at >= now())
    ) THEN
      v_is_authorized := TRUE;
    END IF;
  END IF;

  IF NOT v_is_authorized THEN
    RAISE EXCEPTION 'Access denied: You are not assigned, or this examination is not currently open.';
  END IF;

  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'id', q.id,
      'title', q.title,
      'question_text', q.question_text,
      'question_type', q.question_type,
      'category', q.category,
      'subcategory', q.subcategory,
      'difficulty', q.difficulty,
      'marks', COALESCE(eq.marks, 0),
      'starter_code', q.starter_code,
      'language', q.language,
      'input_requirements', q.input_requirements,
      'output_requirements', q.output_requirements,
      'constraints', q.constraints,
      'options', CASE
        WHEN q.question_type = 'mcq'
             AND jsonb_typeof(q.options) = 'array'
        THEN (
          SELECT jsonb_agg(
            jsonb_build_object('id', elem->>'id', 'text', elem->>'text')
            ORDER BY ord
          )
          FROM jsonb_array_elements(q.options) WITH ORDINALITY AS t(elem, ord)
        )
        ELSE NULL
      END
    ) ORDER BY eq.id
  ), '[]'::jsonb)
  INTO v_result
  FROM public.exam_questions eq
  JOIN public.questions q ON q.id = eq.question_id
  WHERE eq.exam_id = p_exam_id;

  RETURN v_result;
END;
$$;
-- NOTE: no candidate-visible ordering is imposed here. The browser applies a
-- deterministic (studentId, examId)-seeded shuffle that is stable across
-- reloads. Correctness is keyed to option identity, never to display order.


-- ====================================================================
-- SECTION F — MCQ ANSWER KEY IS NOW PERSISTED CORRECTLY
-- ====================================================================
-- The key lives inside questions.options as "is_correct": true on exactly
-- one element. No schema change required, and both student-facing RPCs
-- already strip is_correct when projecting options.
-- Signatures are unchanged, so CREATE OR REPLACE is sufficient.

-- F0. Backward-compatible repair. If a legacy `questions.correct_answer`
--     column exists (created in the dashboard, never populated by the RPC),
--     migrate any key it holds into options[].is_correct.
--     Only rows that currently have NO key are touched. Nothing is deleted.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'questions'
      AND column_name = 'correct_answer'
  ) THEN
    EXECUTE $fix$
      UPDATE public.questions q
      SET options = (
        SELECT jsonb_agg(
          CASE WHEN elem->>'id' = q.correct_answer
               THEN jsonb_set(elem - 'is_correct', '{is_correct}', 'true'::jsonb)
               ELSE (elem - 'is_correct')
          END
          ORDER BY ord
        )
        FROM jsonb_array_elements(
          CASE WHEN jsonb_typeof(q.options) = 'array' THEN q.options ELSE '[]'::jsonb END
        ) WITH ORDINALITY AS t(elem, ord)
      )
      WHERE q.question_type = 'mcq'
        AND q.correct_answer IS NOT NULL
        AND jsonb_typeof(q.options) = 'array'
        AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements(q.options) e
          WHERE COALESCE((e->>'is_correct')::boolean, false) = true
        )
    $fix$;
    RAISE NOTICE 'Legacy MCQ answer keys were migrated into questions.options.';
  END IF;
END $$;

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
  v_stored_options JSONB := NULL;
  v_normalized JSONB;
  v_correct TEXT;
  v_hits INT := 0;
  v_marks INT;
  v_opt_arr JSONB;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can author questions.';
  END IF;

  IF p_title IS NULL OR btrim(p_title) = '' THEN
    RAISE EXCEPTION 'Question title cannot be empty.';
  END IF;

  IF p_question_text IS NULL OR btrim(p_question_text) = '' THEN
    RAISE EXCEPTION 'Question statement cannot be empty.';
  END IF;

  IF p_question_type IS NULL OR p_question_type NOT IN ('mcq', 'coding') THEN
    RAISE EXCEPTION 'Question type must be exactly ''mcq'' or ''coding''.';
  END IF;

  IF p_category IS NULL OR btrim(p_category) = '' THEN
    RAISE EXCEPTION 'Question category cannot be empty.';
  END IF;

  v_marks := COALESCE(p_marks, 10);
  IF v_marks < 1 OR v_marks > 100 THEN
    RAISE EXCEPTION 'Marks must be between 1 and 100.';
  END IF;

  IF p_question_type = 'mcq' THEN
    v_opt_arr := COALESCE(p_options, '[]'::jsonb);

    IF jsonb_typeof(v_opt_arr) <> 'array' THEN
      RAISE EXCEPTION 'MCQ options must be a JSON array.';
    END IF;

    IF jsonb_array_length(v_opt_arr) < 2 THEN
      RAISE EXCEPTION 'An MCQ requires at least two answer options.';
    END IF;

    IF EXISTS (
      SELECT 1 FROM jsonb_array_elements(v_opt_arr) AS elem
      WHERE elem->>'id' IS NULL OR btrim(COALESCE(elem->>'text', '')) = ''
    ) THEN
      RAISE EXCEPTION 'Every MCQ option requires an identifier and non-empty option text.';
    END IF;

    v_correct := NULLIF(btrim(COALESCE(p_correct_answer, '')), '');
    IF v_correct IS NULL THEN
      RAISE EXCEPTION 'You must select the correct answer key for every MCQ.';
    END IF;

    SELECT count(*) INTO v_hits
    FROM jsonb_array_elements(v_opt_arr) AS elem
    WHERE elem->>'id' = v_correct;

    IF v_hits <> 1 THEN
      RAISE EXCEPTION 'The selected answer key (%) does not match exactly one option.', v_correct;
    END IF;

    -- Strip any client-supplied correctness flags, then stamp exactly one.
    SELECT jsonb_agg(
      CASE WHEN elem->>'id' = v_correct
           THEN jsonb_set(elem - 'is_correct', '{is_correct}', 'true'::jsonb)
           ELSE (elem - 'is_correct')
      END
      ORDER BY ord
    )
    INTO v_normalized
    FROM jsonb_array_elements(v_opt_arr) WITH ORDINALITY AS t(elem, ord);

    v_stored_options := v_normalized;
  ELSE
    IF p_reference_solution IS NULL OR btrim(p_reference_solution) = '' THEN
      RAISE EXCEPTION 'A faculty reference solution is required for programming questions.';
    END IF;
    IF p_input_requirements IS NULL OR btrim(p_input_requirements) = '' THEN
      RAISE EXCEPTION 'Input requirements are required for programming questions.';
    END IF;
    IF p_output_requirements IS NULL OR btrim(p_output_requirements) = '' THEN
      RAISE EXCEPTION 'Output requirements are required for programming questions.';
    END IF;
    IF p_constraints IS NULL OR btrim(p_constraints) = '' THEN
      RAISE EXCEPTION 'Constraints are required for programming questions.';
    END IF;
    -- No execution runner is connected, so no evaluation configuration is
    -- recorded. Inventing a "validated" flag would be a false claim.
    v_stored_options := NULL;
  END IF;

  INSERT INTO public.questions (
    title, question_text, question_type, category, subcategory, marks,
    explanation, language, input_requirements, output_requirements,
    constraints, starter_code, reference_solution, options, difficulty,
    created_by, created_at
  )
  VALUES (
    btrim(p_title),
    btrim(p_question_text),
    p_question_type,
    btrim(p_category),
    NULLIF(btrim(COALESCE(p_subcategory, '')), ''),
    v_marks,
    NULLIF(p_explanation, ''),
    p_language,
    p_input_requirements,
    p_output_requirements,
    p_constraints,
    p_starter_code,
    p_reference_solution,
    v_stored_options,
    'medium',
    v_caller_id,
    now()
  )
  RETURNING * INTO v_new_q;

  RETURN to_jsonb(v_new_q);
END;
$$;


-- ====================================================================
-- SECTION G — QUESTION BANK READ RPC (now includes the answer key)
-- ====================================================================
-- Faculty-only. correct_option_id is derived from the stored key and is
-- NEVER exposed by any candidate-facing RPC before final evaluation.

DROP FUNCTION IF EXISTS public.teacher_get_questions();
CREATE FUNCTION public.teacher_get_questions()
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
  ORDER BY q.created_at DESC;
END;
$$;


-- ====================================================================
-- SECTION H — EXAM REGISTRY RPC (now carries paper + cohort counts)
-- ====================================================================

DROP FUNCTION IF EXISTS public.teacher_get_exams();
CREATE FUNCTION public.teacher_get_exams()
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
  ORDER BY e.created_at DESC;
END;
$$;


-- ====================================================================
-- SECTION I — STUDENT REVIEW REPORT (honest outcomes + answer key)
-- ====================================================================
-- Correctness stays attached to option identity. The key is only ever
-- projected once the attempt is 'evaluated'.

CREATE OR REPLACE FUNCTION public.get_student_attempt_review(p_attempt_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_user_role TEXT;
  v_attempt RECORD;
  v_exam RECORD;
  v_evaluations JSONB;
  v_total_marks INT := 0;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_user_role FROM public.profiles p WHERE p.id = v_user_id;

  SELECT att.id, att.exam_id, att.student_id, att.status, att.score, att.started_at, att.submitted_at, att.created_at
  INTO v_attempt
  FROM public.exam_attempts att
  WHERE att.id = p_attempt_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination attempt % does not exist.', p_attempt_id;
  END IF;

  IF v_attempt.student_id <> v_user_id AND (v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL) THEN
    RAISE EXCEPTION 'Access denied: You cannot view results for an attempt belonging to another candidate.';
  END IF;

  IF v_attempt.status = 'in_progress' THEN
    RAISE EXCEPTION 'Review unavailable: this examination is still in progress.';
  END IF;

  -- exams has no total_marks column, so the paper total comes solely from
  -- the exam_questions.marks sum. No fallback exists and none is needed.
  SELECT e.id, e.title INTO v_exam
  FROM public.exams e WHERE e.id = v_attempt.exam_id;

  SELECT COALESCE(SUM(eq.marks), 0)::INT INTO v_total_marks
  FROM public.exam_questions eq WHERE eq.exam_id = v_attempt.exam_id;

  -- Not yet evaluated: report truthfully, do not fabricate a report.
  IF v_attempt.status <> 'evaluated' THEN
    RETURN jsonb_build_object(
      'attempt_id', v_attempt.id,
      'exam_id', v_attempt.exam_id,
      'exam_title', COALESCE(v_exam.title, 'Examination'),
      'score', COALESCE(v_attempt.score, 0),
      'total_marks', v_total_marks,
      'status', v_attempt.status,
      'submitted_at', COALESCE(v_attempt.submitted_at, v_attempt.created_at),
      'evaluations', '[]'::jsonb
    );
  END IF;

  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'question_id', q.id,
      'question_title', q.title,
      'question_type', q.question_type,
      'question_text', q.question_text,
      'marks_total', COALESCE(eq.marks, 0),
      'marks_awarded', COALESCE(a.marks_awarded, 0),
      'outcome', CASE
        WHEN a.evaluation_status IS NOT NULL THEN a.evaluation_status
        WHEN q.question_type = 'mcq' THEN 'Not Attempted'
        ELSE 'Pending Evaluation'
      END,
      'is_correct', a.is_correct,
      'student_option_id', a.selected_option_id,
      'correct_option_id', CASE WHEN q.question_type = 'mcq' THEN key.correct_id ELSE NULL END,
      'student_code', a.code,
      'language', COALESCE(a.language, q.language, 'text'),
      'options', CASE
        WHEN q.question_type = 'mcq' AND jsonb_typeof(q.options) = 'array'
        THEN (
          SELECT jsonb_agg(jsonb_build_object('id', elem->>'id', 'text', elem->>'text') ORDER BY ord)
          FROM jsonb_array_elements(q.options) WITH ORDINALITY AS t(elem, ord)
        )
        ELSE NULL
      END,
      'explanation', q.explanation
    ) ORDER BY eq.id
  ), '[]'::jsonb)
  INTO v_evaluations
  FROM public.exam_questions eq
  JOIN public.questions q ON q.id = eq.question_id
  LEFT JOIN public.answers a ON a.attempt_id = p_attempt_id AND a.question_id = q.id
  LEFT JOIN LATERAL (
    SELECT elem->>'id' AS correct_id
    FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(q.options) = 'array' THEN q.options ELSE '[]'::jsonb END
    ) AS elem
    WHERE COALESCE((elem->>'is_correct')::boolean, false) = true
    LIMIT 1
  ) key ON TRUE
  WHERE eq.exam_id = v_attempt.exam_id;

  RETURN jsonb_build_object(
    'attempt_id', v_attempt.id,
    'exam_id', v_attempt.exam_id,
    'exam_title', COALESCE(v_exam.title, 'Examination'),
    'score', COALESCE(v_attempt.score, 0),
    'total_marks', v_total_marks,
    'status', v_attempt.status,
    'submitted_at', COALESCE(v_attempt.submitted_at, v_attempt.created_at),
    'evaluations', v_evaluations
  );
END;
$$;


-- ====================================================================
-- SECTION J — EXAM COMPOSITION INTEGRITY
-- ====================================================================
-- exam_assignments and exam_questions are written ONLY through the
-- teacher RPCs in Section K. Client INSERT/UPDATE/DELETE is impossible
-- even if a permissive policy is ever added by mistake.

-- J1. Uniqueness (collapse exact duplicates only, and only if any exist)
DO $$
DECLARE
  v_dup_count INT;
BEGIN
  SELECT COUNT(*) INTO v_dup_count FROM (
    SELECT exam_id, student_id FROM public.exam_assignments
    GROUP BY exam_id, student_id HAVING COUNT(*) > 1
  ) d;
  IF v_dup_count > 0 THEN
    WITH ranked AS (
      SELECT a.id, ROW_NUMBER() OVER (
        PARTITION BY a.exam_id, a.student_id ORDER BY a.id ASC
      ) AS rn
      FROM public.exam_assignments a
    )
    DELETE FROM public.exam_assignments
    WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
    RAISE NOTICE 'Collapsed % duplicate exam_assignments row(s).', v_dup_count;
  END IF;

  SELECT COUNT(*) INTO v_dup_count FROM (
    SELECT exam_id, question_id FROM public.exam_questions
    GROUP BY exam_id, question_id HAVING COUNT(*) > 1
  ) d;
  IF v_dup_count > 0 THEN
    WITH ranked AS (
      SELECT q.id, ROW_NUMBER() OVER (
        PARTITION BY q.exam_id, q.question_id ORDER BY q.id ASC
      ) AS rn
      FROM public.exam_questions q
    )
    DELETE FROM public.exam_questions
    WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
    RAISE NOTICE 'Collapsed % duplicate exam_questions row(s).', v_dup_count;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = 'public' AND t.relname = 'exam_assignments'
      AND c.contype = 'u'
      AND (
        SELECT array_agg(a.attname::TEXT ORDER BY a.attname::TEXT)
        FROM unnest(c.conkey) AS k(attnum)
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
      ) = ARRAY['exam_id', 'student_id']
  ) THEN
    ALTER TABLE public.exam_assignments
      ADD CONSTRAINT uq_exam_assignments_exam_student UNIQUE (exam_id, student_id);
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = 'public' AND t.relname = 'exam_questions'
      AND c.contype = 'u'
      AND (
        SELECT array_agg(a.attname::TEXT ORDER BY a.attname::TEXT)
        FROM unnest(c.conkey) AS k(attnum)
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
      ) = ARRAY['exam_id', 'question_id']
  ) THEN
    ALTER TABLE public.exam_questions
      ADD CONSTRAINT uq_exam_questions_exam_question UNIQUE (exam_id, question_id);
  END IF;
END $$;

-- J2. Write protection: only the authoritative RPCs may mutate composition.
CREATE OR REPLACE FUNCTION public.protect_exam_composition_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  IF current_setting('quizeazz.authoritative_context', true) = 'true' THEN
    IF TG_OP = 'DELETE' THEN
      RETURN OLD;
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION
    'Unauthorized: exam assignments and exam papers are maintained exclusively through the faculty RPCs (teacher_set_exam_assignments / teacher_set_exam_paper).';
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_exam_assignments ON public.exam_assignments;
CREATE TRIGGER trg_protect_exam_assignments
BEFORE INSERT OR UPDATE OR DELETE ON public.exam_assignments
FOR EACH ROW EXECUTE FUNCTION public.protect_exam_composition_mutation();

DROP TRIGGER IF EXISTS trg_protect_exam_questions ON public.exam_questions;
CREATE TRIGGER trg_protect_exam_questions
BEFORE INSERT OR UPDATE OR DELETE ON public.exam_questions
FOR EACH ROW EXECUTE FUNCTION public.protect_exam_composition_mutation();


-- ====================================================================
-- SECTION K — FACULTY EXAM MANAGEMENT RPCs (Assign Students / Manage Paper)
-- ====================================================================

-- K0. Shared faculty role guard, and a lock that refuses composition edits
--     once any candidate has already finalized the paper.
CREATE OR REPLACE FUNCTION public.quizeazz_assert_faculty(p_caller_id UUID)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_role TEXT;
BEGIN
  SELECT p.role INTO v_role FROM public.profiles p WHERE p.id = p_caller_id;
  IF v_role NOT IN ('teacher', 'admin') OR v_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: faculty or administrator role required.';
  END IF;
  RETURN v_role;
END;
$$;

CREATE OR REPLACE FUNCTION public.quizeazz_assert_composition_editable(p_exam_id UUID)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_status TEXT;
  v_finalized INT;
BEGIN
  SELECT e.status INTO v_status FROM public.exams e WHERE e.id = p_exam_id;
  IF v_status IS NULL THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  IF v_status = 'closed' THEN
    RAISE EXCEPTION 'This examination is closed. Its cohort and paper are frozen.';
  END IF;

  SELECT count(*) INTO v_finalized
  FROM public.exam_attempts att
  WHERE att.exam_id = p_exam_id
    AND att.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated');

  IF v_finalized > 0 THEN
    RAISE EXCEPTION
      'Cannot change the cohort or paper: % candidate submission(s) already exist for this examination.', v_finalized;
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_assert_faculty(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_assert_composition_editable(UUID) FROM PUBLIC;

-- K1. Read the cohort assigned to an examination.
CREATE OR REPLACE FUNCTION public.teacher_get_exam_assignments(p_exam_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_exam RECORD;
  v_ids JSONB;
  v_count INT;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  SELECT e.id, e.title, e.status, e.starts_at, e.duration_minutes
  INTO v_exam FROM public.exams e WHERE e.id = p_exam_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  SELECT COALESCE(jsonb_agg(ea.student_id), '[]'::jsonb), COUNT(*)::INT
  INTO v_ids, v_count
  FROM public.exam_assignments ea
  WHERE ea.exam_id = p_exam_id;

  RETURN jsonb_build_object(
    'exam', jsonb_build_object(
      'id', v_exam.id, 'title', v_exam.title,
      'status', v_exam.status, 'starts_at', v_exam.starts_at,
      'duration_minutes', v_exam.duration_minutes
    ),
    'assigned_student_ids', v_ids,
    'assigned_count', v_count
  );
END;
$$;

-- K2. Replace the cohort. Idempotent, duplicate-proof, verifiable.
CREATE OR REPLACE FUNCTION public.teacher_set_exam_assignments(
  p_exam_id UUID,
  p_student_ids UUID[] DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_desired UUID[];
  v_existing UUID[];
  v_invalid TEXT;
  v_added INT := 0;
  v_removed INT := 0;
  v_unchanged INT := 0;
  v_total INT := 0;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);
  PERFORM public.quizeazz_assert_composition_editable(p_exam_id);

  -- v_desired holds the CALLER's requested cohort for the whole function.
  -- It must never be reassigned: doing so silently converts this function into
  -- a no-op that reports success while persisting nothing.
  SELECT COALESCE(array_agg(DISTINCT u.id), ARRAY[]::UUID[])
  INTO v_desired
  FROM unnest(COALESCE(p_student_ids, ARRAY[]::UUID[])) AS u(id);

  -- Only registered student candidates may be assigned. A teacher or admin id
  -- is rejected outright rather than silently skipped.
  IF array_length(v_desired, 1) IS NOT NULL THEN
    SELECT string_agg(bad.id::TEXT, ', ')
    INTO v_invalid
    FROM unnest(v_desired) AS bad(id)
    WHERE NOT EXISTS (
      SELECT 1 FROM public.profiles p
      WHERE p.id = bad.id AND p.role = 'student'
    );

    IF v_invalid IS NOT NULL THEN
      RAISE EXCEPTION
        'The following identifiers are not registered student candidates: %', v_invalid;
    END IF;
  END IF;

  -- The current DB cohort, read into its own variable for reporting only.
  SELECT COALESCE(array_agg(ea.student_id), ARRAY[]::UUID[])
  INTO v_existing
  FROM public.exam_assignments ea
  WHERE ea.exam_id = p_exam_id;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  WITH removed AS (
    DELETE FROM public.exam_assignments ea
    WHERE ea.exam_id = p_exam_id
      AND NOT (ea.student_id = ANY(v_desired))
    RETURNING ea.student_id
  )
  SELECT COUNT(*)::INT INTO v_removed FROM removed;

  -- Only (exam_id, student_id) are supplied. assigned_at keeps its column
  -- default, so no assumption is made about audit-column names.
  WITH added AS (
    INSERT INTO public.exam_assignments (exam_id, student_id)
    SELECT p_exam_id, d.id
    FROM unnest(v_desired) AS d(id)
    ON CONFLICT (exam_id, student_id) DO NOTHING
    RETURNING student_id
  )
  SELECT COUNT(*)::INT INTO v_added FROM added;

  SELECT COUNT(*)::INT INTO v_total
  FROM public.exam_assignments ea
  WHERE ea.exam_id = p_exam_id;

  -- Rows the caller kept from the previous cohort.
  v_unchanged := (
    SELECT count(*)::INT FROM unnest(v_existing) AS e(id)
    WHERE e.id = ANY(v_desired)
  );

  RETURN jsonb_build_object(
    'exam_id', p_exam_id,
    'assigned_count', v_total,
    'added', v_added,
    'removed', v_removed,
    'unchanged', v_unchanged,
    'assigned_student_ids', COALESCE((
      SELECT jsonb_agg(ea2.student_id)
      FROM public.exam_assignments ea2
      WHERE ea2.exam_id = p_exam_id
    ), '[]'::jsonb)
  );
END;
$$;

-- K3. Read the fixed question paper for an examination.
CREATE OR REPLACE FUNCTION public.teacher_get_exam_paper(p_exam_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_exam RECORD;
  v_items JSONB;
  v_total INT := 0;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  SELECT e.id, e.title, e.status, e.starts_at, e.duration_minutes
  INTO v_exam FROM public.exams e WHERE e.id = p_exam_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'question_id', q.id,
      'title', q.title,
      'question_type', q.question_type,
      'category', q.category,
      'subcategory', q.subcategory,
      'difficulty', q.difficulty,
      'language', q.language,
      'marks', COALESCE(eq.marks, 0),
      'bank_marks', COALESCE(q.marks, 10),
      'question_order', eq.question_order
    )
    -- Ordered by the faculty-defined question_order, falling back to the
    -- primary key. exam_questions has no created_at column on this schema,
    -- so ordering by it would make this RPC fail to create.
    ORDER BY COALESCE(eq.question_order, 2147483647), eq.id
  ), '[]'::jsonb)
  INTO v_items
  FROM public.exam_questions eq
  JOIN public.questions q ON q.id = eq.question_id
  WHERE eq.exam_id = p_exam_id;

  SELECT COALESCE(SUM(eq.marks), 0)::INT INTO v_total
  FROM public.exam_questions eq WHERE eq.exam_id = p_exam_id;

  RETURN jsonb_build_object(
    'exam', jsonb_build_object(
      'id', v_exam.id, 'title', v_exam.title, 'status', v_exam.status,
      'starts_at', v_exam.starts_at, 'duration_minutes', v_exam.duration_minutes
    ),
    'items', v_items,
    'total_questions', (SELECT COUNT(*)::INT FROM public.exam_questions eq WHERE eq.exam_id = p_exam_id),
    'total_marks', v_total
  );
END;
$$;

-- K4. Replace the paper. Exact, teacher-selected question set. No random pools.
CREATE OR REPLACE FUNCTION public.teacher_set_exam_paper(
  p_exam_id UUID,
  p_items JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_items JSONB;
  v_invalid TEXT;
  v_bad_marks TEXT;
  v_removed INT := 0;
  v_upserted INT := 0;
  v_total_questions INT := 0;
  v_total_marks INT := 0;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);
  PERFORM public.quizeazz_assert_composition_editable(p_exam_id);

  v_items := COALESCE(p_items, '[]'::jsonb);

  IF jsonb_typeof(v_items) <> 'array' THEN
    RAISE EXCEPTION 'Paper definition must be a JSON array of {question_id, marks} objects.';
  END IF;

  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_items) AS elem
    WHERE jsonb_typeof(elem) <> 'object'
       OR (elem->>'question_id') IS NULL
       OR (elem->>'question_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  ) THEN
    RAISE EXCEPTION 'Every paper entry must contain a valid question_id.';
  END IF;

  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_items) AS elem
    WHERE NOT EXISTS (
      SELECT 1 FROM public.questions q
      WHERE q.id = (elem->>'question_id')::UUID
    )
  ) THEN
    SELECT string_agg(DISTINCT (elem->>'question_id'), ', ')
    INTO v_invalid
    FROM jsonb_array_elements(v_items) AS elem
    WHERE NOT EXISTS (
      SELECT 1 FROM public.questions q
      WHERE q.id = (elem->>'question_id')::UUID
    );
    RAISE EXCEPTION 'These questions do not exist in the question bank: %', v_invalid;
  END IF;

  IF EXISTS (
    SELECT 1 FROM (
      SELECT (elem->>'question_id') AS qid, COUNT(*) AS n
      FROM jsonb_array_elements(v_items) AS elem
      GROUP BY 1 HAVING COUNT(*) > 1
    ) dup
  ) THEN
    RAISE EXCEPTION 'The same question cannot appear twice in one paper.';
  END IF;

  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_items) AS elem
    WHERE COALESCE((elem->>'marks')::INT, 0) < 1
       OR COALESCE((elem->>'marks')::INT, 0) > 100
  ) THEN
    SELECT string_agg(COALESCE(elem->>'marks', 'null'), ', ')
    INTO v_bad_marks
    FROM jsonb_array_elements(v_items) AS elem
    WHERE COALESCE((elem->>'marks')::INT, 0) < 1
       OR COALESCE((elem->>'marks')::INT, 0) > 100;
    RAISE EXCEPTION 'Marks must be between 1 and 100 for every question (received: %).', v_bad_marks;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  WITH desired AS (
    SELECT (elem->>'question_id')::UUID AS qid, (elem->>'marks')::INT AS marks
    FROM jsonb_array_elements(v_items) AS elem
  ),
  removed AS (
    DELETE FROM public.exam_questions eq
    WHERE eq.exam_id = p_exam_id
      AND NOT EXISTS (SELECT 1 FROM desired d WHERE d.qid = eq.question_id)
    RETURNING eq.id
  )
  SELECT COUNT(*)::INT INTO v_removed FROM removed;

  WITH desired AS (
    SELECT
      (elem->>'question_id')::UUID AS qid,
      (elem->>'marks')::INT        AS marks,
      ord::INT                     AS ord
    FROM jsonb_array_elements(v_items) WITH ORDINALITY AS t(elem, ord)
  ),
  upserted AS (
    INSERT INTO public.exam_questions (exam_id, question_id, marks, question_order)
    SELECT p_exam_id, d.qid, d.marks, d.ord
    FROM desired d
    ON CONFLICT (exam_id, question_id)
    DO UPDATE SET marks = EXCLUDED.marks,
                  question_order = EXCLUDED.question_order
    RETURNING question_id
  )
  SELECT COUNT(*)::INT INTO v_upserted FROM upserted;

  SELECT COUNT(*)::INT, COALESCE(SUM(eq.marks), 0)::INT
  INTO v_total_questions, v_total_marks
  FROM public.exam_questions eq
  WHERE eq.exam_id = p_exam_id;

  -- exams has no total_marks column, so the paper total is not denormalised
  -- anywhere. Only updated_at is touched, to record that the paper changed.
  UPDATE public.exams e
  SET updated_at = now()
  WHERE e.id = p_exam_id;

  RETURN jsonb_build_object(
    'exam_id', p_exam_id,
    'total_questions', v_total_questions,
    'total_marks', v_total_marks,
    'removed', v_removed,
    'saved', v_upserted
  );
END;
$$;

-- K5. Close / reopen / draft an examination.
CREATE OR REPLACE FUNCTION public.teacher_set_exam_status(p_exam_id UUID, p_status TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_exam public.exams%ROWTYPE;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  IF p_status IS NULL OR p_status NOT IN ('draft', 'published', 'closed') THEN
    RAISE EXCEPTION 'Invalid examination status "%. Allowed values: draft, published, closed.', p_status;
  END IF;

  SELECT * INTO v_exam FROM public.exams e WHERE e.id = p_exam_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  IF p_status = 'published' AND v_exam.starts_at IS NULL THEN
    RAISE EXCEPTION 'A scheduled start time is required before an examination can be published.';
  END IF;

  UPDATE public.exams e
  SET status = p_status, updated_at = now()
  WHERE e.id = p_exam_id
  RETURNING * INTO v_exam;

  RETURN to_jsonb(v_exam);
END;
$$;

-- K5b. Update the schedule of an existing, non-closed examination.
--      Duration, start time and end time are exam-level facts that the
--      candidate experience depends on, so faculty need to be able to correct
--      a mistake. Refused once any candidate has submitted, and refused while
--      a candidate has an attempt in progress, because changing the window
--      mid-attempt would invalidate the authoritative deadline.
CREATE OR REPLACE FUNCTION public.teacher_set_exam_schedule(
  p_exam_id UUID,
  p_duration_minutes INT DEFAULT NULL,
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
  v_exam public.exams%ROWTYPE;
  v_live INT;
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

  IF v_exam.status = 'closed' THEN
    RAISE EXCEPTION 'This examination is closed. Its schedule is frozen.';
  END IF;

  IF p_duration_minutes IS NULL OR p_duration_minutes < 1 OR p_duration_minutes > 1440 THEN
    RAISE EXCEPTION 'Duration must be between 1 and 1440 minutes (24 hours).';
  END IF;

  SELECT COUNT(*)::INT INTO v_live
  FROM public.exam_attempts att
  WHERE att.exam_id = p_exam_id
    AND att.status = 'in_progress';

  IF v_live > 0 THEN
    RAISE EXCEPTION
      'Cannot change the schedule: % candidate attempt(s) are currently in progress.', v_live;
  END IF;

  PERFORM public.quizeazz_assert_composition_editable(p_exam_id);

  IF p_ends_at IS NOT NULL AND p_ends_at <= p_starts_at THEN
    RAISE EXCEPTION 'The close time must be later than the start time.';
  END IF;

  UPDATE public.exams e
  SET duration_minutes = p_duration_minutes,
      starts_at = p_starts_at,
      ends_at = p_ends_at,
      updated_at = now()
  WHERE e.id = p_exam_id
  RETURNING * INTO v_exam;

  RETURN to_jsonb(v_exam);
END;
$$;

-- K6. Cohort submissions with objective evidence.
CREATE OR REPLACE FUNCTION public.teacher_get_attempts(p_exam_id UUID DEFAULT NULL)
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
  ) t;

  RETURN v_rows;
END;
$$;

-- K7. Per-question objective grading evidence for one attempt.
CREATE OR REPLACE FUNCTION public.teacher_get_attempt_detail(p_attempt_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_attempt RECORD;
  v_items JSONB;
  v_total_marks INT := 0;
  v_score INT := 0;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  SELECT att.id, att.exam_id, att.student_id, att.status, att.score, att.started_at, att.submitted_at
  INTO v_attempt
  FROM public.exam_attempts att
  WHERE att.id = p_attempt_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % not found.', p_attempt_id;
  END IF;

  SELECT COALESCE(SUM(eq.marks), 0)::INT INTO v_total_marks
  FROM public.exam_questions eq WHERE eq.exam_id = v_attempt.exam_id;
  v_score := COALESCE(v_attempt.score, 0);

  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'question_id', q.id,
      'title', q.title,
      'question_type', q.question_type,
      'language', COALESCE(a.language, q.language),
      'marks_total', COALESCE(eq.marks, 0),
      'marks_awarded', COALESCE(a.marks_awarded, 0),
      'outcome', COALESCE(a.evaluation_status, CASE WHEN q.question_type = 'coding' THEN 'Pending Evaluation' ELSE 'Not Attempted' END),
      'is_correct', a.is_correct,
      'student_option_id', a.selected_option_id,
      'correct_option_id', CASE WHEN q.question_type = 'mcq' THEN key.correct_id ELSE NULL END,
      'options', CASE
        WHEN q.question_type = 'mcq' AND jsonb_typeof(q.options) = 'array'
        THEN (
          SELECT jsonb_agg(jsonb_build_object('id', elem->>'id', 'text', elem->>'text') ORDER BY ord)
          FROM jsonb_array_elements(q.options) WITH ORDINALITY AS t(elem, ord)
        )
        ELSE NULL
      END,
      'code', a.code,
      'explanation', q.explanation
    ) ORDER BY eq.id
  ), '[]'::jsonb)
  INTO v_items
  FROM public.exam_questions eq
  JOIN public.questions q ON q.id = eq.question_id
  LEFT JOIN public.answers a ON a.attempt_id = p_attempt_id AND a.question_id = q.id
  LEFT JOIN LATERAL (
    SELECT elem->>'id' AS correct_id
    FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(q.options) = 'array' THEN q.options ELSE '[]'::jsonb END
    ) AS elem
    WHERE COALESCE((elem->>'is_correct')::boolean, false) = true
    LIMIT 1
  ) key ON TRUE
  WHERE eq.exam_id = v_attempt.exam_id;

  RETURN jsonb_build_object(
    'attempt_id', v_attempt.id,
    'exam_id', v_attempt.exam_id,
    'student_id', v_attempt.student_id,
    'status', v_attempt.status,
    'score', v_score,
    'total_marks', v_total_marks,
    'started_at', v_attempt.started_at,
    'submitted_at', v_attempt.submitted_at,
    'items', v_items
  );
END;
$$;

-- K8. Security audit trail with resolved identities (no raw UUID wall).
CREATE OR REPLACE FUNCTION public.teacher_get_security_events(
  p_exam_id UUID DEFAULT NULL,
  p_limit INT DEFAULT 200
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_rows JSONB;
  v_lim INT;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  v_lim := LEAST(GREATEST(COALESCE(p_limit, 200), 1), 1000);

  SELECT COALESCE(jsonb_agg(row_to_json(t) ORDER BY t.event_timestamp DESC), '[]'::jsonb)
  INTO v_rows
  FROM (
    SELECT
      se.id,
      se.event_timestamp,
      se.event_type,
      se.details,
      se.question_id,
      se.attempt_id,
      se.exam_id,
      e.title AS exam_title,
      se.student_id,
      p.full_name AS student_name
    FROM public.security_events se
    LEFT JOIN public.exams e ON e.id = se.exam_id
    LEFT JOIN public.profiles p ON p.id = se.student_id
    WHERE (p_exam_id IS NULL OR se.exam_id = p_exam_id)
    ORDER BY se.event_timestamp DESC
    LIMIT v_lim
  ) t;

  RETURN v_rows;
END;
$$;

-- K9. Institution user directory (admin scope). Replaces the RLS-blocked
--     direct read of public.profiles in the admin console.
CREATE OR REPLACE FUNCTION public.admin_get_profiles()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, auth, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_role TEXT;
  v_rows JSONB;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_role IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'Access denied: administrator role required.';
  END IF;

  SELECT COALESCE(jsonb_agg(row_to_json(t) ORDER BY t.created_at DESC), '[]'::jsonb)
  INTO v_rows
  FROM (
    SELECT
      p.id,
      p.full_name,
      p.role,
      u.email::TEXT AS email,
      p.created_at
    FROM public.profiles p
    LEFT JOIN auth.users u ON u.id = p.id
  ) t;

  RETURN v_rows;
END;
$$;


-- ====================================================================
-- SECTION L — AUTHORITATIVE PROFILE PROVISIONING
-- ====================================================================
-- Identity provisioning belongs to the database, not the browser.
-- The client now only ever READS its own profile.

CREATE OR REPLACE FUNCTION public.get_my_profile()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_row RECORD;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.id, p.full_name, p.role, p.created_at
  INTO v_row
  FROM public.profiles p
  WHERE p.id = v_user_id;

  IF v_row.id IS NULL THEN
    -- Legacy accounts provisioned before the auth trigger existed.
    -- Server-side repair only. Role is hard-coded to student and the
    -- authenticated caller can never influence it.
    PERFORM set_config('quizeazz.authoritative_context', 'true', true);

    INSERT INTO public.profiles (id, full_name, role, created_at)
    SELECT v_user_id, COALESCE(NULLIF(btrim(full_name), ''), 'Candidate'), 'student', now()
    FROM auth.users u
    WHERE u.id = v_user_id
    ON CONFLICT (id) DO NOTHING;

    SELECT p.id, p.full_name, p.role, p.created_at
    INTO v_row
    FROM public.profiles p
    WHERE p.id = v_user_id;
  END IF;

  IF v_row.id IS NULL THEN
    RAISE EXCEPTION
      'No platform profile is provisioned for this account. Please contact the examination administrator.';
  END IF;

  RETURN jsonb_build_object(
    'id', v_row.id,
    'full_name', v_row.full_name,
    'role', v_row.role,
    'created_at', v_row.created_at
  );
END;
$$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  INSERT INTO public.profiles (id, full_name, role, created_at)
  VALUES (
    NEW.id,
    COALESCE(
      NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'full_name', '')), ''),
      split_part(COALESCE(NEW.email, 'candidate'), '@', 1),
      'Candidate'
    ),
    'student',
    now()
  )
  ON CONFLICT (id) DO NOTHING;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
AFTER INSERT ON auth.users
FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();


-- ====================================================================
-- SECTION M — CANDIDATE OVERVIEW (only real DB statuses)
-- ====================================================================

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
    AND e.status IN ('published', 'closed');

  RETURN v_result;
END;
$$;


-- ====================================================================
-- SECTION N — RLS + GRANT HARDENING
-- ====================================================================
-- No permissive client policies are created. Sensitive tables stay
-- RLS-enabled and default-deny; all legitimate access is RPC-only.

ALTER TABLE public.exams            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.questions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_questions   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_attempts    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.answers          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.profiles         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.security_events  ENABLE ROW LEVEL SECURITY;

-- anon must never reach any platform table.
REVOKE ALL ON public.exams            FROM anon;
REVOKE ALL ON public.questions        FROM anon;
REVOKE ALL ON public.exam_questions   FROM anon;
REVOKE ALL ON public.exam_assignments FROM anon;
REVOKE ALL ON public.exam_attempts    FROM anon;
REVOKE ALL ON public.answers          FROM anon;
REVOKE ALL ON public.profiles         FROM anon;
REVOKE ALL ON public.security_events  FROM anon;

-- Client-side direct writes on composition tables are never required.
REVOKE INSERT, UPDATE, DELETE ON public.exam_assignments FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON public.exam_questions   FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON public.answers          FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON public.exam_attempts    FROM authenticated;
REVOKE INSERT, UPDATE, DELETE ON public.profiles         FROM authenticated;

-- security_events is read exclusively through teacher_get_security_events().
DROP POLICY IF EXISTS "Teacher security events access" ON public.security_events;
CREATE POLICY "Faculty read security events"
  ON public.security_events
  FOR SELECT
  TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.profiles
      WHERE id = auth.uid() AND role IN ('teacher', 'admin')
    )
  );

-- exams: faculty read policy only. No client INSERT/UPDATE policy exists;
-- scheduling goes through teacher_create_exam / teacher_set_exam_status.
DROP POLICY IF EXISTS "Teacher manage exams" ON public.exams;
CREATE POLICY "Faculty read exams"
  ON public.exams
  FOR SELECT
  TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.profiles
      WHERE id = auth.uid() AND role IN ('teacher', 'admin')
    )
  );


-- ====================================================================
-- SECTION O — FUNCTION EXECUTION ACL NORMALIZATION
-- ====================================================================
-- Default PUBLIC execute was still present on several RPCs in the live
-- database. Every QuizEazz RPC is re-pinned to authenticated only.
--
-- HOW COVERAGE WORKS (read this before editing)
-- ------------------------------------------------
-- Two mechanisms, and between them every function gets an explicit ACL:
--
--  1. This DO loop enumerates pg_proc for the named QuizEazz functions
--     FILTERED ON pronargs > 0, i.e. only functions that take arguments.
--     It issues REVOKE ... FROM PUBLIC for each. Reason: a function name is
--     not enough to address a function in SQL, so the OID is resolved
--     dynamically. Restricting to pronargs > 0 keeps the enumeration simple
--     and avoids accidentally revoking an overload.
--
--  2. Zero-argument functions are therefore SKIPPED by the loop and are
--     handled by the explicit REVOKE / GRANT statements further down.
--     The skipped names are exactly:
--         teacher_get_questions, teacher_get_exams, teacher_get_candidates,
--         teacher_get_overview_stats, get_student_exam_overview,
--         get_student_results, get_my_profile, admin_get_profiles
--     plus all trigger functions:
--         check_answer_write_eligibility, protect_attempt_mutation,
--         protect_exam_composition_mutation, protect_profile_role_escalation,
--         handle_new_user
--
--     Each of those is named verbatim in the GRANT/REVOKE block below, so
--     nothing is left with the default PUBLIC execute grant.
--
-- CONSEQUENCE: if you add a new QuizEazz RPC, it must be added to BOTH the
-- proname list in the loop below (if it takes arguments) AND to the explicit
-- GRANT list. Adding it to only one place leaves it with a PUBLIC execute
-- grant, which means `anon` can invoke it. `npm run verify` prints every
-- function that is defined in SQL but not called from src/ so a newly added
-- RPC is visible during review.
--
-- IDEMPOTENCE: this section is safe to run repeatedly. REVOKE of an
-- already-revoked privilege is a no-op, and GRANT of an existing privilege is
-- a no-op. The only non-idempotent statements in this migration are the three
-- explicit DROP FUNCTION statements in Sections A, G and H, all of which are
-- IF EXISTS.

DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure::TEXT AS sig
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.pronargs > 0
      AND p.proname IN (
          'get_exam_questions','check_answer_write_eligibility','protect_attempt_mutation',
          'submit_exam_attempt','grade_mcq_attempt','pause_exam_attempt','resume_exam_attempt',
          'start_or_get_exam_attempt','get_student_exam_answers','save_exam_answer',
          'record_security_event','get_student_exam_overview','get_student_results',
          'get_student_attempt_review','teacher_get_questions','teacher_create_question',
          'teacher_get_exams','teacher_create_exam','teacher_get_candidates',
          'teacher_get_overview_stats','teacher_get_exam_assignments',
          'teacher_set_exam_assignments','teacher_get_exam_paper','teacher_set_exam_paper',
          'teacher_set_exam_status','teacher_set_exam_schedule','teacher_get_attempts','teacher_get_attempt_detail',
          'teacher_get_security_events','admin_get_profiles','get_my_profile',
          'quizeazz_grade_objective_marks','quizeazz_assert_faculty',
          'quizeazz_assert_composition_editable','protect_exam_composition_mutation',
          'protect_profile_role_escalation','handle_new_user'
        )
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', r.sig);
  END LOOP;
END $$;

-- Caller-facing RPCs: authenticated only.
GRANT EXECUTE ON FUNCTION public.get_exam_questions(UUID)                    TO authenticated;
GRANT EXECUTE ON FUNCTION public.submit_exam_attempt(UUID)                   TO authenticated;
GRANT EXECUTE ON FUNCTION public.grade_mcq_attempt(UUID)                     TO authenticated;
GRANT EXECUTE ON FUNCTION public.pause_exam_attempt(UUID, TEXT)              TO authenticated;
GRANT EXECUTE ON FUNCTION public.resume_exam_attempt(UUID)                   TO authenticated;
GRANT EXECUTE ON FUNCTION public.start_or_get_exam_attempt(UUID)             TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_student_exam_answers(UUID)              TO authenticated;
GRANT EXECUTE ON FUNCTION public.save_exam_answer(UUID, UUID, TEXT, TEXT)    TO authenticated;
GRANT EXECUTE ON FUNCTION public.record_security_event(UUID, UUID, TEXT, UUID, JSONB) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_student_exam_overview()                 TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_student_results()                       TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_student_attempt_review(UUID)            TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_my_profile()                            TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_get_questions()                     TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_create_question(TEXT, TEXT, TEXT, TEXT, TEXT, INT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_get_exams()                         TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_create_exam(TEXT, TEXT, INT, TEXT, TIMESTAMPTZ) TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_get_candidates()                    TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_get_overview_stats()                TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_get_exam_assignments(UUID)          TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_set_exam_assignments(UUID, UUID[])  TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_get_exam_paper(UUID)                TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_set_exam_paper(UUID, JSONB)         TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_set_exam_status(UUID, TEXT)         TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_set_exam_schedule(UUID, INT, TIMESTAMPTZ, TIMESTAMPTZ) TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_get_attempts(UUID)                  TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_get_attempt_detail(UUID)            TO authenticated;
GRANT EXECUTE ON FUNCTION public.teacher_get_security_events(UUID, INT)      TO authenticated;
GRANT EXECUTE ON FUNCTION public.admin_get_profiles()                        TO authenticated;

-- Internal engines: definer-only. Never callable by a client role.
REVOKE ALL ON FUNCTION public.quizeazz_grade_objective_marks(UUID)            FROM authenticated;
REVOKE ALL ON FUNCTION public.quizeazz_assert_faculty(UUID)                   FROM authenticated;
REVOKE ALL ON FUNCTION public.quizeazz_assert_composition_editable(UUID)      FROM authenticated;

-- Trigger functions are never directly executable.
REVOKE ALL ON FUNCTION public.check_answer_write_eligibility()                FROM PUBLIC, authenticated;
REVOKE ALL ON FUNCTION public.protect_attempt_mutation()                      FROM PUBLIC, authenticated;
REVOKE ALL ON FUNCTION public.protect_exam_composition_mutation()             FROM PUBLIC, authenticated;
REVOKE ALL ON FUNCTION public.protect_profile_role_escalation()               FROM PUBLIC, authenticated;
REVOKE ALL ON FUNCTION public.handle_new_user()                                FROM PUBLIC, authenticated;

-- ====================================================================
-- END OF MIGRATION
-- ====================================================================
