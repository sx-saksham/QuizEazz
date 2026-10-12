-- ====================================================================
-- QUIZEAZZ — POST-MIGRATION VERIFICATION (single statement, read-only)
-- --------------------------------------------------------------------
-- PASTE THE WHOLE FILE BELOW INTO THE SUPABASE SQL EDITOR AND PRESS RUN.
-- Exactly ONE result grid is returned with three columns:
--
--     check_name | status | details
--
-- status is PASS or FAIL. Nothing is created, altered or written.
-- No psql backslash commands. Do not add any.
--
-- SCOPE: verifies the state left behind by migration 4. Read the
-- `details` column of any FAIL row; it names the offending objects.
--
-- SCHEMA: written against the verified live schema. This query does not
-- reference exams.total_marks, answers.updated_at or
-- exam_questions.created_at, because none of those columns exist.
-- ====================================================================

WITH expected(name, is_client_rpc) AS (
    VALUES
    -- 29 caller-facing RPCs: authenticated SHOULD be able to execute these
    ('submit_exam_attempt',                    true),
    ('grade_mcq_attempt',                      true),
    ('start_or_get_exam_attempt',              true),
    ('get_exam_questions',                     true),
    ('get_student_attempt_review',             true),
    ('get_student_exam_overview',              true),
    ('get_student_results',                    true),
    ('get_student_exam_answers',               true),
    ('save_exam_answer',                       true),
    ('pause_exam_attempt',                     true),
    ('resume_exam_attempt',                    true),
    ('record_security_event',                  true),
    ('get_my_profile',                         true),
    ('teacher_create_question',                true),
    ('teacher_get_questions',                  true),
    ('teacher_get_exams',                      true),
    ('teacher_create_exam',                    true),
    ('teacher_get_candidates',                 true),
    ('teacher_get_overview_stats',             true),
    ('teacher_get_exam_assignments',           true),
    ('teacher_set_exam_assignments',           true),
    ('teacher_get_exam_paper',                 true),
    ('teacher_set_exam_paper',                 true),
    ('teacher_set_exam_status',                true),
    ('teacher_set_exam_schedule',              true),
    ('teacher_get_attempts',                   true),
    ('teacher_get_attempt_detail',             true),
    ('teacher_get_security_events',            true),
    ('admin_get_profiles',                     true),
    -- 8 internal engines: NO client role may execute these directly
    ('quizeazz_grade_objective_marks',         false),
    ('quizeazz_assert_faculty',                false),
    ('quizeazz_assert_composition_editable',   false),
    ('protect_exam_composition_mutation',      false),
    ('protect_attempt_mutation',               false),
    ('protect_profile_role_escalation',        false),
    ('check_answer_write_eligibility',         false),
    ('handle_new_user',                        false)
),
resolved AS (
    SELECT
        e.name,
        e.is_client_rpc,
        p.oid,
        p.prosecdef
    FROM expected e
    LEFT JOIN pg_proc p
           ON p.proname = e.name
          AND p.pronamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
),
mcq_keys AS (
    SELECT
        q.id,
        q.title,
        jsonb_typeof(q.options) AS options_type,
        (SELECT count(*) FROM jsonb_array_elements(
             CASE WHEN jsonb_typeof(q.options) = 'array' THEN q.options ELSE '[]'::jsonb END
         ) e WHERE COALESCE((e->>'is_correct')::boolean, false) = true) AS correct_flags
    FROM public.questions q
    WHERE q.question_type = 'mcq'
),
dup_assignments AS (
    SELECT exam_id, student_id, count(*) AS n
    FROM public.exam_assignments
    GROUP BY exam_id, student_id
    HAVING count(*) > 1
),
dup_paper AS (
    SELECT exam_id, question_id, count(*) AS n
    FROM public.exam_questions
    GROUP BY exam_id, question_id
    HAVING count(*) > 1
),
score_anomalies AS (
    SELECT id, exam_id, student_id, status, score, max_score
    FROM public.exam_attempts
    WHERE (max_score IS NOT NULL AND score IS NOT NULL AND score > max_score)
       OR score < 0
),
ungraded AS (
    SELECT att.id, att.exam_id, att.student_id, att.status
    FROM public.exam_attempts att
    WHERE att.status IN ('submitted', 'timed_out', 'evaluating')
       OR (att.status = 'evaluated' AND att.score IS NULL)
),
rls_expected AS (
    SELECT t.name
    FROM (VALUES
        ('questions'), ('exams'), ('exam_questions'), ('exam_assignments'),
        ('exam_attempts'), ('answers'), ('profiles'), ('security_events')
    ) AS t(name)
),
rls_tables AS (
    SELECT
        e.name AS table_name,
        c.relname IS NOT NULL AS table_exists,
        c.relrowsecurity AS rls_enabled
    FROM rls_expected e
    LEFT JOIN pg_class c
           ON c.relname = e.name
          AND c.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
          AND c.relkind = 'r'
),
write_policies AS (
    SELECT tablename, policyname, cmd
    FROM pg_policies
    WHERE schemaname = 'public'
      AND cmd IN ('INSERT', 'UPDATE', 'DELETE', 'ALL')
)
SELECT * FROM (
    -- 1 -----------------------------------------------------------------
    SELECT
        1 AS sort_order,
        '1. RPC presence (37 expected)'::text AS check_name,
        CASE
            WHEN (SELECT count(*) FROM resolved WHERE oid IS NOT NULL) = 37
            THEN 'PASS' ELSE 'FAIL'
        END AS status,
        CASE
            WHEN (SELECT count(*) FROM resolved WHERE oid IS NOT NULL) = 37
            THEN 'all 37 present (29 client RPC + 8 internal)'
            ELSE 'missing: ' || COALESCE(
                (SELECT string_agg(name, ', ' ORDER BY name)
                 FROM resolved WHERE oid IS NULL),
                'unknown')
            || ' | present: ' ||
            (SELECT count(*) FROM resolved WHERE oid IS NOT NULL)::text
        END AS details

    -- 2 -----------------------------------------------------------------
    UNION ALL
    SELECT
        2,
        '2. RPC SECURITY DEFINER state'::text,
        CASE
            WHEN (SELECT count(*) FROM resolved
                   WHERE oid IS NOT NULL AND prosecdef IS NOT TRUE) = 0
            THEN 'PASS' ELSE 'FAIL'
        END,
        CASE
            WHEN (SELECT count(*) FROM resolved
                   WHERE oid IS NOT NULL AND prosecdef IS NOT TRUE) = 0
            THEN 'all present functions are SECURITY DEFINER'
            ELSE 'not SECURITY DEFINER: ' || COALESCE(
                (SELECT string_agg(name, ', ' ORDER BY name)
                 FROM resolved WHERE oid IS NOT NULL AND prosecdef IS NOT TRUE),
                'unknown')
        END

    -- 3 -----------------------------------------------------------------
    UNION ALL
    SELECT
        3,
        '3. anon EXECUTE on any RPC (must be none)'::text,
        CASE
            WHEN (SELECT count(*) FROM resolved
                   WHERE oid IS NOT NULL
                     AND has_function_privilege('anon', oid, 'EXECUTE')) = 0
            THEN 'PASS' ELSE 'FAIL'
        END,
        CASE
            WHEN (SELECT count(*) FROM resolved
                   WHERE oid IS NOT NULL
                     AND has_function_privilege('anon', oid, 'EXECUTE')) = 0
            THEN 'anon cannot execute any QuizEazz function'
            ELSE 'anon CAN execute: ' || COALESCE(
                (SELECT string_agg(name, ', ' ORDER BY name)
                 FROM resolved
                 WHERE oid IS NOT NULL
                   AND has_function_privilege('anon', oid, 'EXECUTE')),
                'unknown')
        END

    -- 4 -----------------------------------------------------------------
    UNION ALL
    SELECT
        4,
        '4. authenticated EXECUTE correct per role'::text,
        CASE
            WHEN (SELECT count(*) FROM resolved
                   WHERE oid IS NOT NULL
                     AND ( (is_client_rpc AND NOT has_function_privilege('authenticated', oid, 'EXECUTE'))
                        OR (NOT is_client_rpc AND has_function_privilege('authenticated', oid, 'EXECUTE')) )
                 ) = 0
            THEN 'PASS' ELSE 'FAIL'
        END,
        CASE
            WHEN (SELECT count(*) FROM resolved
                   WHERE oid IS NOT NULL
                     AND ( (is_client_rpc AND NOT has_function_privilege('authenticated', oid, 'EXECUTE'))
                        OR (NOT is_client_rpc AND has_function_privilege('authenticated', oid, 'EXECUTE')) )
                 ) = 0
            THEN '29 client RPCs granted, 8 internal engines denied'
            ELSE 'wrongly denied (client RPC): ' || COALESCE(
                (SELECT string_agg(name, ', ' ORDER BY name)
                 FROM resolved
                 WHERE oid IS NOT NULL AND is_client_rpc
                   AND NOT has_function_privilege('authenticated', oid, 'EXECUTE')),
                'none')
                || ' | wrongly allowed (internal): ' || COALESCE(
                (SELECT string_agg(name, ', ' ORDER BY name)
                 FROM resolved
                 WHERE oid IS NOT NULL AND NOT is_client_rpc
                   AND has_function_privilege('authenticated', oid, 'EXECUTE')),
                'none')
        END

    -- 5 -----------------------------------------------------------------
    UNION ALL
    SELECT
        5,
        '5. MCQ answer-key integrity'::text,
        CASE
            WHEN (SELECT count(*) FROM mcq_keys WHERE correct_flags <> 1) = 0
            THEN 'PASS' ELSE 'FAIL'
        END,
        CASE
            WHEN (SELECT count(*) FROM mcq_keys WHERE correct_flags <> 1) = 0
            THEN 'every MCQ carries exactly 1 key (' ||
                 (SELECT count(*) FROM mcq_keys)::text || ' MCQ total)'
            ELSE COALESCE(
                (SELECT string_agg(title || ' [' || id || '] flags=' || correct_flags,
                                      '; ' ORDER BY title)
                 FROM mcq_keys WHERE correct_flags <> 1),
                'unknown')
        END

    -- 6 -----------------------------------------------------------------
    UNION ALL
    SELECT
        6,
        '6. Duplicate exam assignments'::text,
        CASE WHEN (SELECT count(*) FROM dup_assignments) = 0 THEN 'PASS' ELSE 'FAIL' END,
        CASE
            WHEN (SELECT count(*) FROM dup_assignments) = 0
            THEN 'no duplicate (exam_id, student_id) pairs'
            ELSE (SELECT count(*) FROM dup_assignments)::text || ' duplicate pair(s): ' ||
                 COALESCE((SELECT string_agg('exam=' || exam_id || ' student=' || student_id,
                                             '; ' ORDER BY exam_id, student_id)
                           FROM dup_assignments LIMIT 20), 'unknown')
        END

    -- 7 -----------------------------------------------------------------
    UNION ALL
    SELECT
        7,
        '7. Duplicate exam paper rows'::text,
        CASE WHEN (SELECT count(*) FROM dup_paper) = 0 THEN 'PASS' ELSE 'FAIL' END,
        CASE
            WHEN (SELECT count(*) FROM dup_paper) = 0
            THEN 'no duplicate (exam_id, question_id) pairs'
            ELSE (SELECT count(*) FROM dup_paper)::text || ' duplicate pair(s): ' ||
                 COALESCE((SELECT string_agg('exam=' || exam_id || ' question=' || question_id,
                                             '; ' ORDER BY exam_id, question_id)
                           FROM dup_paper LIMIT 20), 'unknown')
        END

    -- 8 -----------------------------------------------------------------
    UNION ALL
    SELECT
        8,
        '8. Score consistency vs max_score'::text,
        CASE WHEN (SELECT count(*) FROM score_anomalies) = 0 THEN 'PASS' ELSE 'FAIL' END,
        CASE
            WHEN (SELECT count(*) FROM score_anomalies) = 0
            THEN (SELECT count(*) FROM public.exam_attempts)::text ||
                 ' attempt(s); no score exceeds max_score, no negative score'
            ELSE COALESCE(
                (SELECT string_agg('attempt=' || id || ' score=' || score ||
                                          ' max=' || COALESCE(max_score::text, 'null'),
                                   '; ' ORDER BY id)
                 FROM score_anomalies LIMIT 20),
                'unknown')
        END

    -- 9 -----------------------------------------------------------------
    UNION ALL
    SELECT
        9,
        '9. Finalized attempts left ungraded'::text,
        CASE WHEN (SELECT count(*) FROM ungraded) = 0 THEN 'PASS' ELSE 'FAIL' END,
        CASE
            WHEN (SELECT count(*) FROM ungraded) = 0
            THEN 'every finalized attempt carries a score'
            ELSE COALESCE(
                (SELECT string_agg('attempt=' || id || ' status=' || status, '; ' ORDER BY id)
                 FROM ungraded LIMIT 20),
                'unknown')
            || ' | re-grade by resubmitting, or call grade_mcq_attempt as faculty'
        END

    -- 10 ----------------------------------------------------------------
    UNION ALL
    SELECT
        10,
        '10. RLS enabled on all sensitive tables'::text,
        CASE
            WHEN (SELECT count(*) FROM rls_tables WHERE table_exists) = 8
             AND (SELECT count(*) FROM rls_tables WHERE table_exists AND rls_enabled IS NOT TRUE) = 0
            THEN 'PASS' ELSE 'FAIL'
        END,
        CASE
            WHEN (SELECT count(*) FROM rls_tables WHERE table_exists) = 8
             AND (SELECT count(*) FROM rls_tables WHERE table_exists AND rls_enabled IS NOT TRUE) = 0
            THEN 'all 8 tables have RLS enabled; write policies: ' ||
                 (SELECT count(*) FROM write_policies)::text
            ELSE
                'missing: ' || COALESCE(
                    (SELECT string_agg(table_name, ', ' ORDER BY table_name)
                     FROM rls_tables WHERE NOT table_exists), 'none')
                || ' | RLS disabled: ' || COALESCE(
                    (SELECT string_agg(table_name, ', ' ORDER BY table_name)
                     FROM rls_tables WHERE table_exists AND rls_enabled IS NOT TRUE), 'none')
        END
) AS checks
ORDER BY sort_order;

-- ====================================================================
-- END. Single read-only SELECT. No psql commands.
-- ====================================================================
