-- ====================================================================
-- QUIZEAZZ — MIGRATION 11: HIDDEN TEST CASES + SEMANTIC C++ GRADING
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..10. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   Judge0 proves a program compiled and exited; it cannot prove the
--   program is CORRECT. Until now 'Correct' meant "exited 0". This
--   migration adds teacher-authored hidden test cases and the server-side
--   reads the grading Edge Function needs to compare real stdout against
--   expected stdout. AI is not involved anywhere.
--
-- WHAT THIS FILE DOES
--   1. Creates programming_test_cases (question_id, case_order, stdin,
--      expected_stdout, is_hidden, created_at) with a per-question order
--      unique key, a trigger restricting rows to coding questions, and RLS
--      enabled with NO policies (deny-by-default; service_role and
--      SECURITY DEFINER RPCs bypass RLS).
--   2. Adds teacher_set_test_cases / teacher_get_test_cases (faculty-only,
--      authenticated): full transactional replacement + ordered read.
--      is_hidden is forced true; client-supplied flags are ignored.
--   3. Adds quizeazz_judge0_test_cases (service-role only): ordered cases
--      for grading, expected_stdout possibly NULL (unvalidated).
--   4. Adds quizeazz_apply_reference_outputs (service-role only): writes
--      reference-computed expected outputs by case_order.
--   5. Adds quizeazz_question_reference (service-role only): reference
--      solution + language for the reference-validation run.
--   6. Adds quizeazz_caller_is_faculty (authenticated): lets the Edge
--      Function authorize teacher-only actions without touching RLS.
--   7. Extends teacher_set_exam_paper (same signature — CREATE OR REPLACE
--      is legal): a paper cannot gain a coding question with zero
--      validated (non-null expected_stdout) hidden test cases.
--
-- SCOPE
--   No existing table is altered. No data is deleted or converted. No RLS
--   policy is changed. Existing papers/attempts/answers are untouched.
--   CREATE OR REPLACE preserves every existing GRANT/REVOKE.
--
-- IDEMPOTENT: safe to run more than once.
-- ====================================================================


-- ====================================================================
-- SECTION A — hidden test case storage (deny-by-default)
-- ====================================================================

CREATE TABLE IF NOT EXISTS public.programming_test_cases (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  question_id UUID NOT NULL REFERENCES public.questions(id) ON DELETE CASCADE,
  case_order INT NOT NULL CHECK (case_order >= 0),
  stdin TEXT NOT NULL DEFAULT '',
  expected_stdout TEXT,
  is_hidden BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (question_id, case_order)
);

CREATE INDEX IF NOT EXISTS programming_test_cases_question_idx
  ON public.programming_test_cases (question_id, case_order);

-- Test cases may only belong to coding questions. Enforced by trigger so no
-- writer (RPC or otherwise) can attach cases to an MCQ.
CREATE OR REPLACE FUNCTION public.programming_test_cases_coding_only()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
  v_q_type TEXT;
BEGIN
  SELECT q.question_type INTO v_q_type
  FROM public.questions q
  WHERE q.id = NEW.question_id;

  IF v_q_type IS NULL THEN
    RAISE EXCEPTION 'Question % does not exist.', NEW.question_id;
  END IF;

  IF v_q_type <> 'coding' THEN
    RAISE EXCEPTION 'Hidden test cases may only belong to coding questions.';
  END IF;

  -- Hidden in V1, unconditionally. A client flag asking otherwise is ignored.
  NEW.is_hidden := true;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_programming_test_cases_coding_only
  ON public.programming_test_cases;
CREATE TRIGGER trg_programming_test_cases_coding_only
  BEFORE INSERT OR UPDATE ON public.programming_test_cases
  FOR EACH ROW EXECUTE FUNCTION public.programming_test_cases_coding_only();

-- Deny-by-default: RLS on, zero permissive policies. Authenticated roles
-- (including students and teachers) can neither read nor write these rows
-- directly. Service_role bypasses RLS; faculty manage cases through the
-- SECURITY DEFINER RPCs below, which run as the table owner.
ALTER TABLE public.programming_test_cases ENABLE ROW LEVEL SECURITY;


-- ====================================================================
-- SECTION B — teacher test-case management (faculty only)
-- --------------------------------------------------------------------
-- Full replacement per call, inside one transaction: the stored set always
-- matches exactly what the teacher submitted. Order is assigned by position
-- (0-based), never trusted from the client.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.teacher_set_test_cases(
  p_question_id UUID,
  p_cases JSONB DEFAULT '[]'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
  v_q_type TEXT;
  v_count INT := 0;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can manage hidden test cases.';
  END IF;

  SELECT q.question_type INTO v_q_type
  FROM public.questions q
  WHERE q.id = p_question_id;

  IF v_q_type IS NULL THEN
    RAISE EXCEPTION 'Question % does not exist.', p_question_id;
  END IF;

  IF v_q_type <> 'coding' THEN
    RAISE EXCEPTION 'Hidden test cases may only belong to coding questions.';
  END IF;

  IF p_cases IS NULL OR jsonb_typeof(p_cases) <> 'array' THEN
    RAISE EXCEPTION 'Test cases must be a JSON array of {stdin} objects.';
  END IF;

  IF (SELECT count(*) FROM jsonb_array_elements(p_cases)) > 20 THEN
    RAISE EXCEPTION 'A question may hold at most 20 hidden test cases.';
  END IF;

  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(p_cases) AS elem
    WHERE jsonb_typeof(elem) <> 'object'
  ) THEN
    RAISE EXCEPTION 'Every test case must be an object with a stdin string.';
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  DELETE FROM public.programming_test_cases
  WHERE question_id = p_question_id;

  WITH ordered AS (
    SELECT
      (ord - 1)::INT AS case_order,
      COALESCE(elem->>'stdin', '') AS stdin
    FROM jsonb_array_elements(p_cases) WITH ORDINALITY AS t(elem, ord)
  ),
  inserted AS (
    INSERT INTO public.programming_test_cases (question_id, case_order, stdin)
    SELECT p_question_id, case_order, stdin
    FROM ordered
    RETURNING id
  )
  SELECT COUNT(*)::INT INTO v_count FROM inserted;

  RETURN jsonb_build_object(
    'question_id', p_question_id,
    'saved', v_count
  );
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_set_test_cases(UUID, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_set_test_cases(UUID, JSONB) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_set_test_cases(UUID, JSONB) TO authenticated;


CREATE OR REPLACE FUNCTION public.teacher_get_test_cases(
  p_question_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_caller_role TEXT;
  v_result JSONB;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can view hidden test cases.';
  END IF;

  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'id', t.id,
      'case_order', t.case_order,
      'stdin', t.stdin,
      'expected_stdout', t.expected_stdout,
      'validated', (t.expected_stdout IS NOT NULL)
    ) ORDER BY t.case_order
  ), '[]'::jsonb)
  INTO v_result
  FROM public.programming_test_cases t
  WHERE t.question_id = p_question_id;

  RETURN jsonb_build_object(
    'question_id', p_question_id,
    'cases', v_result
  );
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_get_test_cases(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_get_test_cases(UUID) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_get_test_cases(UUID) TO authenticated;


-- ====================================================================
-- SECTION C — service-role grading reads (Edge Function only)
-- ====================================================================
CREATE OR REPLACE FUNCTION public.quizeazz_judge0_test_cases(
  p_question_id UUID
)
RETURNS TABLE (
  case_order INT,
  stdin TEXT,
  expected_stdout TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  RETURN QUERY
  SELECT t.case_order, t.stdin, t.expected_stdout
  FROM public.programming_test_cases t
  WHERE t.question_id = p_question_id
  ORDER BY t.case_order;
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_judge0_test_cases(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_judge0_test_cases(UUID) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_judge0_test_cases(UUID) FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.quizeazz_judge0_test_cases(UUID) TO service_role;


-- Reference solution + language for the server-side validation run.
-- Never exposed to any browser role.
CREATE OR REPLACE FUNCTION public.quizeazz_question_reference(
  p_question_id UUID
)
RETURNS TABLE (
  reference_solution TEXT,
  language TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  RETURN QUERY
  SELECT q.reference_solution, q.language
  FROM public.questions q
  WHERE q.id = p_question_id
    AND q.question_type = 'coding';
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_question_reference(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_question_reference(UUID) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_question_reference(UUID) FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.quizeazz_question_reference(UUID) TO service_role;


-- Writes reference-computed expected outputs by case_order. Only rows whose
-- reference run genuinely succeeded may carry an expected output; the Edge
-- Function omits failed cases, leaving them NULL (unvalidated) so the paper
-- gate keeps blocking publication.
CREATE OR REPLACE FUNCTION public.quizeazz_apply_reference_outputs(
  p_question_id UUID,
  p_outputs JSONB
)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_updated INT := 0;
BEGIN
  IF p_outputs IS NULL OR jsonb_typeof(p_outputs) <> 'array' THEN
    RAISE EXCEPTION 'Reference outputs must be a JSON array of {case_order, expected_stdout} objects.';
  END IF;

  WITH vals AS (
    SELECT
      (elem->>'case_order')::INT AS case_order,
      (elem->>'expected_stdout')::TEXT AS expected_stdout
    FROM jsonb_array_elements(p_outputs) AS elem
  ),
  updated AS (
    UPDATE public.programming_test_cases t
    SET expected_stdout = vals.expected_stdout
    FROM vals
    WHERE t.question_id = p_question_id
      AND t.case_order = vals.case_order
      AND vals.expected_stdout IS NOT NULL
    RETURNING t.id
  )
  SELECT COUNT(*)::INT INTO v_updated FROM updated;

  RETURN v_updated;
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_apply_reference_outputs(UUID, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_apply_reference_outputs(UUID, JSONB) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_apply_reference_outputs(UUID, JSONB) FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.quizeazz_apply_reference_outputs(UUID, JSONB) TO service_role;


-- Lets the Edge Function authorize teacher-only actions (reference
-- validation) for the calling user without touching RLS.
CREATE OR REPLACE FUNCTION public.quizeazz_caller_is_faculty()
RETURNS BOOLEAN
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_role TEXT;
BEGIN
  SELECT p.role INTO v_role
  FROM public.profiles p
  WHERE p.id = auth.uid();

  RETURN v_role IN ('teacher', 'admin');
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_caller_is_faculty() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_caller_is_faculty() FROM anon;
GRANT  EXECUTE ON FUNCTION public.quizeazz_caller_is_faculty() TO authenticated;


-- ====================================================================
-- SECTION D — paper gate: coding questions need validated hidden tests
-- --------------------------------------------------------------------
-- Identical to migration 9 except for the new validated-tests guard after
-- the C++-only guard. Same signature — CREATE OR REPLACE is legal.
-- Existing papers are untouched; only incoming paper sets are validated.
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
  v_bad_tests TEXT;
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

  -- SEMANTIC GRADING (QuizEazz V1): a coding question is gradable only with
  -- at least one VALIDATED hidden test case (expected_stdout IS NOT NULL).
  -- Without this, 'Correct' would mean merely "exited 0". Existing papers
  -- are untouched; only incoming paper sets are validated.
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_items) AS elem
    JOIN public.questions q ON q.id = (elem->>'question_id')::UUID
    WHERE q.question_type = 'coding'
      AND NOT EXISTS (
        SELECT 1 FROM public.programming_test_cases t
        WHERE t.question_id = q.id
          AND t.expected_stdout IS NOT NULL
      )
  ) THEN
    SELECT string_agg(DISTINCT q.id::TEXT, ', ')
    INTO v_bad_tests
    FROM jsonb_array_elements(v_items) AS elem
    JOIN public.questions q ON q.id = (elem->>'question_id')::UUID
    WHERE q.question_type = 'coding'
      AND NOT EXISTS (
        SELECT 1 FROM public.programming_test_cases t
        WHERE t.question_id = q.id
          AND t.expected_stdout IS NOT NULL
      );
    RAISE EXCEPTION 'Coding questions require at least one validated hidden test case before they can join an exam. Unvalidated: %', v_bad_tests;
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
-- E1. Table exists, deny-by-default (no permissive policies):
-- SELECT tablename, rowsecurity FROM pg_tables
-- WHERE schemaname='public' AND tablename='programming_test_cases';
-- SELECT COUNT(*) FROM pg_policies
-- WHERE schemaname='public' AND tablename='programming_test_cases';
-- (expect rowsecurity=true, policy count 0)
--
-- E2. New functions exist with the intended ACLs:
-- SELECT p.proname,
--   has_function_privilege('anon',p.oid,'EXECUTE') AS anon,
--   has_function_privilege('authenticated',p.oid,'EXECUTE') AS auth,
--   has_function_privilege('service_role',p.oid,'EXECUTE') AS svc
-- FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
-- WHERE n.nspname='public' AND p.proname IN ('teacher_set_test_cases',
--   'teacher_get_test_cases','quizeazz_judge0_test_cases',
--   'quizeazz_apply_reference_outputs','quizeazz_question_reference',
--   'quizeazz_caller_is_faculty');
-- (expect teacher_* auth=true/anon=false; quizeazz_* auth=false/svc=true)
--
-- E3. Student RPCs cannot see hidden data: teacher_get_test_cases as a
-- student JWT must raise; get_student_attempt_review output contains no
-- 'expected_stdout', no 'reference_solution', no hidden stdin keys.
-- ====================================================================

-- ====================================================================
-- END OF MIGRATION 11
-- ====================================================================
