-- ====================================================================
-- QUIZEAZZ — RPC EXECUTE PRIVILEGE HARDENING (standalone patch)
-- --------------------------------------------------------------------
-- PURPOSE
--   Establish the intended EXECUTE ACL on every QuizEazz function.
--   This file does NOT create, alter or replace any function. It only
--   adjusts privileges. Run it AFTER migration 4.
--
--   If you have NOT yet applied migration 4, apply it first, then run
--   this file. Every statement here is idempotent, so re-running is safe.
--
-- WHY MIGRATION 4 FAILED TO ESTABLISH THESE ACLS
--   1. It never revoked EXECUTE from `anon` on ANY function. Every
--      revoke targeted PUBLIC or authenticated only. On Supabase, `anon`
--      holds its own explicit grant from ALTER DEFAULT PRIVILEGES, and a
--      PUBLIC revoke does not touch it. Result: anon could invoke every
--      SECURITY DEFINER RPC.
--   2. Its dynamic loop filtered on `p.pronargs > 0`, so every
--      zero-argument function (teacher_get_questions, teacher_get_exams,
--      get_my_profile, admin_get_profiles, ...) was skipped entirely.
--      Those kept the default PUBLIC grant, which is why
--      teacher_get_questions and teacher_get_exams report public = true.
--   3. Supabase's ALTER DEFAULT PRIVILEGES re-grants EXECUTE to anon and
--      authenticated for every function created in the public schema, so
--      CREATE FUNCTION silently widens the ACL again after any revoke.
--
-- POLICY ENFORCED HERE
--   Public-facing authenticated RPCs:
--       anon          NO EXECUTE
--       PUBLIC        NO EXECUTE
--       authenticated EXECUTE
--       service_role  EXECUTE
--   Internal trigger / helper / assertion functions:
--       anon          NO EXECUTE
--       PUBLIC        NO EXECUTE
--       authenticated NO EXECUTE
--       service_role  EXECUTE
--
-- SAFETY
--   - No DROP, no CREATE FUNCTION, no TRUNCATE, no DDL of any kind.
--   - No RLS policy is created, altered or dropped.
--   - No table privilege is changed.
--   - SECURITY DEFINER is untouched; only privileges move.
--   - Uses pg_get_function_identity_arguments so overloaded functions are
--     each addressed by their exact signature.
--   - Covers every function, including zero-argument ones, because it
--     enumerates pg_proc rather than hard-coding signatures.
-- ====================================================================


-- ====================================================================
-- SECTION 1 — ROLE PRECONDITION
-- Confirms the Supabase roles exist. Fails loudly rather than silently
-- granting to a role that is not there.
-- ====================================================================
DO $$
DECLARE
  v_missing TEXT;
BEGIN
  SELECT string_agg(r, ', ')
  INTO v_missing
  FROM (VALUES ('anon'), ('authenticated'), ('service_role')) AS w(r)
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles pr WHERE pr.rolname = w.r);

  IF v_missing IS NOT NULL THEN
    RAISE EXCEPTION 'Required Supabase roles are missing: %', v_missing;
  END IF;

  RAISE NOTICE 'Role precondition satisfied: anon, authenticated, service_role all exist.';
END $$;


-- ====================================================================
-- SECTION 2 — EXTERNAL RPCs: authenticated + service_role ONLY
--
-- Every function a student or faculty member legitimately calls.
-- anon and PUBLIC are stripped unconditionally.
-- ====================================================================
DO $$
DECLARE
  r          RECORD;
  v_sig      TEXT;
  v_external TEXT[] := ARRAY[
    'get_exam_questions',
    'get_student_exam_overview',
    'get_student_results',
    'get_student_attempt_review',
    'get_student_exam_answers',
    'get_my_profile',
    'start_or_get_exam_attempt',
    'save_exam_answer',
    'submit_exam_attempt',
    'grade_mcq_attempt',
    'pause_exam_attempt',
    'resume_exam_attempt',
    'record_security_event',
    'teacher_create_exam',
    'teacher_set_exam_status',
    'teacher_set_exam_schedule',
    'teacher_get_exams',
    'teacher_get_questions',
    'teacher_create_question',
    'teacher_get_candidates',
    'teacher_get_overview_stats',
    'teacher_get_exam_assignments',
    'teacher_set_exam_assignments',
    'teacher_get_exam_paper',
    'teacher_set_exam_paper',
    'teacher_get_attempts',
    'teacher_get_attempt_detail',
    'teacher_get_security_events',
    'admin_get_profiles'
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
    v_sig := r.sig;

    -- Order matters. Strip anon and PUBLIC first, then grant the two
    -- roles that are meant to call it. Doing it in this order means a
    -- re-run can never leave the function briefly over-granted.
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', v_sig);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon',    v_sig);
    EXECUTE format('GRANT  EXECUTE ON FUNCTION %s TO authenticated', v_sig);
    EXECUTE format('GRANT  EXECUTE ON FUNCTION %s TO service_role',  v_sig);

    v_count := v_count + 1;
  END LOOP;

  RAISE NOTICE 'External RPC ACLs pinned: % function(s) -> authenticated + service_role only.', v_count;
END $$;


-- ====================================================================
-- SECTION 3 — INTERNAL FUNCTIONS: service_role ONLY
--
-- Trigger functions, assertion helpers and the internal grading engine.
-- These are called from inside other functions or from a trigger, never
-- directly by a browser, so no client role needs EXECUTE.
-- ====================================================================
DO $$
DECLARE
  r            RECORD;
  v_sig        TEXT;
  v_internal TEXT[] := ARRAY[
    'quizeazz_grade_objective_marks',
    'quizeazz_assert_faculty',
    'quizeazz_assert_composition_editable',
    'protect_exam_composition_mutation',
    'protect_attempt_mutation',
    'protect_profile_role_escalation',
    'check_answer_write_eligibility',
    'handle_new_user'
  ];
  v_count INT := 0;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure::TEXT AS sig
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname = ANY(v_internal)
  LOOP
    v_sig := r.sig;

    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',       v_sig);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon',          v_sig);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', v_sig);
    EXECUTE format('GRANT  EXECUTE ON FUNCTION %s TO service_role', v_sig);

    v_count := v_count + 1;
  END LOOP;

  RAISE NOTICE 'Internal function ACLs pinned: % function(s) -> service_role only.', v_count;
END $$;


-- ====================================================================
-- SECTION 4 — SWEEP: any OTHER function in the public schema
--
-- Catches a QuizEazz RPC added later that is not in the lists above.
-- PostgREST only exposes functions in the exposed schemas, so anything
-- else here is either an internal helper or dead code. Both must not be
-- callable by anon.
--
-- anon is stripped from every remaining public-schema function.
-- authenticated is left alone so no legitimate call path is broken.
-- ====================================================================
DO $$
DECLARE
  r      RECORD;
  v_count INT := 0;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure::TEXT AS sig
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname NOT IN (
        -- external
        'get_exam_questions','get_student_exam_overview','get_student_results',
        'get_student_attempt_review','get_student_exam_answers','get_my_profile',
        'start_or_get_exam_attempt','save_exam_answer','submit_exam_attempt',
        'grade_mcq_attempt','pause_exam_attempt','resume_exam_attempt',
        'record_security_event','teacher_create_exam','teacher_set_exam_status',
        'teacher_set_exam_schedule','teacher_get_exams','teacher_get_questions',
        'teacher_create_question','teacher_get_candidates',
        'teacher_get_overview_stats','teacher_get_exam_assignments',
        'teacher_set_exam_assignments','teacher_get_exam_paper',
        'teacher_set_exam_paper','teacher_get_attempts',
        'teacher_get_attempt_detail','teacher_get_security_events',
        'admin_get_profiles',
        -- internal
        'quizeazz_grade_objective_marks','quizeazz_assert_faculty',
        'quizeazz_assert_composition_editable','protect_exam_composition_mutation',
        'protect_attempt_mutation','protect_profile_role_escalation',
        'check_answer_write_eligibility','handle_new_user'
      )
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', r.sig);
    v_count := v_count + 1;
  END LOOP;

  IF v_count > 0 THEN
    RAISE NOTICE
      'Sweep: stripped anon from % unclassified public-schema function(s). Review these.',
      v_count;
  ELSE
    RAISE NOTICE 'Sweep: no unclassified public-schema functions found.';
  END IF;
END $$;


-- ====================================================================
-- SECTION 5 — STOP THE BLEED
--
-- Supabase's ALTER DEFAULT PRIVILEGES re-grants EXECUTE to anon and
-- authenticated for every function created in the public schema. That is
-- exactly what defeated migration 4: any future CREATE FUNCTION would
-- silently restore anon access.
--
-- Narrow the default so new functions start locked. This affects future
-- CREATE FUNCTION only; existing functions keep the ACL set above.
-- ====================================================================
DO $$
BEGIN
  EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC';
  EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM anon';

  RAISE NOTICE
    'Default privileges narrowed: future public-schema functions will not auto-grant EXECUTE to PUBLIC or anon.';
END $$;


-- ====================================================================
-- SECTION 6 — REPORT
--
-- Read-only. Expected result after a successful run:
--   anon_has_execute        = false for every row
--   public_has_execute      = false for every row
--   authenticated_can_execute = true only for the external list
--   service_role_can_execute = true for every row
-- ====================================================================
SELECT
  p.proname AS function_name,
  pg_get_function_identity_arguments(p.oid) AS arguments,
  p.prosecdef AS security_definer,
  p.pronargs AS arg_count,
  COALESCE(has_function_privilege('anon',          p.oid, 'EXECUTE'), false) AS anon_has_execute,
  COALESCE(has_function_privilege('public',        p.oid, 'EXECUTE'), false) AS public_has_execute,
  COALESCE(has_function_privilege('authenticated', p.oid, 'EXECUTE'), false) AS authenticated_can_execute,
  COALESCE(has_function_privilege('service_role',  p.oid, 'EXECUTE'), false) AS service_role_can_execute,
  p.proacl AS raw_acl
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
ORDER BY p.proname, pg_get_function_identity_arguments(p.oid);


-- === 6B. FAILURE CHECK (expect zero rows) ===
-- Any row here means the privilege fix is incomplete.
SELECT
  p.proname AS function_name,
  pg_get_function_identity_arguments(p.oid) AS arguments
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND (
    has_function_privilege('anon',   p.oid, 'EXECUTE')
    OR has_function_privilege('public', p.oid, 'EXECUTE')
  )
ORDER BY p.proname;


-- === 6C. OVER-REVOCATION CHECK (expect zero rows for these names) ===
-- Confirms requirement 4: legitimate student and teacher RPCs still work.
-- Run this and confirm it returns zero rows.
SELECT
  p.proname AS function_name,
  pg_get_function_identity_arguments(p.oid) AS arguments
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.proname IN (
    'get_exam_questions','get_student_exam_overview','get_student_results',
    'get_student_attempt_review','get_student_exam_answers','get_my_profile',
    'start_or_get_exam_attempt','save_exam_answer','submit_exam_attempt',
    'grade_mcq_attempt','pause_exam_attempt','resume_exam_attempt',
    'record_security_event','teacher_create_exam','teacher_set_exam_status',
    'teacher_set_exam_schedule','teacher_get_exams','teacher_get_questions',
    'teacher_create_question','teacher_get_candidates',
    'teacher_get_overview_stats','teacher_get_exam_assignments',
    'teacher_set_exam_assignments','teacher_get_exam_paper',
    'teacher_set_exam_paper','teacher_get_attempts',
    'teacher_get_attempt_detail','teacher_get_security_events',
    'admin_get_profiles'
  )
  AND NOT has_function_privilege('authenticated', p.oid, 'EXECUTE')
ORDER BY p.proname;

-- ====================================================================
-- END OF PRIVILEGE PATCH
--
-- This file contains only REVOKE / GRANT / ALTER DEFAULT PRIVILEGES
-- against functions, plus three read-only verification SELECTs.
-- It creates nothing, drops nothing and modifies no data.
-- ====================================================================
