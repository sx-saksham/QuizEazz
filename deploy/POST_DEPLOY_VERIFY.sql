-- QuizEazz: post-deployment verification for migration 20
-- ALL statements below are SELECTs. Run them one at a time and read the result.

-- V1. The guard now locks the answer row.  EXPECT: t
SELECT prosrc LIKE '%FOR UPDATE%' AS has_for_update
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND p.proname = 'quizeazz_apply_judge0_verdict';

-- V2. Signature unchanged.  EXPECT: boolean
SELECT pg_get_function_result(p.oid) AS returns,
       pg_get_function_identity_arguments(p.oid) AS identity_args
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND p.proname = 'quizeazz_apply_judge0_verdict';

-- V3. SECURITY DEFINER + pinned search_path.
--     EXPECT: t | {search_path=public, pg_temp}
SELECT p.prosecdef AS security_definer, p.proconfig
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND p.proname = 'quizeazz_apply_judge0_verdict';

-- V4. Privileges must be UNCHANGED: only service_role may execute.
--     EXPECT: f | f | f | t
SELECT has_function_privilege('anon', p.oid, 'EXECUTE')        AS anon,
       has_function_privilege('authenticated', p.oid, 'EXECUTE') AS authenticated,
       has_function_privilege('public', p.oid, 'EXECUTE')        AS public,
       has_function_privilege('service_role', p.oid, 'EXECUTE')  AS service_role
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND p.proname = 'quizeazz_apply_judge0_verdict';

-- V5. The function is still the only writer and grading is idle.
--     EXPECT: true (no attempt should be mid-grade outside a test window)
SELECT NOT EXISTS (SELECT 1 FROM public.exam_attempts
                    WHERE status = 'evaluating') AS no_attempt_evaluating;
