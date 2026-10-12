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
