-- ====================================================================
-- QUIZEAZZ — MIGRATION 9: C++-ONLY PROGRAMMING + VERDICT-WRITE FIX
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..8. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   QuizEazz V1 supports exactly one programming language: C++ (canonical
--   id 'cpp', Judge0 language_id 54). Two defects made multi-language
--   execution possible:
--
--   D1. quizeazz_apply_judge0_verdict stored the current evaluation_status
--       in a composite RECORD variable and compared it directly to a TEXT
--       literal, which raises PostgreSQL 42883 (operator does not exist:
--       record <> text). No programming verdict of any outcome could ever
--       be written (applied:false forever).
--
--   D2. answers.language came from a free browser selector defaulting to
--       'python' (with a database COALESCE fallback to 'python'), so C++
--       source could be executed by the Python interpreter.
--
-- WHAT THIS FILE DOES (function-level only)
--   1. Recreates quizeazz_apply_judge0_verdict with the status held in a
--      scalar TEXT variable. Return type is unchanged, so CREATE OR REPLACE
--      is valid here (no DROP is required; DROP is only required when a
--      RETURNS TABLE shape changes, as in migration 8).
--   2. Recreates save_exam_answer so the coding branch always stores
--      language = 'cpp', ignoring any browser-supplied value. A malicious
--      client cannot persist python/java/c/any through the authoritative
--      backend. Signature unchanged.
--   3. Recreates teacher_create_question so a coding question whose language
--      IS DISTINCT FROM 'cpp' is rejected. Non-coding questions are
--      untouched. Signature unchanged.
--   4. Recreates teacher_set_exam_paper so an exam cannot gain a coding
--      question whose language IS DISTINCT FROM 'cpp'. Signature unchanged.
--
-- SCOPE
--   No table is created, altered or dropped. No column is added. No RLS
--   policy is changed. No data is deleted or converted. Existing non-C++
--   questions are left exactly as they are (see "Existing data" below);
--   they simply cannot be newly published or added to an exam.
--   CREATE OR REPLACE preserves every existing GRANT/REVOKE, so no ACL is
--   restated here.
--
-- EXISTING DATA
--   This migration does NOT convert python/java/c/any questions to C++:
--   their starter code and reference solutions are language-specific.
--   If legacy non-C++ programming questions must disappear from the bank,
--   archive them explicitly (a separate, approved data operation).
--
-- IDEMPOTENT: safe to run more than once (CREATE OR REPLACE + no writes
-- outside the guarded RPC bodies themselves).
-- ====================================================================


-- ====================================================================
-- SECTION A — D1: verdict writer uses a scalar status (was RECORD)
-- --------------------------------------------------------------------
-- Identical to migration 7 except the verdict-status holder is a scalar
-- TEXT variable (v_current_status) instead of a composite RECORD, with the
-- three references updated. Everything else is byte-identical.
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

  SELECT a.evaluation_status INTO v_current_status
  FROM public.answers a
  WHERE a.id = p_answer_id;

  IF v_current_status IS NULL THEN
    RAISE EXCEPTION 'Answer % does not exist.', p_answer_id;
  END IF;

  -- One verdict per answer. Never overwrite a stored verdict.
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
-- SECTION B — save_exam_answer forces language = 'cpp' for coding
-- --------------------------------------------------------------------
-- Identical to migration 6 except the coding branch stores the literal
-- 'cpp' instead of COALESCE(p_language, 'python'). The p_language
-- parameter is kept (signature stability) but is ignored for coding
-- answers, so no browser value — honest or malicious — can persist a
-- non-C++ programming language. MCQ branch untouched.
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
  --
  -- C++-ONLY (QuizEazz V1): the coding branch stores the literal 'cpp'.
  -- p_language is deliberately ignored so a malicious browser cannot
  -- persist python/java/c/any.
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
      'cpp',
      now()
    )
    ON CONFLICT (attempt_id, question_id) DO UPDATE
    SET
      code = EXCLUDED.code,
      language = 'cpp',
      saved_at = now();
  END IF;

  RETURN TRUE;
END;
$$;


-- ====================================================================
-- SECTION C — teacher_create_question rejects non-C++ coding questions
-- --------------------------------------------------------------------
-- Identical to migration_exam_management_integrity.sql except for the new
-- C++-only guard after the question-type check. Non-coding questions are
-- untouched. Existing rows are not modified.
-- ====================================================================
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

  -- C++-ONLY (QuizEazz V1): a programming question is publishable only as
  -- language 'cpp'. python/java/c/any/NULL are rejected here, at the
  -- authoritative backend boundary, so no frontend can bypass the rule.
  IF p_question_type = 'coding' AND p_language IS DISTINCT FROM 'cpp' THEN
    RAISE EXCEPTION 'Programming questions must use language ''cpp'' (C++ only).';
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
-- SECTION D — teacher_set_exam_paper rejects non-C++ coding questions
-- --------------------------------------------------------------------
-- Identical to migration_exam_management_integrity.sql except for the new
-- C++-only membership guard after the marks check. Existing papers are not
-- modified; only the incoming desired set is validated.
-- ====================================================================
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
  v_bad_lang TEXT;
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

  -- C++-ONLY (QuizEazz V1): an exam paper cannot gain a programming
  -- question whose language IS DISTINCT FROM 'cpp'. This rejects
  -- python/java/c/any/NULL at the authoritative backend boundary, so no
  -- frontend can bypass the rule. Existing papers are untouched.
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_items) AS elem
    JOIN public.questions q ON q.id = (elem->>'question_id')::UUID
    WHERE q.question_type = 'coding'
      AND q.language IS DISTINCT FROM 'cpp'
  ) THEN
    SELECT string_agg(DISTINCT q.id::TEXT, ', ')
    INTO v_bad_lang
    FROM jsonb_array_elements(v_items) AS elem
    JOIN public.questions q ON q.id = (elem->>'question_id')::UUID
    WHERE q.question_type = 'coding'
      AND q.language IS DISTINCT FROM 'cpp';
    RAISE EXCEPTION 'Only C++ (language ''cpp'') programming questions may be added to an exam. Rejected: %', v_bad_lang;
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


-- ====================================================================
-- SECTION E — VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- V1. quizeazz_apply_judge0_verdict stores status in TEXT, not RECORD.
--     Expect: proargnames contains v_current_status, and the function
--     applies Correct / Wrong Answer / Runtime Error without 42883.
--     (Exercise against a real pending answer; never fabricate one.)
--
-- V2. save_exam_answer forces 'cpp':
--     the function body contains "language = 'cpp'" and no
--     "COALESCE(p_language" fallback remains.
--
-- V3. teacher_create_question rejects non-cpp coding language:
--     CALL with p_language := 'python' must raise
--     "Programming questions must use language 'cpp'".
--
-- V4. teacher_set_exam_paper rejects papers containing a non-cpp coding
--     question: must raise "Only C++ (language 'cpp')...".
--
-- V5. ACLs unchanged (CREATE OR REPLACE preserves grants):
-- SELECT p.proname,
--        has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth_exec,
--        has_function_privilege('service_role', p.oid, 'EXECUTE') AS svc_exec
-- FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'public'
--   AND p.proname IN ('quizeazz_apply_judge0_verdict', 'save_exam_answer',
--                     'teacher_create_question', 'teacher_set_exam_paper');
-- ====================================================================

-- ====================================================================
-- END OF MIGRATION 9
-- ====================================================================
