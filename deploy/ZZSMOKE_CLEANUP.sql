-- ====================================================================
-- QuizEazz ZZSMOKE fixture cleanup  -- APPLICATION RECORDS ONLY
-- ====================================================================
-- PURPOSE
--   Remove ONLY the public-schema records created by the ZZSMOKE fixture,
--   and prove it had the right to do so before deleting a single row.
--
--   The two auth identities are NOT deleted here. This script deliberately
--   stops at public.profiles. Removing the auth users is a separate step that
--   uses the supported Supabase Auth Admin API (see STEP 5 below and
--   scripts/zzsmoke_delete_auth_users.mjs). Nothing in this file touches
--   auth.users, auth.identities or any other auth internal table.
--
-- WHY auth DELETION MUST COME LAST
--   public.profiles -> auth.users is ON DELETE CASCADE and cascades from the
--   PARENT (auth.users) down to the CHILD (profiles). Deleting an auth user
--   therefore tries to remove its profile. But these are all NO ACTION:
--       exams.created_by          -> profiles
--       questions.created_by      -> profiles
--       exam_attempts.student_id  -> profiles
--       security_events.student_id-> profiles
--       security_events.exam_id   -> exams / questions
--       exam_attempts.exam_id     -> exams
--       answers.question_id       -> questions
--   so admin.deleteUser() FAILS with a foreign-key violation if any fixture
--   application record still references those profiles. Application rows must
--   therefore be deleted FIRST and the auth identities LAST.
--
-- PARTIAL-SETUP SAFE
--   Setup can stop at any stage (accounts created but teacher never promoted;
--   exam created but never published; attempt never started). Each fixture
--   entity is classified as ABSENT / OWNED / VIOLATION:
--     ABSENT   -> skipped, nothing to do
--     OWNED    -> deleted
--     VIOLATION-> HARD ABORT, nothing deleted
--   This script therefore runs correctly after a partial setup, and is safe
--   to re-run (it is idempotent: a second run finds everything absent and
--   deletes nothing).
--
-- SAFETY MODEL
--   1. MANIFEST-ONLY. Every DELETE is keyed on an explicit UUID supplied by
--      the operator. No DELETE without an exact-id WHERE clause, no LIKE
--      based delete, no TRUNCATE.
--   2. VERIFY-THEN-DELETE, ACROSS THE WHOLE TREE. Every entity is classified
--      before the first DELETE. One violation aborts the entire transaction.
--   3. REFUSE-ON-DRIFT. A present-but-not-ZZSMOKE record, or any fixture
--      record that touches real data, is a hard stop - never a warning.
--   4. TRANSACTION-LOCAL ADMIN CONTEXT, SET LAST. See STEP 3.
--
-- DEPENDENCY ORDER (from live FK metadata; del codes: c=CASCADE, a=NO ACTION)
--   answers              -> exam_attempts  CASCADE
--   answers              -> questions      NO ACTION
--   security_events      -> exam_attempts  CASCADE
--   security_events      -> exams          NO ACTION
--   security_events      -> questions      NO ACTION
--   security_events      -> profiles       NO ACTION
--   exam_attempts        -> exams          NO ACTION
--   exam_attempts        -> profiles       NO ACTION
--   exams                -> profiles       NO ACTION
--   questions            -> profiles       NO ACTION
--   exam_questions       -> exams/questions  CASCADE
--   exam_assignments     -> exams/profiles   CASCADE
--   programming_test_cases -> questions     CASCADE
--   profiles             -> auth.users     CASCADE   (parent -> child)
--
-- OPERATOR MUST REPLACE EVERY ::uuid LITERAL BELOW.
-- ====================================================================

DO $$
DECLARE
  -- ---- MANIFEST: paste the ids captured at fixture creation -------------
  v_teacher_id    UUID := '00000000-0000-0000-0000-000000000001'::uuid;
  v_student_id    UUID := '00000000-0000-0000-0000-000000000002'::uuid;
  v_exam_id       UUID := '00000000-0000-0000-0000-000000000003'::uuid;
  v_question_id   UUID := '00000000-0000-0000-0000-000000000004'::uuid;
  v_attempt_id    UUID := '00000000-0000-0000-0000-000000000005'::uuid;

  v_n        INT;
  v_deleted  INT := 0;
  v_marker   CONSTANT TEXT := 'ZZSMOKE';

  v_exam_present      BOOLEAN;
  v_question_present  BOOLEAN;
  v_attempt_present   BOOLEAN;
  v_teacher_present   BOOLEAN;
  v_student_present   BOOLEAN;
BEGIN
  -- ==================================================================
  -- STEP 0 - manifest sanity
  -- ==================================================================
  IF v_teacher_id IS NULL OR v_student_id IS NULL THEN
    RAISE EXCEPTION 'CLEANUP ABORTED: the two account ids are mandatory and must be set.';
  END IF;
  IF v_teacher_id = v_student_id THEN
    RAISE EXCEPTION 'CLEANUP ABORTED: teacher and student ids are identical.';
  END IF;
  IF v_exam_id IS NULL AND v_question_id IS NULL AND v_attempt_id IS NULL THEN
    RAISE EXCEPTION 'CLEANUP ABORTED: no exam/question/attempt ids supplied, and '
      'account ids alone are never used to infer or select content.';
  END IF;

  RAISE NOTICE 'ZZSMOKE cleanup: classifying fixture entities (nothing deleted yet)';

  -- ==================================================================
  -- STEP 1 - classify every entity: ABSENT / OWNED / VIOLATION
  -- ==================================================================

  -- 1.1 accounts ---------------------------------------------------------
  SELECT count(*) INTO v_n FROM public.profiles
   WHERE id = v_teacher_id AND full_name ILIKE v_marker || '%';
  v_teacher_present := (v_n = 1);
  IF v_n > 1 THEN
    RAISE EXCEPTION 'CLEANUP ABORTED: % profile rows match teacher id %.', v_n, v_teacher_id;
  END IF;
  -- present-but-not-ZZSMOKE is a violation, never a silent skip
  IF v_n = 0 AND EXISTS (SELECT 1 FROM public.profiles WHERE id = v_teacher_id) THEN
    RAISE EXCEPTION 'CLEANUP ABORTED: teacher id % exists but is NOT a ZZSMOKE fixture.', v_teacher_id;
  END IF;

  SELECT count(*) INTO v_n FROM public.profiles
   WHERE id = v_student_id AND full_name ILIKE v_marker || '%';
  v_student_present := (v_n = 1);
  IF v_n > 1 THEN
    RAISE EXCEPTION 'CLEANUP ABORTED: % profile rows match student id %.', v_n, v_student_id;
  END IF;
  IF v_n = 0 AND EXISTS (SELECT 1 FROM public.profiles WHERE id = v_student_id) THEN
    RAISE EXCEPTION 'CLEANUP ABORTED: student id % exists but is NOT a ZZSMOKE fixture.', v_student_id;
  END IF;

  -- 1.2 exam -------------------------------------------------------------
  v_exam_present := EXISTS (SELECT 1 FROM public.exams WHERE id = v_exam_id);
  IF v_exam_present THEN
    SELECT count(*) INTO v_n FROM public.exams
     WHERE id = v_exam_id AND title LIKE v_marker || '%';
    IF v_n <> 1 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: exam % is NOT a ZZSMOKE fixture.', v_exam_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.profiles WHERE id =
                     (SELECT created_by FROM public.exams WHERE id = v_exam_id)) THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: exam % has no owning profile.', v_exam_id;
    END IF;
  END IF;

  -- 1.3 question ---------------------------------------------------------
  v_question_present := EXISTS (SELECT 1 FROM public.questions WHERE id = v_question_id);
  IF v_question_present THEN
    SELECT count(*) INTO v_n FROM public.questions
     WHERE id = v_question_id AND title LIKE v_marker || '%';
    IF v_n <> 1 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: question % is NOT a ZZSMOKE fixture.', v_question_id;
    END IF;
    -- SHARED-QUESTION SAFEGUARD: our question must be in no other paper.
    SELECT count(*) INTO v_n FROM public.exam_questions
     WHERE question_id = v_question_id
       AND (v_exam_id IS NULL OR exam_id <> v_exam_id);
    IF v_n <> 0 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: question % is shared with % other paper(s).',
        v_question_id, v_n;
    END IF;
  END IF;

  -- 1.4 attempt ----------------------------------------------------------
  -- Strictly manifest-driven. If the fixture exam has an attempt but the
  -- manifest id does not match it, we ABORT rather than fall back to
  -- "the one attempt on this exam" (that would be implicit selection).
  IF v_attempt_id IS NOT NULL THEN
    v_attempt_present := EXISTS (SELECT 1 FROM public.exam_attempts WHERE id = v_attempt_id);
  ELSE
    v_attempt_present := FALSE;
  END IF;
  IF v_attempt_present AND NOT EXISTS (
       SELECT 1 FROM public.exam_attempts
        WHERE id = v_attempt_id AND student_id = v_student_id) THEN
    RAISE EXCEPTION 'CLEANUP ABORTED: attempt % is not owned by the fixture student %.',
      v_attempt_id, v_student_id;
  END IF;
  -- Any attempt on our exam must be exactly the recorded one.
  IF v_exam_present THEN
    SELECT count(*) INTO v_n FROM public.exam_attempts WHERE exam_id = v_exam_id;
    IF v_n > 1 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: exam % has % attempts; expected at most 1.', v_exam_id, v_n;
    ELSIF v_n = 1 AND NOT EXISTS (
            SELECT 1 FROM public.exam_attempts
             WHERE exam_id = v_exam_id AND id = v_attempt_id
               AND student_id = v_student_id) THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: exam % has an attempt that does not match '
        'the manifest (attempt_id=%, student_id=%). Refusing to infer it.', v_exam_id,
        v_attempt_id, v_student_id;
    END IF;
  END IF;

  -- 1.5 the fixture exam must touch nothing real -------------------------
  IF v_exam_present THEN
    SELECT count(*) INTO v_n FROM public.exam_questions
     WHERE exam_id = v_exam_id
       AND (v_question_id IS NULL OR question_id <> v_question_id);
    IF v_n <> 0 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: exam % paper references % question(s) outside the manifest.',
        v_exam_id, v_n;
    END IF;

    SELECT count(*) INTO v_n FROM public.exam_assignments
     WHERE exam_id = v_exam_id AND student_id <> v_student_id;
    IF v_n <> 0 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: exam % is assigned to % other student(s).',
        v_exam_id, v_n;
    END IF;
  END IF;

  -- 1.6 security events must not touch real data, and must not tie the
  --     fixture profiles to anything outside this fixture (that would
  --     block the profile delete AND mean we are looking at real activity)
  IF v_teacher_id IS NOT NULL THEN
    SELECT count(*) INTO v_n FROM public.security_events
     WHERE student_id IN (v_teacher_id, v_student_id)
       AND (v_exam_id IS NULL OR exam_id IS DISTINCT FROM v_exam_id);
    IF v_n <> 0 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: % security_event(s) reference a fixture account but not the fixture exam.',
        v_n;
    END IF;
  END IF;

  RAISE NOTICE 'ZZSMOKE cleanup: classification OK (exam=% question=% attempt=% accounts=%)',
    v_exam_present, v_question_present, v_attempt_present,
    (v_teacher_present OR v_student_present);

  -- ==================================================================
  -- STEP 2 - dependency-order reachability proof (no writes)
  -- ------------------------------------------------------------------
  -- security_events.student_id -> profiles is NO ACTION, so an event that
  -- names a fixture account would block the profile delete. STEP 1.6 proves
  -- such events do not exist.
  IF (v_teacher_present OR v_student_present) THEN
    SELECT count(*) INTO v_n FROM public.security_events
     WHERE student_id IN (v_teacher_id, v_student_id)
       AND (v_exam_id IS NULL OR exam_id IS DISTINCT FROM v_exam_id);
    IF v_n <> 0 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: % blocking security_event(s) on fixture profiles.', v_n;
    END IF;
  END IF;

  -- ==================================================================
  -- STEP 3 - administrative context, TRANSACTION-LOCAL, SET LAST
  -- ------------------------------------------------------------------
  -- These three production triggers fire on DELETE and would otherwise
  -- raise, because a SQL Editor session has auth.uid() = NULL:
  --   protect_exam_composition_mutation -> RAISES unconditionally
  --   protect_attempt_mutation (DELETE) -> RAISES when auth.uid() IS NULL
  --   check_answer_write_eligibility (DELETE) -> RAISES when auth.uid() IS NULL
  --
  -- set_config(..., is_local => true) is TRANSACTION-LOCAL: the setting is
  -- discarded when this transaction ends. It cannot leak to another session,
  -- another connection, or a later statement in this script. It is set only
  -- AFTER every classification and reachability gate above has passed, and it
  -- is scoped to the deletes that immediately follow.
  --
  -- This is the same mechanism the platform's own RPCs use. No client-facing
  -- control is weakened: the trigger still blocks every candidate-initiated
  -- path, which is what it exists to protect. Cleanup is administrative.
  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  RAISE NOTICE 'ZZSMOKE cleanup: validation passed, deleting';

  -- ==================================================================
  -- STEP 4 - delete in dependency order, only entities classified OWNED
  -- ==================================================================

  -- 4.1 every answer on the fixture attempt (scoped by the attempt id alone,
  --     so a duplicated answer row cannot survive as an orphan)
  IF v_attempt_present THEN
    DELETE FROM public.answers WHERE attempt_id = v_attempt_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_deleted := v_deleted + v_n;
    RAISE NOTICE '  answers: %', v_n;
  END IF;

  -- 4.2 security events referencing the fixture by any of its keys
  IF v_exam_id IS NOT NULL OR v_question_id IS NOT NULL OR v_attempt_id IS NOT NULL THEN
    DELETE FROM public.security_events
     WHERE (v_attempt_id IS NOT NULL AND attempt_id = v_attempt_id)
        OR (v_exam_id    IS NOT NULL AND exam_id     = v_exam_id)
        OR (v_question_id IS NOT NULL AND question_id = v_question_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_deleted := v_deleted + v_n;
    RAISE NOTICE '  security_events: %', v_n;
  END IF;

  -- 4.3 the attempt (blocks both exams and profiles)
  IF v_attempt_present THEN
    DELETE FROM public.exam_attempts
     WHERE id = v_attempt_id AND exam_id = v_exam_id AND student_id = v_student_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: expected to delete 1 attempt, deleted %.', v_n;
    END IF;
    v_deleted := v_deleted + v_n;
    RAISE NOTICE '  exam_attempts: %', v_n;
  END IF;

  -- 4.4 exam-owned links
  IF v_exam_present THEN
    DELETE FROM public.exam_questions WHERE exam_id = v_exam_id;
    DELETE FROM public.exam_assignments WHERE exam_id = v_exam_id;
  END IF;

  -- 4.5 the exam, then the question and its test cases
  IF v_exam_present THEN
    DELETE FROM public.exams WHERE id = v_exam_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: expected to delete 1 exam, deleted %.', v_n;
    END IF;
    RAISE NOTICE '  exams: %', v_n;
  END IF;

  IF v_question_present THEN
    DELETE FROM public.programming_test_cases WHERE question_id = v_question_id;
    DELETE FROM public.questions WHERE id = v_question_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: expected to delete 1 question, deleted %.', v_n;
    END IF;
    RAISE NOTICE '  questions: %', v_n;
  END IF;

  -- 4.6 profiles (public schema only - this is NOT an auth internal table)
  IF v_teacher_present THEN
    DELETE FROM public.profiles
     WHERE id = v_teacher_id AND full_name ILIKE v_marker || '%';
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: expected to delete 1 teacher profile, deleted %.', v_n;
    END IF;
  END IF;
  IF v_student_present THEN
    DELETE FROM public.profiles
     WHERE id = v_student_id AND full_name ILIKE v_marker || '%';
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN
      RAISE EXCEPTION 'CLEANUP ABORTED: expected to delete 1 student profile, deleted %.', v_n;
    END IF;
  END IF;
  RAISE NOTICE '  profiles: %', (v_teacher_present::int + v_student_present::int);

  -- ==================================================================
  -- STEP 5 - post-verify: no fixture id survives anywhere in public
  -- ==================================================================
  SELECT count(*) INTO v_n FROM public.answers
   WHERE (v_attempt_id IS NOT NULL AND attempt_id = v_attempt_id)
      OR (v_question_id IS NOT NULL AND question_id = v_question_id);
  IF v_n <> 0 THEN RAISE EXCEPTION 'POST-CHECK FAILED: % answer(s) remain.', v_n; END IF;

  SELECT count(*) INTO v_n FROM public.exam_attempts
   WHERE (v_attempt_id IS NOT NULL AND id = v_attempt_id)
      OR (v_exam_id    IS NOT NULL AND exam_id = v_exam_id);
  IF v_n <> 0 THEN RAISE EXCEPTION 'POST-CHECK FAILED: % attempt(s) remain.', v_n; END IF;

  SELECT count(*) INTO v_n FROM public.security_events
   WHERE (v_attempt_id  IS NOT NULL AND attempt_id  = v_attempt_id)
      OR (v_exam_id     IS NOT NULL AND exam_id     = v_exam_id)
      OR (v_question_id IS NOT NULL AND question_id = v_question_id);
  IF v_n <> 0 THEN RAISE EXCEPTION 'POST-CHECK FAILED: % security_event(s) remain.', v_n; END IF;

  SELECT count(*) INTO v_n FROM public.exams      WHERE id = v_exam_id;
  IF v_n <> 0 THEN RAISE EXCEPTION 'POST-CHECK FAILED: exam remains.'; END IF;

  SELECT count(*) INTO v_n FROM public.questions WHERE id = v_question_id;
  IF v_n <> 0 THEN RAISE EXCEPTION 'POST-CHECK FAILED: question remains.'; END IF;

  SELECT count(*) INTO v_n FROM public.programming_test_cases WHERE question_id = v_question_id;
  IF v_n <> 0 THEN RAISE EXCEPTION 'POST-CHECK FAILED: % test case(s) remain.', v_n; END IF;

  SELECT count(*) INTO v_n FROM public.exam_questions
   WHERE exam_id = v_exam_id OR question_id = v_question_id;
  IF v_n <> 0 THEN RAISE EXCEPTION 'POST-CHECK FAILED: % paper link(s) remain.', v_n; END IF;

  SELECT count(*) INTO v_n FROM public.exam_assignments WHERE exam_id = v_exam_id;
  IF v_n <> 0 THEN RAISE EXCEPTION 'POST-CHECK FAILED: % assignment(s) remain.', v_n; END IF;

  SELECT count(*) INTO v_n FROM public.profiles WHERE id IN (v_teacher_id, v_student_id);
  IF v_n <> 0 THEN RAISE EXCEPTION 'POST-CHECK FAILED: % profile(s) remain.', v_n; END IF;

  RAISE NOTICE 'ZZSMOKE cleanup: APPLICATION RECORDS COMPLETE (% row(s) deleted).', v_deleted;
  RAISE NOTICE 'ZZSMOKE cleanup: the two AUTH identities still exist. Now run '
               'scripts/zzsmoke_delete_auth_users.mjs (Supabase Auth Admin API).';
END $$;


-- ====================================================================
-- POST-CLEANUP SQL VERIFICATION (read-only; run separately)
-- ====================================================================
-- SELECT p.id, u.email, p.full_name, p.role
--   FROM public.profiles p JOIN auth.users u ON u.id = p.id
--  WHERE p.id IN ('<teacher_uuid>','<student_uuid>')::uuid[];
-- Expect: ZERO rows.
--
-- SELECT count(*) FROM auth.users
--  WHERE id IN ('<teacher_uuid>','<student_uuid>')::uuid;
-- Expect: 2 immediately after this script; 0 after the Admin API step.
--
-- Then run the byte-exact whole-database comparison:
--   python scripts/zzsmoke_baseline_fingerprint.py compare
-- Expect: every table "identical", exit 0.