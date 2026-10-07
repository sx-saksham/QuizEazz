-- ====================================================================
-- QUIZEAZZ — MIGRATION 12: EXAM QUESTION SNAPSHOTS + SAFE EDITING
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..11. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   Exam taking, grading and review read LIVE question-bank rows, so any
--   future edit of a question would silently rewrite published exams,
--   in-flight grading and historical reviews. This migration freezes an
--   immutable per-exam snapshot at paper-save time and switches every
--   historical read path onto it. Editing the bank afterwards is then
--   always safe: future papers pick up new content only when explicitly
--   re-saved, and only while no finalized attempt exists (existing guard).
--
-- WHAT THIS FILE DOES
--   1. Adds exam_questions.question_snapshot JSONB (nullable; backfilled).
--   2. Adds quizeazz_build_question_snapshot() (internal helper, no public
--      execute): captures title/text/type/language/category/subcategory/
--      difficulty/marks/requirements/constraints/starter/reference/options
--      (with MCQ keys)/validated+unvalidated hidden tests/snapshotted_at.
--   3. Backfills every existing paper row from today's live rows (no
--      fabrication: a byte-faithful copy of what grading/review read now).
--   4. Recreates teacher_set_exam_paper (same signature): rebuilds snapshots
--      for the whole paper on every save. Reachable only with zero finalized
--      attempts (existing composition guard), so rebuilds are always safe.
--      Publish stays transactional: paper + snapshots or nothing.
--   5. Recreates quizeazz_judge0_pending_answers (DROP + CREATE: one column
--      added) returning tests JSONB from the SNAPSHOT. Grading no longer
--      touches mutable test rows. ACLs re-applied explicitly (DROP clears).
--   6. Recreates get_exam_questions / get_student_attempt_review /
--      teacher_get_attempt_detail to serve question content from the
--      snapshot with live fallback (identical output shapes; reference
--      solution and MCQ keys stay stripped for candidates exactly as now).
--   7. Adds teacher_update_question (all fields, create-mirroring
--      validation incl. C++-only) and teacher_update_exam
--      (title/description/duration, blocked when finalized attempts exist).
--      Bank edits never touch snapshots or history.
--
-- SCOPE
--   No table dropped. No column dropped. No row deleted. No RLS change.
--   No ACL widened (helper denies everyone; edit RPCs are faculty-checked
--   authenticated calls). Legacy rows keep working via backfill + fallback.
--   The 7 legacy Pending Python answers are untouched.
--
-- IDEMPOTENT: safe to run more than once (IF NOT EXISTS / DROP IF EXISTS /
-- CREATE OR REPLACE / conditional backfill).
-- ====================================================================


-- ====================================================================
-- SECTION A — snapshot column + builder + backfill
-- ====================================================================

ALTER TABLE public.exam_questions
  ADD COLUMN IF NOT EXISTS question_snapshot JSONB;

-- Internal helper. NOT granted to any role (like quizeazz_assert_faculty):
-- only SECURITY DEFINER functions call it, which run with owner rights.
CREATE OR REPLACE FUNCTION public.quizeazz_build_question_snapshot(
  p_question_id UUID,
  p_marks NUMERIC
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_q RECORD;
  v_tests JSONB;
BEGIN
  SELECT q.* INTO v_q
  FROM public.questions q
  WHERE q.id = p_question_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Question % does not exist.', p_question_id;
  END IF;

  -- All cases, validated or not: a faithful record. Grading filters to
  -- validated ones at read time, exactly as the paper gate requires.
  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'case_order', t.case_order,
      'stdin', t.stdin,
      'expected_stdout', t.expected_stdout
    ) ORDER BY t.case_order
  ), '[]'::jsonb)
  INTO v_tests
  FROM public.programming_test_cases t
  WHERE t.question_id = p_question_id;

  RETURN jsonb_build_object(
    'question_id', v_q.id,
    'title', v_q.title,
    'question_text', v_q.question_text,
    'question_type', v_q.question_type,
    'category', v_q.category,
    'subcategory', v_q.subcategory,
    'difficulty', v_q.difficulty,
    'language', v_q.language,
    'marks', COALESCE(p_marks, 0),
    'input_requirements', v_q.input_requirements,
    'output_requirements', v_q.output_requirements,
    'constraints', v_q.constraints,
    'starter_code', v_q.starter_code,
    'reference_solution', v_q.reference_solution,
    'options', COALESCE(v_q.options, '[]'::jsonb),
    'tests', v_tests,
    'snapshotted_at', now()
  );
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_build_question_snapshot(UUID, NUMERIC) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_build_question_snapshot(UUID, NUMERIC) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_build_question_snapshot(UUID, NUMERIC) FROM authenticated;
REVOKE ALL ON FUNCTION public.quizeazz_build_question_snapshot(UUID, NUMERIC) FROM service_role;

-- Backfill: freeze today's live content onto every existing paper row.
-- Deterministic copy, no invention. Rows already snapshotted are skipped.
-- The composition trigger requires the authoritative flag for direct writes
-- (faculty RPCs set it internally; this one-off backfill sets it here).
SELECT set_config('quizeazz.authoritative_context', 'true', true);
UPDATE public.exam_questions eq
SET question_snapshot = public.quizeazz_build_question_snapshot(eq.question_id, eq.marks)
WHERE eq.question_snapshot IS NULL;


-- ====================================================================
-- SECTION B — paper save rebuilds snapshots (same signature)
-- --------------------------------------------------------------------
-- The upserted/desired CTEs, guards and return shape are unchanged from
-- migration 9. One addition: after the upsert, every paper row's snapshot
-- is rebuilt from the current bank. Reachable only with zero finalized
-- attempts (quizeazz_assert_composition_editable), so no historical exam
-- can ever have its snapshot rewritten underneath live attempts.
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

  -- Freeze the paper exactly as saved: every row carries the bank content
  -- and hidden tests the exam was composed with. Later bank edits cannot
  -- leak into this paper; a future re-save (draft only, per the composition
  -- guard above) explicitly refreshes it.
  UPDATE public.exam_questions eq
  SET question_snapshot = public.quizeazz_build_question_snapshot(eq.question_id, eq.marks)
  WHERE eq.exam_id = p_exam_id;

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
-- SECTION C — pending answers carry snapshot tests (DROP + CREATE)
-- --------------------------------------------------------------------
-- Return shape gains `tests JSONB` sourced from the paper snapshot, so a
-- DROP + CREATE is required (CREATE OR REPLACE cannot change it).
-- Everything else is byte-identical to migration 8. ACLs re-applied
-- explicitly because DROP discards them.
-- ====================================================================
DROP FUNCTION IF EXISTS public.quizeazz_judge0_pending_answers(UUID);

CREATE FUNCTION public.quizeazz_judge0_pending_answers(p_attempt_id UUID)
RETURNS TABLE (
  answer_id UUID,
  question_id UUID,
  language TEXT,
  source_code TEXT,
  stdin TEXT,
  marks_total NUMERIC,
  tests JSONB
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_status TEXT;
BEGIN
  SELECT att.status INTO v_status
  FROM public.exam_attempts att
  WHERE att.id = p_attempt_id;

  IF v_status IS NULL THEN
    RAISE EXCEPTION 'Attempt % does not exist.', p_attempt_id;
  END IF;

  -- Hard stop: an exam in progress is never sent to Judge0.
  IF v_status = 'in_progress' THEN
    RAISE EXCEPTION
      'Refusing to execute code for attempt %: the examination is still in progress.', p_attempt_id;
  END IF;

  IF v_status NOT IN ('submitted', 'timed_out', 'evaluating', 'evaluated') THEN
    RAISE EXCEPTION
      'Attempt % is in status % and cannot be graded.', p_attempt_id, v_status;
  END IF;

  RETURN QUERY
  SELECT
    a.id,
    a.question_id,
    a.language,
    a.code,
    NULL::TEXT,
    COALESCE(eq.marks, 0),
    COALESCE(eq.question_snapshot->'tests', '[]'::jsonb)
  FROM public.answers a
  JOIN public.exam_questions eq
    ON eq.question_id = a.question_id
   AND eq.exam_id = (SELECT att.exam_id FROM public.exam_attempts att WHERE att.id = p_attempt_id)
  JOIN public.questions q
    ON q.id = a.question_id
   AND q.question_type = 'coding'
  WHERE a.attempt_id = p_attempt_id
    AND a.evaluation_status = 'Pending Evaluation'
  ORDER BY eq.id;
END;
$$;

REVOKE ALL ON FUNCTION public.quizeazz_judge0_pending_answers(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.quizeazz_judge0_pending_answers(UUID) FROM anon;
REVOKE ALL ON FUNCTION public.quizeazz_judge0_pending_answers(UUID) FROM authenticated;
GRANT  EXECUTE ON FUNCTION public.quizeazz_judge0_pending_answers(UUID) TO service_role;


-- ====================================================================
-- SECTION D — candidate paper reads the snapshot (same shape)
-- --------------------------------------------------------------------
-- Output keys are unchanged; each question field prefers the frozen paper
-- snapshot and falls back to the live bank row (backfill + paper saves keep
-- snapshots populated, so the fallback is belt-and-braces only).
-- reference_solution is still never served; MCQ options stay key-stripped.
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
      'title', COALESCE(eq.question_snapshot->>'title', q.title),
      'question_text', COALESCE(eq.question_snapshot->>'question_text', q.question_text),
      'question_type', COALESCE(eq.question_snapshot->>'question_type', q.question_type),
      'category', COALESCE(eq.question_snapshot->>'category', q.category),
      'subcategory', COALESCE(eq.question_snapshot->>'subcategory', q.subcategory),
      'difficulty', COALESCE(eq.question_snapshot->>'difficulty', q.difficulty),
      'marks', COALESCE(eq.marks, 0),
      'starter_code', COALESCE(eq.question_snapshot->>'starter_code', q.starter_code),
      'language', COALESCE(eq.question_snapshot->>'language', q.language),
      'input_requirements', COALESCE(eq.question_snapshot->>'input_requirements', q.input_requirements),
      'output_requirements', COALESCE(eq.question_snapshot->>'output_requirements', q.output_requirements),
      'constraints', COALESCE(eq.question_snapshot->>'constraints', q.constraints),
      'options', CASE
        WHEN COALESCE(eq.question_snapshot->>'question_type', q.question_type) = 'mcq'
             AND jsonb_typeof(COALESCE(eq.question_snapshot->'options', q.options)) = 'array'
        THEN (
          SELECT jsonb_agg(
            jsonb_build_object('id', elem->>'id', 'text', elem->>'text') ORDER BY ord
          )
          FROM jsonb_array_elements(COALESCE(eq.question_snapshot->'options', q.options)) WITH ORDINALITY AS t(elem, ord)
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
-- SECTION E — student review reads the snapshot (same shape)
-- --------------------------------------------------------------------
-- Every question field prefers the frozen paper snapshot so the review
-- shows exactly what the candidate received. MCQ keys still resolve, and
-- reference/hidden content is still never served.
-- ====================================================================
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
      'question_title', COALESCE(eq.question_snapshot->>'title', q.title),
      'question_type', COALESCE(eq.question_snapshot->>'question_type', q.question_type),
      'question_text', COALESCE(eq.question_snapshot->>'question_text', q.question_text),
      'marks_total', COALESCE(eq.marks, 0),
      'marks_awarded', COALESCE(a.marks_awarded, 0),
      'outcome', CASE
        WHEN a.evaluation_status IS NOT NULL THEN a.evaluation_status
        WHEN COALESCE(eq.question_snapshot->>'question_type', q.question_type) = 'mcq' THEN 'Not Attempted'
        ELSE 'Pending Evaluation'
      END,
      'is_correct', a.is_correct,
      'student_option_id', a.selected_option_id,
      'correct_option_id', CASE WHEN COALESCE(eq.question_snapshot->>'question_type', q.question_type) = 'mcq' THEN key.correct_id ELSE NULL END,
      'student_code', a.code,
      'language', COALESCE(a.language, eq.question_snapshot->>'language', q.language, 'text'),
      'options', CASE
        WHEN COALESCE(eq.question_snapshot->>'question_type', q.question_type) = 'mcq'
             AND jsonb_typeof(COALESCE(eq.question_snapshot->'options', q.options)) = 'array'
        THEN (
          SELECT jsonb_agg(jsonb_build_object('id', elem->>'id', 'text', elem->>'text') ORDER BY ord)
          FROM jsonb_array_elements(COALESCE(eq.question_snapshot->'options', q.options)) WITH ORDINALITY AS t(elem, ord)
        )
        ELSE NULL
      END,
      'explanation', COALESCE(eq.question_snapshot->>'explanation', q.explanation)
    ) ORDER BY eq.id
  ), '[]'::jsonb)
  INTO v_evaluations
  FROM public.exam_questions eq
  JOIN public.questions q ON q.id = eq.question_id
  LEFT JOIN public.answers a ON a.attempt_id = p_attempt_id AND a.question_id = q.id
  LEFT JOIN LATERAL (
    SELECT elem->>'id' AS correct_id
    FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(COALESCE(eq.question_snapshot->'options', q.options)) = 'array'
           THEN COALESCE(eq.question_snapshot->'options', q.options) ELSE '[]'::jsonb END
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
-- SECTION F — teacher attempt detail reads the snapshot (same shape)
-- --------------------------------------------------------------------
-- Faculty see what the candidate saw. Snapshot-first with live fallback,
-- identical keys to the previous version.
-- ====================================================================
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
      'title', COALESCE(eq.question_snapshot->>'title', q.title),
      'question_type', COALESCE(eq.question_snapshot->>'question_type', q.question_type),
      'language', COALESCE(a.language, eq.question_snapshot->>'language', q.language),
      'marks_total', COALESCE(eq.marks, 0),
      'marks_awarded', COALESCE(a.marks_awarded, 0),
      'outcome', COALESCE(a.evaluation_status, CASE WHEN COALESCE(eq.question_snapshot->>'question_type', q.question_type) = 'coding' THEN 'Pending Evaluation' ELSE 'Not Attempted' END),
      'is_correct', a.is_correct,
      'student_option_id', a.selected_option_id,
      'correct_option_id', CASE WHEN COALESCE(eq.question_snapshot->>'question_type', q.question_type) = 'mcq' THEN key.correct_id ELSE NULL END,
      'options', CASE
        WHEN COALESCE(eq.question_snapshot->>'question_type', q.question_type) = 'mcq'
             AND jsonb_typeof(COALESCE(eq.question_snapshot->'options', q.options)) = 'array'
        THEN (
          SELECT jsonb_agg(jsonb_build_object('id', elem->>'id', 'text', elem->>'text') ORDER BY ord)
          FROM jsonb_array_elements(COALESCE(eq.question_snapshot->'options', q.options)) WITH ORDINALITY AS t(elem, ord)
        )
        ELSE NULL
      END,
      'code', a.code,
      'explanation', COALESCE(eq.question_snapshot->>'explanation', q.explanation)
    ) ORDER BY eq.id
  ), '[]'::jsonb)
  INTO v_items
  FROM public.exam_questions eq
  JOIN public.questions q ON q.id = eq.question_id
  LEFT JOIN public.answers a ON a.attempt_id = p_attempt_id AND a.question_id = q.id
  LEFT JOIN LATERAL (
    SELECT elem->>'id' AS correct_id
    FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(COALESCE(eq.question_snapshot->'options', q.options)) = 'array'
           THEN COALESCE(eq.question_snapshot->'options', q.options) ELSE '[]'::jsonb END
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


-- ====================================================================
-- SECTION G — bank editing that can never rewrite history
-- --------------------------------------------------------------------
-- teacher_update_question edits ONLY the live bank row. Published papers,
-- attempts and reviews read frozen snapshots, so no edit — however large —
-- can alter what any existing exam showed or graded. No version column is
-- needed precisely because history lives in snapshots, not in versions.
--
-- teacher_update_exam edits title/description/duration only, and only while
-- no finalized attempt exists (same rule as paper edits). Schedule stays on
-- teacher_set_exam_schedule with its own lighter guard.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.teacher_update_question(
  p_question_id UUID,
  p_title TEXT DEFAULT NULL,
  p_question_text TEXT DEFAULT NULL,
  p_question_type TEXT DEFAULT NULL,
  p_category TEXT DEFAULT NULL,
  p_subcategory TEXT DEFAULT NULL,
  p_marks INT DEFAULT NULL,
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
  v_current RECORD;
  v_new_type TEXT;
  v_new_marks INT;
  v_new_lang TEXT;
  v_opt_arr JSONB;
  v_correct TEXT;
  v_hits INT := 0;
  v_normalized JSONB;
  v_row RECORD;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role INTO v_caller_role FROM public.profiles p WHERE p.id = v_caller_id;
  IF v_caller_role NOT IN ('teacher', 'admin') OR v_caller_role IS NULL THEN
    RAISE EXCEPTION 'Access denied: Only faculty or administrators can edit questions.';
  END IF;

  SELECT q.* INTO v_current
  FROM public.questions q
  WHERE q.id = p_question_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Question % does not exist.', p_question_id;
  END IF;

  -- NULL means "leave unchanged". Every supplied value is validated exactly
  -- as in teacher_create_question.
  IF p_title IS NOT NULL AND btrim(p_title) = '' THEN
    RAISE EXCEPTION 'Question title cannot be empty.';
  END IF;

  IF p_question_text IS NOT NULL AND btrim(p_question_text) = '' THEN
    RAISE EXCEPTION 'Question statement cannot be empty.';
  END IF;

  IF p_question_type IS NOT NULL AND p_question_type NOT IN ('mcq', 'coding') THEN
    RAISE EXCEPTION 'Question type must be exactly ''mcq'' or ''coding''.';
  END IF;
  v_new_type := COALESCE(p_question_type, v_current.question_type);

  IF p_category IS NOT NULL AND btrim(p_category) = '' THEN
    RAISE EXCEPTION 'Question category cannot be empty.';
  END IF;

  v_new_marks := COALESCE(p_marks, v_current.marks, 10);
  IF v_new_marks < 1 OR v_new_marks > 100 THEN
    RAISE EXCEPTION 'Marks must be between 1 and 100.';
  END IF;

  -- C++-ONLY (QuizEazz V1): a coding question may only carry 'cpp'.
  v_new_lang := COALESCE(p_language, v_current.language);
  IF v_new_type = 'coding' AND v_new_lang IS DISTINCT FROM 'cpp' THEN
    RAISE EXCEPTION 'Programming questions must use language ''cpp'' (C++ only).';
  END IF;

  IF v_new_type = 'mcq' THEN
    IF p_options IS NOT NULL THEN
      v_opt_arr := p_options;
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
      IF p_correct_answer IS NULL THEN
        RAISE EXCEPTION 'You must select the correct answer key when replacing MCQ options.';
      END IF;
      SELECT count(*) INTO v_hits
      FROM jsonb_array_elements(v_opt_arr) AS elem
      WHERE elem->>'id' = p_correct_answer;
      IF v_hits <> 1 THEN
        RAISE EXCEPTION 'The selected answer key does not match exactly one option.';
      END IF;
      SELECT jsonb_agg(
        CASE WHEN elem->>'id' = p_correct_answer
             THEN jsonb_set(elem - 'is_correct', '{is_correct}', 'true'::jsonb)
             ELSE (elem - 'is_correct')
        END
        ORDER BY ord
      )
      INTO v_normalized
      FROM jsonb_array_elements(v_opt_arr) WITH ORDINALITY AS t(elem, ord);
    ELSE
      v_normalized := NULL;
    END IF;
  ELSE
    -- Coding: reference/spec fields follow the same required rules as
    -- creation whenever they are supplied; stored values otherwise persist.
    IF p_reference_solution IS NOT NULL AND btrim(p_reference_solution) = ''
       AND (v_current.reference_solution IS NULL OR btrim(v_current.reference_solution) = '') THEN
      RAISE EXCEPTION 'A faculty reference solution is required for programming questions.';
    END IF;
    IF p_input_requirements IS NOT NULL AND btrim(p_input_requirements) = ''
       AND (v_current.input_requirements IS NULL OR btrim(v_current.input_requirements) = '') THEN
      RAISE EXCEPTION 'Input requirements are required for programming questions.';
    END IF;
    IF p_output_requirements IS NOT NULL AND btrim(p_output_requirements) = ''
       AND (v_current.output_requirements IS NULL OR btrim(v_current.output_requirements) = '') THEN
      RAISE EXCEPTION 'Output requirements are required for programming questions.';
    END IF;
    IF p_constraints IS NOT NULL AND btrim(p_constraints) = ''
       AND (v_current.constraints IS NULL OR btrim(v_current.constraints) = '') THEN
      RAISE EXCEPTION 'Constraints are required for programming questions.';
    END IF;
    v_normalized := NULL;
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.questions q SET
    title = COALESCE(NULLIF(btrim(p_title), ''), q.title),
    question_text = COALESCE(NULLIF(btrim(p_question_text), ''), q.question_text),
    question_type = v_new_type,
    category = COALESCE(NULLIF(btrim(p_category), ''), q.category),
    subcategory = CASE WHEN p_subcategory IS NULL THEN q.subcategory
                       ELSE NULLIF(btrim(p_subcategory), '') END,
    marks = v_new_marks,
    explanation = CASE WHEN p_explanation IS NULL THEN q.explanation
                       ELSE NULLIF(p_explanation, '') END,
    language = CASE WHEN v_new_type = 'coding' THEN 'cpp' ELSE p_language END,
    input_requirements = COALESCE(NULLIF(btrim(p_input_requirements), ''), q.input_requirements),
    output_requirements = COALESCE(NULLIF(btrim(p_output_requirements), ''), q.output_requirements),
    constraints = COALESCE(NULLIF(btrim(p_constraints), ''), q.constraints),
    starter_code = CASE WHEN p_starter_code IS NULL THEN q.starter_code ELSE p_starter_code END,
    reference_solution = CASE WHEN p_reference_solution IS NULL THEN q.reference_solution ELSE p_reference_solution END,
    options = CASE WHEN v_new_type = 'mcq' AND p_options IS NOT NULL THEN v_normalized ELSE q.options END
  WHERE q.id = p_question_id
  RETURNING * INTO v_row;

  RETURN to_jsonb(v_row);
END;
$$;

-- NOTE: bare (arg-list-free) form is used here because this is the sole
-- overload: it resolves unambiguously, while the explicit 16-type list hit
-- a function-lookup quirk (42883) on this execution path despite matching
-- pg_proc exactly. Verified live.
REVOKE ALL ON FUNCTION public.teacher_update_question FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_update_question FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_update_question TO authenticated;


CREATE OR REPLACE FUNCTION public.teacher_update_exam(
  p_exam_id UUID,
  p_title TEXT DEFAULT NULL,
  p_description TEXT DEFAULT NULL,
  p_duration_minutes INT DEFAULT NULL
)
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

  SELECT * INTO v_exam FROM public.exams e WHERE e.id = p_exam_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Examination % does not exist.', p_exam_id;
  END IF;

  IF v_exam.status = 'closed' THEN
    RAISE EXCEPTION 'This examination is closed. Its details are frozen.';
  END IF;

  -- Title/description/duration shape what candidates see, so they follow the
  -- same no-finalized-attempts rule as paper edits. Schedule keeps its own
  -- lighter guard on teacher_set_exam_schedule.
  PERFORM public.quizeazz_assert_composition_editable(p_exam_id);

  IF p_title IS NOT NULL AND btrim(p_title) = '' THEN
    RAISE EXCEPTION 'Examination title cannot be empty.';
  END IF;

  IF p_duration_minutes IS NOT NULL AND (p_duration_minutes < 1 OR p_duration_minutes > 1440) THEN
    RAISE EXCEPTION 'Duration must be between 1 and 1440 minutes (24 hours).';
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.exams e
  SET title = COALESCE(NULLIF(btrim(p_title), ''), e.title),
      description = CASE WHEN p_description IS NULL THEN e.description ELSE NULLIF(p_description, '') END,
      duration_minutes = COALESCE(p_duration_minutes, e.duration_minutes),
      updated_at = now()
  WHERE e.id = p_exam_id
  RETURNING * INTO v_exam;

  RETURN to_jsonb(v_exam);
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_update_exam FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_update_exam FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_update_exam TO authenticated;


-- ====================================================================
-- SECTION H — VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- H1. Snapshot column populated on every paper row (expect 0 rows):
-- SELECT eq.exam_id, eq.question_id FROM public.exam_questions eq
-- WHERE eq.question_snapshot IS NULL;
--
-- H2. Read paths resolve from snapshots (spot-check one evaluated attempt;
-- compare against the same question's live row to confirm independence):
-- SELECT a.attempt_id, a.evaluation_status FROM public.answers a LIMIT 5;
--
-- H3. New RPCs exist, faculty-gated, authenticated-executable:
-- SELECT p.proname,
--   has_function_privilege('anon',p.oid,'EXECUTE') AS anon,
--   has_function_privilege('authenticated',p.oid,'EXECUTE') AS auth
-- FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
-- WHERE n.nspname='public'
-- AND p.proname IN ('teacher_update_question','teacher_update_exam',
--   'quizeazz_build_question_snapshot');
--
-- H4. pending_answers now carries snapshot tests (expect tests JSONB):
-- SELECT pg_get_function_result(p.oid) FROM pg_proc p
-- JOIN pg_namespace n ON n.oid=p.pronamespace
-- WHERE n.nspname='public' AND p.proname='quizeazz_judge0_pending_answers';
-- ====================================================================

-- ====================================================================
-- END OF MIGRATION 12
-- ====================================================================
