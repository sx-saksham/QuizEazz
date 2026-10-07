-- ====================================================================
-- QUIZEAZZ — PHASE 3 SECURE STUDENT EXAMINATION ARCHITECTURE MIGRATION
-- Authoritative RPCs for Attempt Management, Answer Persistence & Security
-- Hardened with Concurrency Safety, Anti-Tamper Triggers & Role Defense
-- ====================================================================

-- 1. SECURITY EVENTS TABLE DEFINITION (IF NOT PRESENT)
CREATE TABLE IF NOT EXISTS public.security_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  attempt_id UUID REFERENCES public.exam_attempts(id) ON DELETE CASCADE,
  student_id UUID REFERENCES public.profiles(id) ON DELETE CASCADE,
  exam_id UUID REFERENCES public.exams(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  details JSONB DEFAULT '{}'::jsonb,
  question_id UUID REFERENCES public.questions(id) ON DELETE SET NULL,
  event_timestamp TIMESTAMPTZ DEFAULT now()
);

-- Ensure RLS is active on security_events
ALTER TABLE public.security_events ENABLE ROW LEVEL SECURITY;

-- Candidates cannot directly select or insert arbitrary security events
DROP POLICY IF EXISTS "Teacher security events access" ON public.security_events;
CREATE POLICY "Teacher security events access"
  ON public.security_events
  FOR ALL
  TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.profiles
      WHERE id = auth.uid() AND role IN ('teacher', 'admin')
    )
  );

-- 2. PREVENT PROFILE ROLE ESCALATION
-- Blocks non-admin callers from modifying user roles or setting elevated roles upon registration.
CREATE OR REPLACE FUNCTION public.protect_profile_role_escalation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
  v_is_trusted BOOLEAN;
BEGIN
  v_is_trusted := (current_setting('quizeazz.authoritative_context', true) = 'true');

  IF v_is_trusted THEN
    RETURN NEW;
  END IF;

  v_caller_id := auth.uid();

  -- If operation is by service_role (auth.uid() is null in backend scripts/seed)
  IF v_caller_id IS NULL THEN
    RETURN NEW;
  END IF;

  -- On INSERT: If a non-admin tries to insert a row with role <> 'student'
  IF TG_OP = 'INSERT' THEN
    IF NEW.role IS NOT NULL AND NEW.role <> 'student' THEN
      SELECT role INTO v_caller_role FROM public.profiles WHERE id = v_caller_id;
      IF v_caller_role <> 'admin' OR v_caller_role IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Only platform administrators can assign non-student roles.';
      END IF;
    END IF;
    IF NEW.role IS NULL THEN
      NEW.role := 'student';
    END IF;
  END IF;

  -- On UPDATE: If a non-admin tries to change the role
  IF TG_OP = 'UPDATE' THEN
    IF NEW.role IS DISTINCT FROM OLD.role THEN
      SELECT role INTO v_caller_role FROM public.profiles WHERE id = v_caller_id;
      IF v_caller_role <> 'admin' OR v_caller_role IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: You do not have permission to modify user roles.';
      END IF;
    END IF;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_profile_role ON public.profiles;
CREATE TRIGGER trg_protect_profile_role
BEFORE INSERT OR UPDATE ON public.profiles
FOR EACH ROW
EXECUTE FUNCTION public.protect_profile_role_escalation();

-- 3. SAFE DEDUPLICATION AND UNIQUE CONSTRAINTS (RACE CONDITION DEFENSE)
DO $$
DECLARE
  v_dup_count INT;
BEGIN
  -- 1. Deduplicate exam_attempts if historical duplicates exist
  SELECT COUNT(*) INTO v_dup_count
  FROM (
    SELECT exam_id, student_id
    FROM public.exam_attempts
    GROUP BY exam_id, student_id
    HAVING COUNT(*) > 1
  ) dups;

  IF v_dup_count > 0 THEN
    WITH ranked_attempts AS (
      SELECT id,
             ROW_NUMBER() OVER (
               PARTITION BY exam_id, student_id 
               ORDER BY 
                 CASE status 
                   WHEN 'evaluated' THEN 1 
                   WHEN 'submitted' THEN 2 
                   WHEN 'timed_out' THEN 3 
                   ELSE 4 
                 END,
                 score DESC NULLS LAST,
                 created_at DESC
             ) AS rn
      FROM public.exam_attempts
    )
    DELETE FROM public.exam_attempts
    WHERE id IN (
      SELECT id FROM ranked_attempts WHERE rn > 1
    );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint 
    WHERE conname = 'uq_exam_attempts_exam_student'
  ) THEN
    ALTER TABLE public.exam_attempts 
    ADD CONSTRAINT uq_exam_attempts_exam_student UNIQUE (exam_id, student_id);
  END IF;

  -- 2. Deduplicate answers if historical duplicates exist
  SELECT COUNT(*) INTO v_dup_count
  FROM (
    SELECT attempt_id, question_id
    FROM public.answers
    GROUP BY attempt_id, question_id
    HAVING COUNT(*) > 1
  ) dups;

  IF v_dup_count > 0 THEN
    WITH ranked_answers AS (
      SELECT id,
             ROW_NUMBER() OVER (
               PARTITION BY attempt_id, question_id 
               ORDER BY COALESCE(updated_at, now()) DESC, id DESC
             ) AS rn
      FROM public.answers
    )
    DELETE FROM public.answers
    WHERE id IN (
      SELECT id FROM ranked_answers WHERE rn > 1
    );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint 
    WHERE conname = 'uq_answers_attempt_question'
  ) THEN
    ALTER TABLE public.answers 
    ADD CONSTRAINT uq_answers_attempt_question UNIQUE (attempt_id, question_id);
  END IF;
END $$;

-- 4. HARDENED ANSWERS WRITE & DELETE PROTECTION TRIGGER
CREATE OR REPLACE FUNCTION public.check_answer_write_eligibility()
RETURNS TRIGGER 
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_attempt RECORD;
  v_duration INT;
  v_deadline TIMESTAMPTZ;
  v_user_role TEXT;
  v_is_trusted BOOLEAN;
BEGIN
  v_is_trusted := (current_setting('quizeazz.authoritative_context', true) = 'true');
  SELECT role INTO v_user_role FROM public.profiles WHERE id = auth.uid();

  -- 0. Guard against direct candidate DELETE
  IF TG_OP = 'DELETE' THEN
    IF NOT v_is_trusted THEN
      IF v_user_role = 'student' OR v_user_role IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Candidates cannot delete examination answers.';
      END IF;
    END IF;
    RETURN OLD;
  END IF;

  -- 1. Fetch attempt details and associated exam duration
  SELECT ea.status, ea.started_at, ea.is_paused, ea.exam_id, ea.student_id, e.duration_minutes
  INTO v_attempt
  FROM public.exam_attempts ea
  JOIN public.exams e ON e.id = ea.exam_id
  WHERE ea.id = NEW.attempt_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Associated examination attempt % does not exist.', NEW.attempt_id;
  END IF;

  -- 2. Verify caller ownership
  IF auth.uid() IS NOT NULL AND v_attempt.student_id <> auth.uid() THEN
    IF v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL THEN
      RAISE EXCEPTION 'Access denied: You cannot write answers to an attempt belonging to another candidate.';
    END IF;
  END IF;

  -- 3. Verify question belongs to this exam
  IF NOT EXISTS (
    SELECT 1 
    FROM public.exam_questions 
    WHERE exam_id = v_attempt.exam_id 
      AND question_id = NEW.question_id
  ) THEN
    RAISE EXCEPTION 'Question % is not part of examination %.', NEW.question_id, v_attempt.exam_id;
  END IF;

  -- 4. On UPDATE: Enforce primary relationship immutability
  IF TG_OP = 'UPDATE' THEN
    IF NEW.attempt_id <> OLD.attempt_id THEN
      RAISE EXCEPTION 'Unauthorized: attempt_id is immutable on answers.';
    END IF;
    IF NEW.question_id <> OLD.question_id THEN
      RAISE EXCEPTION 'Unauthorized: question_id is immutable on answers.';
    END IF;
  END IF;

  -- 5. Student cannot set or modify evaluation fields directly
  IF (v_user_role = 'student' OR v_user_role IS NULL) AND NOT v_is_trusted THEN
    IF TG_OP = 'INSERT' THEN
      IF NEW.is_correct IS NOT NULL OR NEW.marks_awarded IS NOT NULL OR NEW.evaluation_status IS NOT NULL THEN
        RAISE EXCEPTION 'Unauthorized: Candidates cannot supply evaluation marks or status.';
      END IF;
    ELSIF TG_OP = 'UPDATE' THEN
      IF NEW.is_correct IS DISTINCT FROM OLD.is_correct 
         OR NEW.marks_awarded IS DISTINCT FROM OLD.marks_awarded 
         OR NEW.evaluation_status IS DISTINCT FROM OLD.evaluation_status THEN
        RAISE EXCEPTION 'Unauthorized: Candidate clients cannot modify evaluation outcomes.';
      END IF;
    END IF;
  END IF;

  -- 6. Check attempt lifecycle, pause status, and authoritative deadline
  IF NOT v_is_trusted THEN
    IF v_attempt.status <> 'in_progress' THEN
      RAISE EXCEPTION 'Write rejected: attempt status is %, not in_progress.', v_attempt.status;
    END IF;

    IF v_attempt.is_paused = TRUE THEN
      RAISE EXCEPTION 'Write rejected: examination session is currently PAUSED.';
    END IF;

    v_duration := COALESCE(v_attempt.duration_minutes, 60);
    v_deadline := v_attempt.started_at + (v_duration * INTERVAL '1 minute') + INTERVAL '15 seconds';
    
    IF now() > v_deadline THEN
      RAISE EXCEPTION 'Write rejected: authoritative examination deadline has expired.';
    END IF;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_check_answer_eligibility ON public.answers;
CREATE TRIGGER trg_check_answer_eligibility
BEFORE INSERT OR UPDATE OR DELETE ON public.answers
FOR EACH ROW
EXECUTE FUNCTION public.check_answer_write_eligibility();

-- 5. HARDENED ATTEMPT MUTATION TRIGGER (INSERT, UPDATE, DELETE)
CREATE OR REPLACE FUNCTION public.protect_attempt_mutation()
RETURNS TRIGGER 
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_role TEXT;
  v_is_trusted BOOLEAN;
BEGIN
  v_is_trusted := (current_setting('quizeazz.authoritative_context', true) = 'true');
  SELECT role INTO v_user_role FROM public.profiles WHERE id = auth.uid();

  -- 1. Guard against direct candidate INSERT
  IF TG_OP = 'INSERT' THEN
    IF NOT v_is_trusted THEN
      IF v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL OR v_user_role = 'student' THEN
        RAISE EXCEPTION 'Unauthorized: Direct candidate creation of examination attempts is prohibited. Attempts must be initialized via authoritative platform RPCs.';
      END IF;
    END IF;
    RETURN NEW;
  END IF;

  -- 2. Guard against direct candidate DELETE
  IF TG_OP = 'DELETE' THEN
    IF NOT v_is_trusted THEN
      IF v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Candidates cannot delete examination attempts.';
      END IF;
    END IF;
    RETURN OLD;
  END IF;

  -- 3. Guard against direct candidate UPDATE
  IF TG_OP = 'UPDATE' THEN
    IF v_user_role = 'student' OR v_user_role IS NULL THEN
      IF NOT v_is_trusted THEN
        RAISE EXCEPTION 'Unauthorized: Direct candidate modifications to examination attempts are prohibited. All transitions must occur via authoritative platform RPCs.';
      END IF;
    END IF;

    IF NEW.student_id <> OLD.student_id THEN
      RAISE EXCEPTION 'Unauthorized: student_id on attempt is immutable.';
    END IF;
    IF NEW.exam_id <> OLD.exam_id THEN
      RAISE EXCEPTION 'Unauthorized: exam_id on attempt is immutable.';
    END IF;
    IF NEW.started_at <> OLD.started_at THEN
      RAISE EXCEPTION 'Unauthorized: started_at timestamp is immutable.';
    END IF;

    IF OLD.status = 'in_progress' AND NEW.status NOT IN ('in_progress', 'submitted', 'timed_out') THEN
      RAISE EXCEPTION 'Invalid attempt transition from in_progress to %.', NEW.status;
    END IF;
    IF OLD.status = 'submitted' AND NEW.status NOT IN ('submitted', 'evaluating') THEN
      RAISE EXCEPTION 'Invalid attempt transition from submitted to %.', NEW.status;
    END IF;
    IF OLD.status = 'timed_out' AND NEW.status NOT IN ('timed_out', 'evaluating') THEN
      RAISE EXCEPTION 'Invalid attempt transition from timed_out to %.', NEW.status;
    END IF;
    IF OLD.status = 'evaluating' AND NEW.status NOT IN ('evaluating', 'evaluated') THEN
      RAISE EXCEPTION 'Invalid attempt transition from evaluating to %.', NEW.status;
    END IF;
    IF OLD.status = 'evaluated' AND NEW.status NOT IN ('evaluated', 'evaluating') THEN
      RAISE EXCEPTION 'Evaluated attempts are frozen and cannot transition back to %.', NEW.status;
    END IF;

    RETURN NEW;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_attempt_mutation ON public.exam_attempts;
CREATE TRIGGER trg_protect_attempt_mutation
BEFORE INSERT OR UPDATE OR DELETE ON public.exam_attempts
FOR EACH ROW
EXECUTE FUNCTION public.protect_attempt_mutation();

-- 6. AUTHORITATIVE EXAM ATTEMPT SUBMISSION RPC (WITH FOR UPDATE ROW LOCKING)
CREATE OR REPLACE FUNCTION public.submit_exam_attempt(p_attempt_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_attempt RECORD;
  v_exam RECORD;
  v_user_id UUID;
  v_user_role TEXT;
  v_duration INT;
  v_deadline TIMESTAMPTZ;
  v_is_late BOOLEAN := FALSE;
  v_final_status TEXT;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required to submit attempt.';
  END IF;

  -- Row-level lock to prevent concurrent duplicate submissions
  SELECT id, student_id, status, started_at, exam_id
  INTO v_attempt
  FROM public.exam_attempts
  WHERE id = p_attempt_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % does not exist.', p_attempt_id;
  END IF;

  SELECT role INTO v_user_role FROM public.profiles WHERE id = v_user_id;

  -- Caller must be student owner or faculty/admin
  IF v_attempt.student_id <> v_user_id AND (v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL) THEN
    RAISE EXCEPTION 'Access denied: You cannot submit an attempt belonging to another candidate.';
  END IF;

  -- Idempotency check: if attempt is already submitted, timed_out, evaluating, or evaluated, return status gracefully
  IF v_attempt.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated') THEN
    RETURN jsonb_build_object(
      'attempt_id', p_attempt_id,
      'status', v_attempt.status,
      'submitted_at', now(),
      'timed_out', (v_attempt.status = 'timed_out'),
      'message', 'Examination already submitted.'
    );
  END IF;

  -- Attempt must be in_progress
  IF v_attempt.status NOT IN ('in_progress') THEN
    RAISE EXCEPTION 'Attempt cannot be submitted because current status is %.', v_attempt.status;
  END IF;

  -- Authoritative deadline verification
  SELECT duration_minutes INTO v_exam FROM public.exams WHERE id = v_attempt.exam_id;
  v_duration := COALESCE(v_exam.duration_minutes, 60);
  v_deadline := v_attempt.started_at + (v_duration * INTERVAL '1 minute') + INTERVAL '15 seconds';

  IF now() > v_deadline THEN
    v_is_late := TRUE;
    v_final_status := 'timed_out';
  ELSE
    v_final_status := 'submitted';
  END IF;

  -- Set authoritative execution context for trigger
  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  -- Authoritative transition
  UPDATE public.exam_attempts
  SET 
    status = v_final_status,
    submitted_at = now(),
    is_paused = FALSE,
    paused_at = NULL,
    pause_reason = CASE WHEN v_is_late THEN 'Attempt exceeded allotted duration' ELSE NULL END
  WHERE id = p_attempt_id;

  RETURN jsonb_build_object(
    'attempt_id', p_attempt_id,
    'status', v_final_status,
    'submitted_at', now(),
    'timed_out', v_is_late,
    'message', CASE 
      WHEN v_is_late THEN 'Submission occurred after authoritative deadline. Attempt marked as timed_out.'
      ELSE 'Examination successfully submitted.'
    END
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.submit_exam_attempt(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.submit_exam_attempt(UUID) TO authenticated;

-- 7. TRUSTED SERVER-SIDE MCQ GRADING FUNCTION (RPC WITH ROW LOCKING & IDEMPOTENCY)
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
  v_mcq_score INT := 0;
  v_updated_count INT := 0;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  -- 1. Fetch attempt details with row-level lock
  SELECT id, student_id, status, exam_id, is_paused, score
  INTO v_attempt
  FROM public.exam_attempts
  WHERE id = p_attempt_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % not found.', p_attempt_id;
  END IF;

  SELECT role INTO v_user_role FROM public.profiles WHERE id = v_user_id;

  -- 2. Authorization: Caller must be the attempt owner or faculty/admin
  IF v_attempt.student_id <> v_user_id THEN
    IF v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL THEN
      RAISE EXCEPTION 'Access denied: Cannot grade another candidate attempt.';
    END IF;
  END IF;

  -- 3. Strict Lifecycle & Idempotency Enforcement:
  -- If already evaluated and caller is student, return existing score idempotently
  IF v_attempt.status = 'evaluated' THEN
    IF v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL THEN
      RETURN jsonb_build_object(
        'attempt_id', p_attempt_id,
        'status', 'evaluated',
        'mcq_score', COALESCE(v_attempt.score, 0),
        'message', 'Attempt is already evaluated.'
      );
    END IF;
  ELSIF v_attempt.status = 'in_progress' OR v_attempt.is_paused = TRUE THEN
    RAISE EXCEPTION 'Lifecycle violation: Attempt % is still in_progress or paused. Final submission is required prior to evaluation.', p_attempt_id;
  ELSIF v_attempt.status NOT IN ('submitted', 'timed_out', 'evaluating') THEN
    RAISE EXCEPTION 'Attempt % is in status % and cannot be evaluated.', p_attempt_id, v_attempt.status;
  END IF;

  -- Set authoritative execution context for triggers
  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  -- Set status: submitted/timed_out -> evaluating
  UPDATE public.exam_attempts
  SET status = 'evaluating'
  WHERE id = p_attempt_id;

  -- 4. Objective server-side evaluation:
  WITH mcq_eval AS (
    SELECT 
      a.id AS answer_id,
      correct_data.correct_id,
      eq.marks AS question_marks,
      (a.selected_option_id IS NOT NULL AND a.selected_option_id = correct_data.correct_id) AS is_correct_answer,
      (a.selected_option_id IS NULL) AS is_unanswered
    FROM public.answers a
    JOIN public.exam_attempts ea ON ea.id = a.attempt_id
    JOIN public.questions q ON q.id = a.question_id
    LEFT JOIN public.exam_questions eq ON eq.exam_id = ea.exam_id AND eq.question_id = a.question_id
    LEFT JOIN LATERAL (
      SELECT elem->>'id' AS correct_id
      FROM jsonb_array_elements(q.options) elem
      WHERE COALESCE((elem->>'is_correct')::boolean, false) = true
      LIMIT 1
    ) correct_data ON TRUE
    WHERE a.attempt_id = p_attempt_id
      AND q.question_type = 'mcq'
  ),
  updated_answers AS (
    UPDATE public.answers a
    SET 
      is_correct = me.is_correct_answer,
      marks_awarded = CASE 
        WHEN me.is_correct_answer THEN COALESCE(me.question_marks, 0)
        ELSE 0
      END,
      evaluation_status = CASE 
        WHEN me.is_correct_answer THEN 'Correct'
        WHEN me.is_unanswered THEN 'Not Attempted'
        ELSE 'Wrong Answer'
      END
    FROM mcq_eval me
    WHERE a.id = me.answer_id
    RETURNING a.marks_awarded
  )
  SELECT COALESCE(SUM(marks_awarded), 0), COUNT(*)
  INTO v_mcq_score, v_updated_count
  FROM updated_answers;

  -- 5. Calculate total score and transition status to evaluated
  UPDATE public.exam_attempts
  SET 
    score = (
      SELECT COALESCE(SUM(marks_awarded), 0)
      FROM public.answers
      WHERE attempt_id = p_attempt_id
    ),
    status = 'evaluated'
  WHERE id = p_attempt_id;

  RETURN jsonb_build_object(
    'attempt_id', p_attempt_id,
    'status', 'evaluated',
    'mcq_score', v_mcq_score,
    'graded_questions', v_updated_count
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.grade_mcq_attempt(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.grade_mcq_attempt(UUID) TO authenticated;

-- 8. SECURE EXAM ATTEMPT INITIALIZATION & RETRIEVAL (RPC WITH ATOMIC ON CONFLICT)
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
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required to start or load an examination attempt.';
  END IF;

  SELECT role INTO v_user_role FROM public.profiles WHERE id = v_user_id;

  -- Verify exam existence
  SELECT id, title, description, duration_minutes, status, total_marks, starts_at, ends_at
  INTO v_exam
  FROM public.exams
  WHERE id = p_exam_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  -- Verify candidate assignment & exam availability
  IF v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL THEN
    IF NOT EXISTS (
      SELECT 1 
      FROM public.exam_assignments ea
      WHERE ea.exam_id = p_exam_id 
        AND ea.student_id = v_user_id
    ) THEN
      RAISE EXCEPTION 'Access denied: You are not assigned to this examination.';
    END IF;

    IF v_exam.status NOT IN ('published', 'in_progress', 'active', 'scheduled') THEN
      RAISE EXCEPTION 'Examination is not currently open for taking (status: %).', v_exam.status;
    END IF;
  END IF;

  -- Authoritatively perform atomic insertion if no attempt exists
  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  INSERT INTO public.exam_attempts (
    exam_id,
    student_id,
    status,
    started_at
  ) VALUES (
    p_exam_id,
    v_user_id,
    'in_progress',
    now()
  )
  ON CONFLICT (exam_id, student_id) DO NOTHING;

  -- Authoritatively fetch the attempt (either newly inserted or existing)
  SELECT id, exam_id, student_id, status, started_at, submitted_at, is_paused, paused_at, pause_reason, created_at, metadata
  INTO v_attempt
  FROM public.exam_attempts
  WHERE exam_id = p_exam_id AND student_id = v_user_id;

  -- Return sanitized exam and attempt data
  RETURN jsonb_build_object(
    'exam', jsonb_build_object(
      'id', v_exam.id,
      'title', v_exam.title,
      'duration_minutes', COALESCE(v_exam.duration_minutes, 60),
      'status', v_exam.status,
      'total_marks', v_exam.total_marks
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

REVOKE EXECUTE ON FUNCTION public.start_or_get_exam_attempt(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.start_or_get_exam_attempt(UUID) TO authenticated;

-- 9. SECURE STUDENT ANSWER RETRIEVAL (RPC)
CREATE OR REPLACE FUNCTION public.get_student_exam_answers(p_attempt_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_user_role TEXT;
  v_attempt RECORD;
  v_result JSONB;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT student_id, status INTO v_attempt 
  FROM public.exam_attempts 
  WHERE id = p_attempt_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % not found.', p_attempt_id;
  END IF;

  SELECT role INTO v_user_role FROM public.profiles WHERE id = v_user_id;

  IF v_attempt.student_id <> v_user_id AND (v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL) THEN
    RAISE EXCEPTION 'Access denied: You cannot view answers belonging to another candidate.';
  END IF;

  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'question_id', a.question_id,
      'code', a.code,
      'language', a.language,
      'selected_option_id', a.selected_option_id
    )
  ), '[]'::jsonb)
  INTO v_result
  FROM public.answers a
  WHERE a.attempt_id = p_attempt_id;

  RETURN v_result;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_student_exam_answers(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_student_exam_answers(UUID) TO authenticated;

-- 10. SECURE ANSWER PERSISTENCE (RPC WITH ATOMIC UPSERT)
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
  -- Candidate clients NEVER supply correctness or marks
  -- Uses atomic PostgreSQL upsert to prevent duplicate answer races
  IF v_q_type = 'mcq' THEN
    INSERT INTO public.answers (
      attempt_id,
      question_id,
      selected_option_id,
      code,
      language,
      updated_at
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
      updated_at = now();
  ELSE
    INSERT INTO public.answers (
      attempt_id,
      question_id,
      selected_option_id,
      code,
      language,
      updated_at
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
      updated_at = now();
  END IF;

  RETURN TRUE;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.save_exam_answer(UUID, UUID, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.save_exam_answer(UUID, UUID, TEXT, TEXT) TO authenticated;

-- 11. SECURE SECURITY EVENT LOGGER (RPC)
CREATE OR REPLACE FUNCTION public.record_security_event(
  p_attempt_id UUID,
  p_exam_id UUID,
  p_event_type TEXT,
  p_question_id UUID DEFAULT NULL,
  p_details JSONB DEFAULT '{}'::jsonb
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_attempt RECORD;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT student_id, exam_id INTO v_attempt 
  FROM public.exam_attempts 
  WHERE id = p_attempt_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % not found.', p_attempt_id;
  END IF;

  IF v_attempt.student_id <> v_user_id THEN
    RAISE EXCEPTION 'Access denied: Cannot record security event for another candidate.';
  END IF;

  IF v_attempt.exam_id <> p_exam_id THEN
    RAISE EXCEPTION 'Attempt % does not belong to examination %.', p_attempt_id, p_exam_id;
  END IF;

  INSERT INTO public.security_events (
    attempt_id,
    student_id,
    exam_id,
    event_type,
    details,
    question_id,
    event_timestamp
  ) VALUES (
    p_attempt_id,
    v_user_id,
    p_exam_id,
    p_event_type,
    COALESCE(p_details, '{}'::jsonb),
    p_question_id,
    now()
  );

  RETURN TRUE;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.record_security_event(UUID, UUID, TEXT, UUID, JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.record_security_event(UUID, UUID, TEXT, UUID, JSONB) TO authenticated;

-- 12. CANDIDATE & INVIGILATOR EXAM PAUSE AND RESUME FUNCTIONS (RPC)
CREATE OR REPLACE FUNCTION public.pause_exam_attempt(p_attempt_id UUID, p_reason TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_attempt RECORD;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT student_id, status INTO v_attempt FROM public.exam_attempts WHERE id = p_attempt_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % not found.', p_attempt_id;
  END IF;

  IF v_attempt.student_id <> v_user_id THEN
    RAISE EXCEPTION 'Access denied: Cannot pause another candidate attempt.';
  END IF;

  IF v_attempt.status <> 'in_progress' THEN
    RAISE EXCEPTION 'Cannot pause attempt in status %.', v_attempt.status;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.exam_attempts
  SET 
    is_paused = TRUE,
    paused_at = now(),
    pause_reason = COALESCE(p_reason, 'Focus interruption detected')
  WHERE id = p_attempt_id;

  RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION public.resume_exam_attempt(p_attempt_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_user_role TEXT;
  v_attempt RECORD;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT student_id, status, is_paused INTO v_attempt FROM public.exam_attempts WHERE id = p_attempt_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Attempt % not found.', p_attempt_id;
  END IF;

  SELECT role INTO v_user_role FROM public.profiles WHERE id = v_user_id;

  -- Candidate can resume their own attempt; teacher/admin can resume any candidate attempt
  IF v_attempt.student_id <> v_user_id AND (v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL) THEN
    RAISE EXCEPTION 'Access denied: You cannot resume an attempt belonging to another candidate.';
  END IF;

  IF v_attempt.status <> 'in_progress' THEN
    RAISE EXCEPTION 'Cannot resume attempt in status %.', v_attempt.status;
  END IF;

  IF v_attempt.is_paused <> TRUE THEN
    RAISE EXCEPTION 'Attempt is not currently paused.';
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.exam_attempts
  SET 
    is_paused = FALSE,
    paused_at = NULL,
    pause_reason = NULL
  WHERE id = p_attempt_id;

  RETURN TRUE;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.pause_exam_attempt(UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.pause_exam_attempt(UUID, TEXT) TO authenticated;

REVOKE EXECUTE ON FUNCTION public.resume_exam_attempt(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.resume_exam_attempt(UUID) TO authenticated;

-- 13. STUDENT EXAM OVERVIEW (RPC)
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
      'total_marks', COALESCE(e.total_marks, (
        SELECT COALESCE(sum(eq.marks), 0) FROM public.exam_questions eq WHERE eq.exam_id = e.id
      )),
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
    AND e.status IN ('published', 'in_progress', 'active', 'scheduled', 'completed');

  RETURN v_result;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_student_exam_overview() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_student_exam_overview() TO authenticated;

-- 14. SECURE STUDENT RESULTS RETRIEVAL (RPC)
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
      'total_marks', COALESCE(e.total_marks, (
        SELECT COALESCE(sum(eq.marks), 0) FROM public.exam_questions eq WHERE eq.exam_id = att.exam_id
      ), 20),
      'status', att.status
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

REVOKE EXECUTE ON FUNCTION public.get_student_results() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_student_results() TO authenticated;

-- 15. SECURE STUDENT ATTEMPT REVIEW REPORT (RPC)
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
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT role INTO v_user_role FROM public.profiles WHERE id = v_user_id;

  SELECT id, exam_id, student_id, status, score, started_at, submitted_at, created_at
  INTO v_attempt
  FROM public.exam_attempts
  WHERE id = p_attempt_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination attempt % does not exist.', p_attempt_id;
  END IF;

  -- Caller must be student owner or faculty/admin
  IF v_attempt.student_id <> v_user_id AND (v_user_role NOT IN ('teacher', 'admin') OR v_user_role IS NULL) THEN
    RAISE EXCEPTION 'Access denied: You cannot view results for an attempt belonging to another candidate.';
  END IF;

  -- Attempt must not be in_progress
  IF v_attempt.status = 'in_progress' THEN
    RAISE EXCEPTION 'Review unavailable: Examination is still in progress.';
  END IF;

  -- Fetch exam details
  SELECT id, title, total_marks
  INTO v_exam
  FROM public.exams
  WHERE id = v_attempt.exam_id;

  -- If attempt is not yet evaluated, return empty evaluations list with pending status
  IF v_attempt.status <> 'evaluated' THEN
    RETURN jsonb_build_object(
      'attempt_id', v_attempt.id,
      'exam_id', v_attempt.exam_id,
      'exam_title', COALESCE(v_exam.title, 'Examination'),
      'score', COALESCE(v_attempt.score, 0),
      'total_marks', COALESCE(v_exam.total_marks, 20),
      'status', v_attempt.status,
      'submitted_at', COALESCE(v_attempt.submitted_at, v_attempt.created_at),
      'evaluations', '[]'::jsonb
    );
  END IF;

  -- Build sanitized evaluations for questions assigned to this specific exam
  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'question_id', q.id,
      'question_title', q.title,
      'question_type', q.question_type,
      'question_text', q.question_text,
      'marks_total', COALESCE(eq.marks, 2),
      'marks_awarded', COALESCE(a.marks_awarded, 0),
      'outcome', CASE 
        WHEN a.evaluation_status IS NOT NULL THEN a.evaluation_status
        WHEN a.selected_option_id IS NULL AND a.code IS NULL THEN 'Not Attempted'
        WHEN a.is_correct = TRUE THEN 'Correct'
        WHEN a.is_correct = FALSE THEN 'Wrong Answer'
        ELSE 'Evaluated'
      END,
      'student_option_id', a.selected_option_id,
      'student_code', a.code,
      'language', COALESCE(a.language, q.language, 'python'),
      -- Sanitize options: ONLY expose array of { id, text }, stripping is_correct
      'options', CASE 
        WHEN q.question_type = 'mcq' AND q.options IS NOT NULL AND jsonb_typeof(q.options) = 'array' THEN (
          SELECT jsonb_agg(jsonb_build_object('id', elem->>'id', 'text', elem->>'text'))
          FROM jsonb_array_elements(q.options) elem
        )
        ELSE NULL
      END,
      'explanation', 'Objective evaluation record saved.'
    )
  ), '[]'::jsonb)
  INTO v_evaluations
  FROM public.exam_questions eq
  JOIN public.questions q ON q.id = eq.question_id
  LEFT JOIN public.answers a ON a.attempt_id = p_attempt_id AND a.question_id = q.id
  WHERE eq.exam_id = v_attempt.exam_id;

  RETURN jsonb_build_object(
    'attempt_id', v_attempt.id,
    'exam_id', v_attempt.exam_id,
    'exam_title', COALESCE(v_exam.title, 'Examination'),
    'score', COALESCE(v_attempt.score, 0),
    'total_marks', COALESCE(v_exam.total_marks, (
      SELECT COALESCE(sum(eq2.marks), 0) FROM public.exam_questions eq2 WHERE eq2.exam_id = v_attempt.exam_id
    ), 20),
    'status', v_attempt.status,
    'submitted_at', COALESCE(v_attempt.submitted_at, v_attempt.created_at),
    'evaluations', v_evaluations
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_student_attempt_review(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_student_attempt_review(UUID) TO authenticated;
