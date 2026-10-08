-- ====================================================================
-- QUIZEAZZ — MIGRATION 14: FACULTY CLEAR SECURITY LOG
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..13. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   Faculty need a deliberate way to clear the security-event audit log
--   (e.g. between examination sessions). This must be a server-authoritative
--   faculty-gated RPC — never a frontend-only delete, never reachable by
--   students.
--
-- WHAT THIS FILE DOES
--   Adds teacher_clear_security_events(): deletes ALL rows from
--   public.security_events and returns the removed count. Faculty-only
--   (teacher/admin via quizeazz_assert_faculty), authenticated-executable.
--   Students receive 'Access denied' from the faculty guard; anon has no
--   execute grant at all.
--
-- SCOPE
--   No table created/altered/dropped. No RLS change. No other function
--   touched. The destructive scope is exactly one table, chosen explicitly.
--
-- IDEMPOTENT: CREATE OR REPLACE (new signature, no DROP needed).
-- ====================================================================

CREATE OR REPLACE FUNCTION public.teacher_clear_security_events()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_caller_id UUID;
  v_removed INT := 0;
BEGIN
  v_caller_id := auth.uid();
  IF v_caller_id IS NULL THEN
    RAISE EXCEPTION 'Authentication required.';
  END IF;
  PERFORM public.quizeazz_assert_faculty(v_caller_id);

  PERFORM set_config('quizeazz.authoritative_context', 'true', true);

  WITH removed AS (
    DELETE FROM public.security_events
    RETURNING id
  )
  SELECT COUNT(*)::INT INTO v_removed FROM removed;

  RETURN jsonb_build_object(
    'cleared', TRUE,
    'removed', v_removed
  );
END;
$$;

REVOKE ALL ON FUNCTION public.teacher_clear_security_events() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.teacher_clear_security_events() FROM anon;
GRANT  EXECUTE ON FUNCTION public.teacher_clear_security_events() TO authenticated;


-- ====================================================================
-- VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- SELECT p.proname,
--   has_function_privilege('anon',p.oid,'EXECUTE') AS anon,
--   has_function_privilege('authenticated',p.oid,'EXECUTE') AS auth
-- FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
-- WHERE n.nspname='public' AND p.proname='teacher_clear_security_events';
-- (expect anon=false, auth=true; students calling it get 'Access denied'
-- from the faculty guard)
-- ====================================================================

-- ====================================================================
-- END OF MIGRATION 14
-- ====================================================================
