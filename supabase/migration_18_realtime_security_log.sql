-- ====================================================================
-- QUIZEAZZ — MIGRATION 18: REALTIME FOR THE FACULTY SECURITY LOG
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..17. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   Two screens were stale until the operator pressed F5:
--     * a candidate's "My Exams" list after faculty assigned or rescheduled an
--       examination, and
--     * the faculty Security Event Log after a new proctoring event landed.
--
--   This migration addresses the half of that which needs DATABASE support.
--   Inspection of the live database found that NO table at all was a member of
--   the `supabase_realtime` publication, so `postgres_changes` was inert: a
--   subscription could connect and never receive an event.
--
-- WHY ONLY security_events IS PUBLISHED HERE
--   Supabase Realtime applies the subscriber's RLS policies to every
--   postgres_changes delivery, so a table is only publishable *for the roles
--   that may already read it*:
--
--     * security_events carries a SELECT policy for faculty
--       ("Faculty read security events"). Adding it to the publication lets a
--       teacher receive their own audit rows. Students and anon have NO policy
--       on that table, so RLS still delivers nothing to them — publishing it
--       does NOT widen who can read the audit log.
--
--     * exams and exam_assignments are deliberately NOT published. `exams` has a
--       faculty-only SELECT policy and `exam_assignments` has no policy at all,
--       so a candidate could never be sent these rows without first granting
--       them direct table reads — which is exactly what the SECURITY DEFINER
--       RPC-first architecture (get_student_exam_overview) exists to prevent.
--       Candidate-side freshness is therefore driven by a payload-free
--       broadcast signal from the teacher client plus a local boundary timer,
--       and it still refetches through the same RPC.
--
-- SCOPE
--   One publication membership. No table created/altered/dropped. No RLS
--   policy added, changed or removed. No privilege granted. No function
--   created. No data touched.
--
-- IDEMPOTENT: re-running is a no-op rather than an error, so this file is safe
--   to apply twice.
-- ====================================================================

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename  = 'security_events'
  ) THEN
    EXECUTE 'ALTER PUBLICATION supabase_realtime ADD TABLE public.security_events';
  END IF;
END;
$$;


-- ====================================================================
-- VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- B1. security_events is now published (expect exactly one row):
--   SELECT schemaname, tablename
--   FROM pg_publication_tables
--   WHERE pubname = 'supabase_realtime';
--
-- B2. NO candidate-readable surface was added. Both must still be empty for
--     the 'authenticated' role, and exam_assignments must still have no
--     SELECT policy at all:
--   SELECT table_name, privilege_type
--   FROM information_schema.role_table_grants
--   WHERE table_schema = 'public'
--     AND table_name IN ('exams','exam_assignments')
--     AND grantee IN ('anon','authenticated');
--   -- (expect no rows)
--
--   SELECT c.relname, p.polname, p.polcmd
--   FROM pg_policy p
--   JOIN pg_class c ON c.oid = p.polrelid
--   JOIN pg_namespace n ON n.oid = c.relnamespace
--   WHERE n.nspname = 'public'
--     AND c.relname IN ('exams','exam_assignments');
--   -- (expect only the faculty read policy on exams; none on exam_assignments)
--
-- B3. The destructive clear path is unchanged and still faculty-only
--     (expect anon=false, auth=true):
--   SELECT p.proname,
--     has_function_privilege('anon', p.oid, 'EXECUTE')         AS anon,
--     has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth
--   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--   WHERE n.nspname = 'public' AND p.proname = 'teacher_clear_security_events';
-- ====================================================================


-- ====================================================================
-- END OF MIGRATION 18
-- ====================================================================