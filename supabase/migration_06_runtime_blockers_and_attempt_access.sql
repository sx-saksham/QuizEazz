-- ====================================================================
-- QUIZEAZZ — MIGRATION 6: RUNTIME BLOCKERS & ATTEMPT-ACCESS CORRECTIONS
-- --------------------------------------------------------------------
-- Run AFTER migrations 1, 2, 3, 4 and 5. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   PL/pgSQL validates a statement the FIRST TIME IT EXECUTES, not when the
--   function is created. Two functions created by migration 2 therefore
--   installed cleanly while referencing columns that do not exist in the live
--   schema, and now fail at runtime:
--
--     save_exam_answer()   references answers.updated_at  (column is saved_at)
--     get_student_results() references exams.total_marks  (column does not exist)
--
--   save_exam_answer is the ONLY write path for candidate answers, so its
--   failure makes the entire examination unusable.
--
-- THIS FILE ALSO
--   - separates NEW-attempt eligibility from EXISTING-attempt access, so a
--     candidate holding a valid in_progress attempt is never locked out of
--     their own paper by a teacher closing the exam or by ends_at elapsing
--   - populates exam_attempts.max_score inside the authoritative grading
--     transaction, so the recorded ceiling always exists
--
-- SCOPE: function-level only.
--   No table is created, altered or dropped. No column is added or removed.
--   No RLS policy is touched. No data is deleted.
--   The only row-level statement is the max_score backfill in Section 6,
--   which writes one derived column and never removes anything.
--
-- IDEMPOTENT: safe to run more than once.
--   Every function uses CREATE OR REPLACE with an unchanged signature.
--   The backfill is a WHERE max_score IS NULL UPDATE, so it stops changing
--   once it has done its work.
-- ====================================================================


-- ====================================================================
-- SECTION A — P0-1  save_exam_answer: answers.updated_at -> answers.saved_at
-- --------------------------------------------------------------------
-- Signature unchanged: save_exam_answer(UUID, UUID, TEXT, TEXT)
-- Authorization, lifecycle checks, pause gate, authoritative deadline and
-- question-membership checks are all preserved byte-for-byte. Only the
-- nonexistent timestamp column is replaced by the real one.
-- SECURITY DEFINER preserved.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.save_exam_answer(
  p_attempt_id UUID,
  p_question_id UUID,
  p_content TEXT,
  p_language TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_user_role TEXT;
  v_attempt RECORD;
  v_duration INT;
  v_deadline TIMESTAMPTZ;
  v_q_type TEXT;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required to save answers.';
  END IF;

  SELECT role INTO v_user_role FROM public.profiles WHERE id = v_user_id;

  -- 1. Fetch attempt and exam duration
  SELECT ea.student_id, ea.status, ea.is_paused, ea.exam_id, ea.started_at, e.duration_minutes
  INTO v_attempt
  FROM public.exam_attempts ea
  JOIN public.exams e ON e.id = ea.exam_id
  WHERE ea.id = p_attempt_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination attempt % does not exist.', p_attempt_id;
  END IF;

  -- 2. Verify ownership
  IF v_attempt.student_id <> v_user_id AND (v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL) THEN
    RAISE EXCEPTION 'Access denied: You cannot save answers to an attempt belonging to another candidate.';
  END IF;

  -- 3. Check attempt lifecycle and pause state
  IF v_attempt.status <> 'in_progress' THEN
    RAISE EXCEPTION 'Save rejected: attempt status is %, not in_progress.', v_attempt.status;
  END IF;

  IF v_attempt.is_paused = TRUE THEN
    RAISE EXCEPTION 'Save rejected: examination session is currently PAUSED.';
  END IF;

  -- 4. Authoritative deadline check
  -- This uses the candidate's OWN start time plus the exam duration, so a
  -- candidate who already began is never extended by exam.ends_at.
  v_duration := COALESCE(v_attempt.duration_minutes, 60);
  v_deadline := v_attempt.started_at + (v_duration * INTERVAL '1 minute') + INTERVAL '15 seconds';

  IF now() > v_deadline THEN
    RAISE EXCEPTION 'Save rejected: authoritative examination deadline has expired.';
  END IF;

  -- 5. Verify question belongs to this exam
  IF NOT EXISTS (
    SELECT 1
    FROM public.exam_questions
    WHERE exam_id = v_attempt.exam_id
      AND question_id = p_question_id
  ) THEN
    RAISE EXCEPTION 'Question % is not part of examination %.', p_question_id, v_attempt.exam_id;
  END IF;

  -- 6. Check question type from questions table
  SELECT question_type INTO v_q_type FROM public.questions WHERE id = p_question_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Question % not found.', p_question_id;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  -- 7. Persist answer: MCQ -> selected_option_id; Coding -> code & language
  -- Candidate clients NEVER supply correctness or marks.
  -- Uses atomic PostgreSQL upsert to prevent duplicate answer races.
  -- answers has saved_at, not updated_at: saved_at is this schema's
  -- answer save/update timestamp, so it is what both branches must write.
  IF v_q_type = 'mcq' THEN
    INSERT INTO public.answers (
      attempt_id,
      question_id,
      selected_option_id,
      code,
      language,
      saved_at
    ) VALUES (
      p_attempt_id,
      p_question_id,
      p_content,
      NULL,
      NULL,
      now()
    )
    ON CONFLICT (attempt_id, question_id) DO UPDATE
    SET
      selected_option_id = EXCLUDED.selected_option_id,
      saved_at = now();
  ELSE
    INSERT INTO public.answers (
      attempt_id,
      question_id,
      selected_option_id,
      code,
      language,
      saved_at
    ) VALUES (
      p_attempt_id,
      p_question_id,
      NULL,
      p_content,
      COALESCE(p_language, 'python'),
      now()
    )
    ON CONFLICT (attempt_id, question_id) DO UPDATE
    SET
      code = EXCLUDED.code,
      language = COALESCE(EXCLUDED.language, public.answers.language, 'python'),
      saved_at = now();
  END IF;

  RETURN TRUE;
END;
$$;


-- ====================================================================
-- SECTION B — P0-2  get_student_results: exams.total_marks removed
-- --------------------------------------------------------------------
-- Signature unchanged: get_student_results()
-- Returned JSON shape is unchanged, plus one additive, derived key
-- (programming_pending) used by the results table so it never has to
-- claim "Evaluated" while programming questions are still unevaluated.
-- Keys: id, exam_id, exam_title, date, score, total_marks, status,
--       programming_pending.
-- exams has no total_marks column, so the ceiling now comes from the
-- attempt's own recorded max_score, falling back to the live paper sum.
-- The previous hardcoded fallback of 20 was a fabricated value and is gone.
--
-- programming_pending is read from the grading block that
-- quizeazz_grade_objective_marks() already writes into
-- exam_attempts.metadata. It adds no column, no write and no new authority.
-- The regexp guard keeps a malformed value from raising instead of
-- degrading to 0.
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
      'score', att.score,
      'total_marks', COALESCE(att.max_score, (
        SELECT COALESCE(sum(eq.marks), 0)
        FROM public.exam_questions eq
        WHERE eq.exam_id = att.exam_id
      )),
      'status', att.status,
      'programming_pending', COALESCE(
        (regexp_match(
          att.metadata -> 'grading' ->> 'programming_pending',
          '^[0-9]+$'
        ))[1]::INT,
        0
      )
    ) ORDER BY att.created_at DESC
  ), '[]'::jsonb)
  INTO v_result
  FROM public.exam_attempts att
  JOIN public.exams e ON e.id = att.exam_id
  WHERE att.student_id = v_user_id
    AND att.status IN ('submitted', 'evaluated', 'timed_out');

  RETURN v_result;
END;
$$;


-- ====================================================================
-- SECTION C — P1-1a  start_or_get_exam_attempt: ACTIVE attempts survive
-- --------------------------------------------------------------------
-- Signature unchanged: start_or_get_exam_attempt(UUID)
-- Returned JSON shape unchanged.
--
-- NEW-ATTEMPT ELIGIBILITY (no attempt row at all) is fully unchanged:
--   assignment required, status must be 'published', now() >= starts_at,
--   now() <= ends_at.
--
-- ACTIVE-ATTEMPT ACCESS (this candidate holds a row with status
-- 'in_progress') keeps every authorization rule but is no longer blocked by
-- the exam being closed or by ends_at having elapsed. The candidate still
-- needs their assignment, and the row is still keyed to their own uid, so no
-- cross-attempt access is possible.
--
-- FINALIZED attempts (submitted, timed_out, evaluating, evaluated) are NOT
-- active. They do not bypass the schedule gates and therefore cannot re-open
-- the exam-taking flow after closure. Review and results remain reachable
-- through get_student_attempt_review() and get_student_results(), neither of
-- which consults exams.status.
--
-- The write deadline in save_exam_answer is unchanged and remains the real
-- authority on how long a candidate may write.
-- ====================================================================
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
  v_has_attempt BOOLEAN := FALSE;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required to start or load an examination attempt.';
  END IF;

  SELECT COALESCE(SUM(eq.marks), 0)::INT INTO v_paper_marks
  FROM public.exam_questions eq
  WHERE eq.exam_id = p_exam_id;

  SELECT p.role INTO v_user_role FROM public.profiles p WHERE p.id = v_user_id;

  -- exams has no total_marks column. The paper total is the exam_questions sum.
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
    -- Authorization is unconditional: an assigned candidate only.
    IF NOT EXISTS (
      SELECT 1 FROM public.exam_assignments ea
      WHERE ea.exam_id = p_exam_id AND ea.student_id = v_user_id
    ) THEN
      RAISE EXCEPTION 'Access denied: You are not assigned to this examination.';
    END IF;

    -- ACTIVE-attempt test. Only a LIVE in_progress attempt grants the
    -- schedule bypass. A finalized attempt (submitted, timed_out,
    -- evaluating, evaluated) is deliberately NOT an active attempt, so it
    -- cannot re-open the exam-taking flow once the exam is closed.
    -- That path stays available through get_student_attempt_review() and
    -- get_student_results(), which never consult exams.status.
    SELECT EXISTS (
      SELECT 1 FROM public.exam_attempts att
      WHERE att.exam_id = p_exam_id
        AND att.student_id = v_user_id
        AND att.status = 'in_progress'
    ) INTO v_has_attempt;

    IF NOT v_has_attempt THEN
      -- NEW ATTEMPT (no row at all, or an already-finalized row):
      -- schedule enforcement is absolute and unchanged.
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
    -- ACTIVE ATTEMPT: the candidate is mid-paper, so a closure or an
    -- elapsed ends_at must not lock them out of their own paper. The
    -- assignment check above still applies, and the attempt row is fetched
    -- with student_id = auth.uid(), so no other candidate's work is reachable.
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
-- SECTION D — P1-1b  get_exam_questions: existing attempts survive
-- --------------------------------------------------------------------
-- Signature unchanged: get_exam_questions(UUID)
-- Returned JSON shape unchanged.
--
-- A candidate is authorized when EITHER
--   (a) the exam is published, starts_at has passed, ends_at has not
--       -- covers the ordinary in-window case, and
--   (b) the candidate already holds an in_progress attempt for this exam
--       -- covers a closure or an elapsed ends_at mid-attempt
-- AND in both cases the candidate is assigned to the exam.
--
-- The projection is unchanged: no reference_solution, no explanation, and
-- options carry only {id, text}.
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
    -- Assignment is required in BOTH branches.
    IF EXISTS (
      SELECT 1 FROM public.exam_assignments ea
      WHERE ea.exam_id = p_exam_id AND ea.student_id = v_user_id
    ) AND (
      -- (a) exam is open right now
      EXISTS (
        SELECT 1 FROM public.exams e
        WHERE e.id = p_exam_id
          AND e.status = 'published'
          AND (e.starts_at IS NULL OR e.starts_at <= now())
          AND (e.ends_at IS NULL OR e.ends_at >= now())
      )
      -- OR (b) candidate already holds a live in_progress attempt
      OR EXISTS (
        SELECT 1 FROM public.exam_attempts att
        WHERE att.exam_id = p_exam_id
          AND att.student_id = v_user_id
          AND att.status = 'in_progress'
      )
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


-- ====================================================================
-- SECTION E — P1-2  quizeazz_grade_objective_marks: record max_score
-- --------------------------------------------------------------------
-- Signature unchanged: quizeazz_grade_objective_marks(UUID)
-- Internal engine. Still definer-only; it is called from inside
-- submit_exam_attempt, never from a browser.
--
-- max_score is derived from the paper (SUM(exam_questions.marks)) inside the
-- same FOR UPDATE transaction that already computes score, so the recorded
-- ceiling and the score are always written atomically together.
-- No client input participates.
-- ====================================================================
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
  v_paper_marks INT := 0;
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

  -- Authoritative paper ceiling. Derived, never supplied by a client.
  SELECT COALESCE(SUM(eq.marks), 0)::INT INTO v_paper_marks
  FROM public.exam_questions eq
  WHERE eq.exam_id = v_attempt.exam_id;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  IF v_attempt.status IN ('submitted', 'timed_out') THEN
    UPDATE public.exam_attempts SET status = 'evaluating' WHERE id = p_attempt_id;
  END IF;

  -- 1. Deterministic MCQ grading against the private key.
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
      saved_at = now()
    FROM mcq_eval me
    WHERE a.id = me.answer_id
    RETURNING a.marks_awarded
  )
  SELECT COALESCE(SUM(marks_awarded), 0), COUNT(*)
  INTO v_mcq_score, v_updated_count
  FROM updated_answers;

  -- 2. Programming questions: no execution runner is connected.
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

  -- 3. Freeze the attempt: score AND max_score in the same statement.
  UPDATE public.exam_attempts ea
  SET
    score = COALESCE((
      SELECT SUM(a.marks_awarded)
      FROM public.answers a
      WHERE a.attempt_id = p_attempt_id
    ), 0)::INT,
    max_score = v_paper_marks,
    status = 'evaluated',
    metadata = COALESCE(ea.metadata, '{}'::jsonb) || jsonb_build_object(
      'grading', jsonb_build_object(
        'mcq_evaluated', true,
        'mcq_score', v_mcq_score,
        'programming_evaluated', false,
        'programming_pending', v_pending_programs,
        'programming_engine', 'Judge0 CE (not connected)',
        'max_score', v_paper_marks,
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
    'programming_evaluated', false,
    'max_score', v_paper_marks
  );
END;
$$;


-- ====================================================================
-- SECTION F — P1-2  max_score backfill for attempts already finalized
-- --------------------------------------------------------------------
-- Attempts evaluated before this migration have max_score = NULL. The paper
-- is frozen once a submission exists (quizeazz_assert_composition_editable
-- refuses edits), so deriving the ceiling now yields the same value the
-- grading transaction would have produced.
--
-- This only fills a NULL column. It never deletes or rewrites a score, and
-- it never touches an attempt that is still in_progress.
--
-- The trusted flag is required: exam_attempts has a BEFORE UPDATE trigger
-- that refuses writes when no role can be resolved, which is the case in
-- the SQL editor.
-- ====================================================================
DO $$
DECLARE
  v_rows INT;
BEGIN
  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.exam_attempts att
  SET max_score = COALESCE((
    SELECT SUM(eq.marks)
    FROM public.exam_questions eq
    WHERE eq.exam_id = att.exam_id
  ), 0)::INT
  WHERE att.max_score IS NULL
    AND att.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated');

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RAISE NOTICE 'max_score backfilled on % already-finalized attempt(s).', v_rows;
END $$;


-- ====================================================================
-- SECTION G — RE-ESTABLISH THE EXECUTE ACL
-- --------------------------------------------------------------------
-- CREATE OR REPLACE keeps an existing function's ACL, but relying on that
-- alone is fragile: the default privileges were narrowed by migration 5 and
-- any future edit could widen access again. The intended policy is stated
-- explicitly here so this file is self-sufficient.
--
--   External RPCs : anon NO, PUBLIC NO, authenticated YES, service_role YES
--   Internal      : anon NO, PUBLIC NO, authenticated NO, service_role YES
-- ====================================================================
DO $$
DECLARE
  r           RECORD;
  v_external TEXT[] := ARRAY[
    'save_exam_answer',
    'get_student_results',
    'start_or_get_exam_attempt',
    'get_exam_questions'
  ];
  v_internal TEXT[] := ARRAY[
    'quizeazz_grade_objective_marks'
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
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',        r.sig);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon',           r.sig);
    EXECUTE format('GRANT  EXECUTE ON FUNCTION %s TO authenticated', r.sig);
    EXECUTE format('GRANT  EXECUTE ON FUNCTION %s TO service_role',  r.sig);
    v_count := v_count + 1;
  END LOOP;

  FOR r IN
    SELECT p.oid::regprocedure::TEXT AS sig
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname = ANY(v_internal)
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',        r.sig);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon',           r.sig);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', r.sig);
    EXECUTE format('GRANT  EXECUTE ON FUNCTION %s TO service_role',  r.sig);
    v_count := v_count + 1;
  END LOOP;

  RAISE NOTICE 'ACLs re-established on % replaced function(s).', v_count;
END $$;


-- ====================================================================
-- SECTION H — VERIFICATION (read-only)
-- ====================================================================

-- H1. Every replaced function exists, is SECURITY DEFINER, and has the
--     intended ACL. Expect all rows anon=false, public=false.
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
    'save_exam_answer','get_student_results','start_or_get_exam_attempt',
    'get_exam_questions','quizeazz_grade_objective_marks'
  )
ORDER BY p.proname, pg_get_function_identity_arguments(p.oid);

-- H2. Internal engine must NOT be callable by authenticated. Expect 0 rows.
SELECT
  p.proname AS function_name,
  pg_get_function_identity_arguments(p.oid) AS arguments
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.proname = 'quizeazz_grade_objective_marks'
  AND has_function_privilege('authenticated', p.oid, 'EXECUTE');

-- H3. No function anywhere may be executable by anon. Expect 0 rows.
SELECT
  p.proname AS function_name,
  pg_get_function_identity_arguments(p.oid) AS arguments
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND has_function_privilege('anon', p.oid, 'EXECUTE')
ORDER BY p.proname;

-- H4. P1-2 invariant: every finalized attempt must carry both score and
--     max_score. Expect 0 rows.
SELECT
  att.id,
  att.exam_id,
  att.student_id,
  att.status,
  att.score,
  att.max_score
FROM public.exam_attempts att
WHERE att.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated')
  AND (att.score IS NULL OR att.max_score IS NULL);

-- H5. max_score must equal the paper total for every finalized attempt.
--     Expect 0 rows.
SELECT
  att.id,
  att.max_score,
  COALESCE((
    SELECT SUM(eq.marks) FROM public.exam_questions eq
    WHERE eq.exam_id = att.exam_id
  ), 0) AS paper_marks
FROM public.exam_attempts att
WHERE att.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated')
  AND att.max_score IS DISTINCT FROM COALESCE((
    SELECT SUM(eq.marks) FROM public.exam_questions eq
    WHERE eq.exam_id = att.exam_id
  ), 0);

-- H6. Schema discipline: confirms migration 6 introduced no new columns.
SELECT
  r.table_name,
  r.column_name,
  EXISTS (
    SELECT 1 FROM information_schema.columns c
    WHERE c.table_schema = 'public'
      AND c.table_name = r.table_name
      AND c.column_name = r.column_name
  ) AS present
FROM (VALUES
  ('answers',       'saved_at'),
  ('answers',       'updated_at'),
  ('exams',         'total_marks'),
  ('exam_attempts', 'max_score'),
  ('exam_questions','question_order')
) AS r(table_name, column_name)
ORDER BY r.table_name, r.column_name;

-- H7. programming_pending derivation used by the results table.
--     Every programming answer still lacking a real verdict must be counted,
--     so the student UI can say "Evaluation Pending" instead of "Evaluated".
--     The derived value must equal the count of non-conclusive answer rows.
--     Expect 0 mismatched rows.
SELECT
  att.id,
  att.status,
  att.score,
  derived.programming_pending,
  actual.pending_count
FROM public.exam_attempts att
CROSS JOIN LATERAL (
  SELECT COALESCE(
    (regexp_match(
      att.metadata -> 'grading' ->> 'programming_pending',
      '^[0-9]+$'
    ))[1]::INT,
    0
  ) AS programming_pending
) derived
CROSS JOIN LATERAL (
  SELECT count(*)::INT AS pending_count
  FROM public.answers a
  JOIN public.questions q ON q.id = a.question_id
  WHERE a.attempt_id = att.id
    AND q.question_type = 'coding'
    AND COALESCE(a.evaluation_status, 'Pending Evaluation') = 'Pending Evaluation'
) actual
WHERE att.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated')
  AND derived.programming_pending <> actual.pending_count;

-- ====================================================================
-- END OF MIGRATION 6
--
-- Row-modifying statements in this file: exactly one, the max_score
-- backfill in Section F, which only fills a NULL column on attempts that
-- are already finalized. It deletes nothing.
-- ====================================================================
