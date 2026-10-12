-- ====================================================================
-- QuizEazz ZZSMOKE TEACHER BOOTSTRAP  (privileged - run ONCE)
-- ====================================================================
-- PURPOSE
--   Promote ONE newly created ZZSMOKE fixture account to role 'teacher' so
--   that the fixture's authoring RPCs (teacher_create_question,
--   teacher_set_test_cases, teacher_set_exam_paper, teacher_set_exam_assignments,
--   teacher_publish_now) can be called.
--
-- WHY A PROMPT IS NEEDED
--   handle_new_user() hardcodes role='student' and never reads a role from
--   metadata. protect_profile_role_escalation blocks any non-admin caller from
--   changing a role, and quizeazz_assert_faculty reads only profiles.role.
--   No RPC can grant faculty. So exactly one role write is unavoidable, and
--   this is the project's own documented mechanism (see
--   supabase/seed_bootstrap_teacher.sql).
--
-- HOW THIS IS SAFE (unlike the generic seed script)
--   * Targeting is by EXACT UUID only. There is no email lookup, no
--     "most recently registered user" fallback, no LIKE-based selection.
--     The generic seed's blank-email fallback can promote a REAL user; that
--     path is deliberately absent here.
--   * The target is verified three ways before any write:
--       (a) the auth user with that exact id exists;
--       (b) its email matches the ZZSMOKE fixture pattern;
--       (c) its profile exists, is named ZZSMOKE, and is currently 'student'.
--   * It aborts if the account is anything other than a pristine fixture.
--   * The authoritative context is set TRANSACTION-LOCAL (is_local = true) and
--     only around the intended UPDATE. It is discarded when the transaction
--     ends and cannot leak to another session.
--
-- OPERATOR: replace BOTH uuid literals below.
--   v_teacher_uuid  - the id returned when the ZZSMOKE Teacher was created
--   v_expected_email - the exact email you used for that account
-- ====================================================================

DO $$
DECLARE
  v_teacher_uuid   UUID := '00000000-0000-0000-0000-000000000000'::uuid;
  v_expected_email TEXT := 'replace-with-the-exact-fixture-email';

  v_user_email   TEXT;
  v_profile_name TEXT;
  v_profile_role TEXT;
  v_n            INT;
BEGIN
  -- ==================================================================
  -- STEP 1 - validate the manifest BEFORE touching anything
  -- ==================================================================
  IF v_teacher_uuid IS NULL THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: v_teacher_uuid is NULL.';
  END IF;
  IF v_expected_email IS NULL OR btrim(v_expected_email) = ''
     OR v_expected_email ILIKE '%replace-%'
     OR v_expected_email ILIKE '%your-%' THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: set v_expected_email to the real fixture email.';
  END IF;

  -- (a) the auth user must exist under this exact id
  SELECT u.email INTO v_user_email
    FROM auth.users u
   WHERE u.id = v_teacher_uuid;

  IF v_user_email IS NULL THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: no auth.users row with id %.', v_teacher_uuid;
  END IF;

  -- (b) the email must match BOTH the expected value and the ZZSMOKE pattern
  IF lower(v_user_email) <> lower(btrim(v_expected_email)) THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: email mismatch. expected % but id % is %.',
      v_expected_email, v_teacher_uuid, v_user_email;
  END IF;
  IF v_user_email NOT ILIKE '%zzsmoke%' THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: % is not a ZZSMOKE fixture account.', v_user_email;
  END IF;

  -- (c) the profile must exist, be named ZZSMOKE, and still be 'student'
  SELECT p.full_name, p.role INTO v_profile_name, v_profile_role
    FROM public.profiles p
   WHERE p.id = v_teacher_uuid;

  IF v_profile_name IS NULL THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: no profile for %. handle_new_user did not run.', v_teacher_uuid;
  END IF;
  IF v_profile_name NOT ILIKE 'ZZSMOKE%' THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: profile name "%" is not a ZZSMOKE fixture.', v_profile_name;
  END IF;
  IF v_profile_role <> 'student' THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: profile role is "%", expected "student". '
      'Refusing to touch an account that is not a pristine fixture.', v_profile_role;
  END IF;

  -- (d) this account must own nothing at all - a pristine fixture only.
  --     Any prior record means this is not a freshly created account, so the
  --     promotion is refused rather than guessed at.
  SELECT (SELECT count(*) FROM public.exams            WHERE created_by = v_teacher_uuid)
       + (SELECT count(*) FROM public.questions       WHERE created_by = v_teacher_uuid)
       + (SELECT count(*) FROM public.exam_attempts   WHERE student_id = v_teacher_uuid)
       + (SELECT count(*) FROM public.exam_assignments WHERE student_id = v_teacher_uuid)
       + (SELECT count(*) FROM public.security_events  WHERE student_id = v_teacher_uuid)
    INTO v_n;
  IF v_n <> 0 THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: fixture account already owns % record(s).', v_n;
  END IF;

  RAISE NOTICE 'ZZSMOKE bootstrap: all checks passed for % (%)', v_user_email, v_teacher_uuid;

  -- ==================================================================
  -- STEP 2 - the single privileged write, transaction-local context
  -- ==================================================================
  -- is_local = true -> the setting dies with this transaction.
  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  UPDATE public.profiles
     SET role = 'teacher'
   WHERE id = v_teacher_uuid
     AND full_name ILIKE 'ZZSMOKE%'
     AND role = 'student';

  GET DIAGNOSTICS v_n = ROW_COUNT;
  IF v_n <> 1 THEN
    RAISE EXCEPTION 'BOOTSTRAP ABORTED: expected to update 1 profile, updated %.', v_n;
  END IF;

  -- Mirror the role into auth metadata so any JWT-metadata consumer agrees
  -- with the database. The database remains the authority.
  UPDATE auth.users
     SET raw_app_meta_data = COALESCE(raw_app_meta_data, '{}'::jsonb)
                            || jsonb_build_object('role', 'teacher'),
         raw_user_meta_data = COALESCE(raw_user_meta_data, '{}'::jsonb)
                            || jsonb_build_object('role', 'teacher')
   WHERE id = v_teacher_uuid;

  -- ==================================================================
  -- STEP 3 - verify
  -- ==================================================================
  SELECT p.role INTO v_profile_role FROM public.profiles p WHERE p.id = v_teacher_uuid;
  IF v_profile_role <> 'teacher' THEN
    RAISE EXCEPTION 'BOOTSTRAP FAILED: role is still "%".', v_profile_role;
  END IF;

  RAISE NOTICE 'ZZSMOKE bootstrap: SUCCESS. % is now teacher.', v_user_email;
END $$;


-- ====================================================================
-- POST-BOOTSTRAP VERIFICATION (read-only; run separately)
-- ====================================================================
-- SELECT p.id, u.email, p.full_name, p.role,
--        u.raw_app_meta_data->>'role' AS jwt_role
--   FROM public.profiles p JOIN auth.users u ON u.id = p.id
--  WHERE p.id = '<paste v_teacher_uuid here>'::uuid;
--
-- Expect exactly one row: role = 'teacher', jwt_role = 'teacher'.
--
-- Also confirm no REAL account was touched:
-- SELECT role, count(*) FROM public.profiles GROUP BY role ORDER BY role;
-- Expect: student = 6, teacher = 2   (6 real + 1 ZZSMOKE student + 1 ZZSMOKE teacher)
--          before cleanup; student = 5, teacher = 1 after cleanup.