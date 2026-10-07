-- ====================================================================
-- QUIZEAZZ — MIGRATION 10: CLASS + BATCH SEGREGATION
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..9. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   There is no slot system. Class + Batch is the segregation mechanism:
--   3 classes (SY CSDS A/B/C) x 3 batches (Batch 1/2/3) = 9 student groups.
--   A student belongs to exactly one group; a teacher assigns an exam to the
--   students of exactly one group via Select All (with optional individual
--   deselection). The backend enforces membership — never the browser.
--
-- WHAT THIS FILE DOES
--   1. Adds nullable class_name / batch columns to public.profiles with
--      CHECK constraints pinning the exact 9-group vocabulary. NULL means
--      "not onboarded yet". A both-or-neither CHECK forbids partial state.
--      No existing row is modified.
--   2. Adds set_my_class_batch(p_class, p_batch): one-shot, self-only,
--      student-only onboarding. Rejects any second write (immutability
--      after onboarding) and any value outside the vocabulary. Returns JSONB.
--   3. Recreates teacher_get_candidates() adding class_name/batch columns
--      (DROP + CREATE is required: RETURNS TABLE shape changes; CREATE OR
--      REPLACE cannot change it). Grants are re-applied explicitly because
--      DROP discards them.
--   4. Recreates teacher_set_exam_assignments() with optional
--      p_class/p_batch (DEFAULT NULL). When both are supplied, every
--      requested student id must carry that exact membership or the whole
--      call is rejected with the offending ids listed. When both are NULL,
--      the legacy individual-pick behavior is byte-identical. Duplicate
--      prevention (ON CONFLICT DO NOTHING) is unchanged.
--   5. Extends get_my_profile() (JSONB — no signature change) and
--      admin_get_profiles() (JSONB via row_to_json — select list extended)
--      to expose class_name/batch.
--
-- SCOPE
--   No table is dropped. No column is dropped. No data is deleted or
--   converted. No RLS policy is changed (profiles keeps SELECT-own-only; all
--   writes go through the new RPC). No exam/attempt/answer row is touched.
--   Student exam visibility is unchanged: get_student_exam_overview already
--   filters by exam_assignments membership server-side.
--
-- IDEMPOTENT: safe to run more than once (IF NOT EXISTS columns,
-- DROP CONSTRAINT IF EXISTS + ADD, DROP FUNCTION IF EXISTS + CREATE,
-- CREATE OR REPLACE elsewhere).
-- ====================================================================


-- ====================================================================
-- SECTION A — profile columns + vocabulary enforcement
-- ====================================================================

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS class_name TEXT;
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS batch TEXT;

ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_class_check;
ALTER TABLE public.profiles
  ADD CONSTRAINT profiles_class_check
  CHECK (class_name IS NULL OR class_name IN ('SY CSDS A', 'SY CSDS B', 'SY CSDS C'));

ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_batch_check;
ALTER TABLE public.profiles
  ADD CONSTRAINT profiles_batch_check
  CHECK (batch IS NULL OR batch IN ('Batch 1', 'Batch 2', 'Batch 3'));

-- A student is either fully onboarded or not onboarded at all.
ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_cohort_both_or_neither;
ALTER TABLE public.profiles
  ADD CONSTRAINT profiles_cohort_both_or_neither
  CHECK ((class_name IS NULL) = (batch IS NULL));


-- ====================================================================
-- SECTION B — one-shot self onboarding (student only)
-- --------------------------------------------------------------------
-- The browser supplies a choice, but the database owns the write: exactly
-- one row (the caller's own), exactly once (immutable afterwards), exactly
-- the fixed vocabulary. There is no edit path by design; corrections are an
-- administrator SQL operation.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.set_my_class_batch(
  p_class TEXT,
  p_batch TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user_id UUID;
  v_role TEXT;
  v_class TEXT;
  v_batch TEXT;
BEGIN
  v_user_id := auth.uid();
  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;

  SELECT p.role, p.class_name, p.batch
  INTO v_role, v_class, v_batch
  FROM public.profiles p
  WHERE p.id = v_user_id;

  IF v_role IS NULL THEN
    RAISE EXCEPTION 'No platform profile is provisioned for this account.';
  END IF;

  IF v_role <> 'student' THEN
    RAISE EXCEPTION 'Only student candidates set a class and batch.';
  END IF;

  -- One-shot: once stored, the browser can never change it again.
  IF v_class IS NOT NULL OR v_batch IS NOT NULL THEN
    RAISE EXCEPTION 'Class and batch are already set and cannot be changed from this device. Contact the examination administrator.';
  END IF;

  IF p_class IS NULL OR p_class NOT IN ('SY CSDS A', 'SY CSDS B', 'SY CSDS C') THEN
    RAISE EXCEPTION 'Class must be exactly SY CSDS A, SY CSDS B or SY CSDS C.';
  END IF;

  IF p_batch IS NULL OR p_batch NOT IN ('Batch 1', 'Batch 2', 'Batch 3') THEN
    RAISE EXCEPTION 'Batch must be exactly Batch 1, Batch 2 or Batch 3.';
  END IF;

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.profiles
  SET class_name = p_class,
      batch = p_batch
  WHERE id = v_user_id;

  RETURN jsonb_build_object(
    'id', v_user_id,
    'class_name', p_class,
    'batch', p_batch
  );
END;
$$;

REVOKE ALL ON FUNCTION public.set_my_class_batch(TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.set_my_class_batch(TEXT, TEXT) FROM anon;
GRANT  EXECUTE ON FUNCTION public.set_my_class_batch(TEXT, TEXT) TO authenticated;


-- ====================================================================
-- SECTION C — candidate registry exposes membership (DROP + CREATE)
-- --------------------------------------------------------------------
-- RETURNS TABLE shape changes (two columns added), so CREATE OR REPLACE is
-- illegal here (PostgreSQL 42P13). DROP + CREATE is required, and because
-- DROP discards grants, the ACL is re-applied explicitly below.
-- ====================================================================
DROP FUNCTION IF EXISTS public.teacher_get_candidates();

CREATE FUNCTION public.teacher_get_candidates()
RETURNS TABLE (
  id UUID,
  full_name TEXT,
  email TEXT,
  role TEXT,
  class_name TEXT,
  batch TEXT,
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
    p.class_name,
    p.batch,
    p.created_at
  FROM public.profiles p
  LEFT JOIN auth.users u ON u.id = p.id
  WHERE p.role = 'student'
  ORDER BY p.created_at DESC;
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_get_candidates() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_get_candidates() FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_get_candidates() TO authenticated;


-- ====================================================================
-- SECTION D — assignment RPC enforces group membership (DROP + CREATE)
-- --------------------------------------------------------------------
-- Signature gains p_class/p_batch (DEFAULT NULL), so DROP + CREATE is
-- required and the ACL is re-applied explicitly below. When a group is
-- supplied, BOTH values are required and EVERY requested id must match it;
-- otherwise the whole call fails and nothing is written. When both are
-- NULL the legacy individual-pick path is byte-identical (including
-- ON CONFLICT DO NOTHING duplicate prevention).
-- ====================================================================
DROP FUNCTION IF EXISTS public.teacher_set_exam_assignments(UUID, UUID[]);

CREATE FUNCTION public.teacher_set_exam_assignments(
  p_exam_id UUID,
  p_student_ids UUID[] DEFAULT NULL,
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
  v_desired UUID[];
  v_existing UUID[];
  v_invalid TEXT;
  v_mismatched TEXT;
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

  -- A group selection is all-or-nothing: a half-specified group would let a
  -- crafted request widen the cohort beyond what the teacher chose.
  IF (p_class IS NULL) <> (p_batch IS NULL) THEN
    RAISE EXCEPTION 'Class and batch must be selected together, or neither.';
  END IF;

  IF p_class IS NOT NULL THEN
    IF p_class NOT IN ('SY CSDS A', 'SY CSDS B', 'SY CSDS C') THEN
      RAISE EXCEPTION 'Class must be exactly SY CSDS A, SY CSDS B or SY CSDS C.';
    END IF;
    IF p_batch NOT IN ('Batch 1', 'Batch 2', 'Batch 3') THEN
      RAISE EXCEPTION 'Batch must be exactly Batch 1, Batch 2 or Batch 3.';
    END IF;
  END IF;

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

  -- Group enforcement: when the teacher assigned by class + batch, every id
  -- must carry that exact authoritative membership. Membership is read from
  -- profiles, never from the request. A single outsider aborts the write.
  IF p_class IS NOT NULL AND array_length(v_desired, 1) IS NOT NULL THEN
    SELECT string_agg(bad.id::TEXT, ', ')
    INTO v_mismatched
    FROM unnest(v_desired) AS bad(id)
    WHERE NOT EXISTS (
      SELECT 1 FROM public.profiles p
      WHERE p.id = bad.id
        AND p.class_name = p_class
        AND p.batch = p_batch
    );

    IF v_mismatched IS NOT NULL THEN
      RAISE EXCEPTION
        'The following students are not members of % / %: %. No assignments were changed.',
        p_class, p_batch, v_mismatched;
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

REVOKE ALL ON FUNCTION public.teacher_set_exam_assignments(UUID, UUID[], TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_set_exam_assignments(UUID, UUID[], TEXT, TEXT) FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_set_exam_assignments(UUID, UUID[], TEXT, TEXT) TO authenticated;


-- ====================================================================
-- SECTION E — expose membership on profile reads (JSONB: no DROP needed)
-- ====================================================================
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

  SELECT p.id, p.full_name, p.role, p.class_name, p.batch, p.created_at
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

    SELECT p.id, p.full_name, p.role, p.class_name, p.batch, p.created_at
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
    'class_name', v_row.class_name,
    'batch', v_row.batch,
    'created_at', v_row.created_at
  );
END;
$$;

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
      p.class_name,
      p.batch,
      u.email::TEXT AS email,
      p.created_at
    FROM public.profiles p
    LEFT JOIN auth.users u ON u.id = p.id
  ) t;

  RETURN v_rows;
END;
$$;


-- ====================================================================
-- SECTION F — VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- V1. Columns exist, constraints pinned:
-- SELECT column_name, data_type FROM information_schema.columns
-- WHERE table_schema='public' AND table_name='profiles'
-- AND column_name IN ('class_name','batch');
-- SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
-- WHERE conrelid='public.profiles'::regclass AND conname LIKE 'profiles_%';
--
-- V2. set_my_class_batch is one-shot, self-only, student-only:
-- SELECT p.proname, pg_get_function_identity_arguments(p.oid)
-- FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
-- WHERE n.nspname='public' AND p.proname='set_my_class_batch';
--
-- V3. Candidates registry exposes membership; assignments RPC carries group:
-- SELECT p.proname, pg_get_function_result(p.oid) AS result,
--        pg_get_function_identity_arguments(p.oid) AS args
-- FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
-- WHERE n.nspname='public'
-- AND p.proname IN ('teacher_get_candidates','teacher_set_exam_assignments');
--
-- V4. ACLs: authenticated executes set_my_class_batch, candidates registry
-- and assignments; anon/PUBLIC execute none of them; service_role untouched.
-- SELECT p.proname,
--   has_function_privilege('anon',p.oid,'EXECUTE') AS anon,
--   has_function_privilege('authenticated',p.oid,'EXECUTE') AS auth,
--   has_function_privilege('service_role',p.oid,'EXECUTE') AS svc
-- FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
-- WHERE n.nspname='public'
-- AND p.proname IN ('set_my_class_batch','teacher_get_candidates',
--                   'teacher_set_exam_assignments');
-- ====================================================================

-- ====================================================================
-- END OF MIGRATION 10
-- ====================================================================
