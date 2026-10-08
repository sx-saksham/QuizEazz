-- ====================================================================
-- QUIZEAZZ — MIGRATION 19: EXPLICIT WHERE IN CLEAR SECURITY LOG
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..18. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   Production evidence, captured from the deployed app's Network panel:
--
--     POST /rest/v1/rpc/teacher_clear_security_events
--     { "code": "21000", "details": null, "hint": null,
--       "message": "DELETE requires a WHERE clause" }
--
--   SQLSTATE 21000 is cardinality_violation, raised on the RPC path through
--   PostgREST. Migration 14's DELETE has no WHERE clause:
--
--     WITH removed AS (
--       DELETE FROM public.security_events
--       RETURNING id
--     )
--     SELECT COUNT(*)::INT INTO v_removed FROM removed;
--
--   That statement is valid SQL and does delete every row, but it is refused
--   on the RPC request path, so faculty could never clear the audit log and the
--   button reported the error above instead of succeeding.
--
-- THE FIX
--   Write the same deletion as `DELETE FROM public.security_events WHERE true`.
--   `WHERE true` is a tautology, so the affected rows are IDENTICAL: every row
--   in the table is still removed. It only makes the predicate explicit, which
--   is what the guard on that path requires.
--
--   No client-supplied WHERE clause is introduced. The function still takes NO
--   parameters at all, so it remains "delete everything, and nothing else" -
--   there is no way for a caller to narrow, target or scope the deletion.
--
-- SCOPE
--   One CREATE OR REPLACE of an existing function. No table created, altered
--   or dropped. No RLS policy added, changed or removed. No grant or revoke
--   (migration 14's ACL is preserved: EXECUTE to authenticated only; PUBLIC
--   and anon revoked). No change to the RPC name, its argument list, its
--   return shape, or the SECURITY DEFINER / faculty authorization behaviour.
--   No client-side table deletion is exposed.
--
-- IDEMPOTENT: CREATE OR REPLACE, so re-running is safe.
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

  -- `WHERE true` is a tautology: this still deletes every row. It is written
  -- explicitly so the statement is accepted on the RPC request path, which
  -- refuses a DELETE with no predicate. There is still no parameter, so a
  -- caller can neither scope nor target the deletion.
  WITH removed AS (
    DELETE FROM public.security_events
    WHERE true
    RETURNING id
  )
  SELECT COUNT(*)::INT INTO v_removed FROM removed;

  RETURN jsonb_build_object(
    'cleared', TRUE,
    'removed', v_removed
  );
END;
$$;

-- ACL is intentionally NOT restated: CREATE OR REPLACE preserves the grant
-- set from migration 14. Restating it would be a no-op.


-- ====================================================================
-- VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- B1. Signature, return type, definer and ACL are unchanged
--     (expect: args='', returns=jsonb, security_definer=true,
--              anon=false, public=false, auth=true):
--   SELECT p.proname,
--     pg_get_function_arguments(p.oid) AS args,
--     pg_get_function_result(p.oid)    AS returns,
--     p.prosecdef                       AS security_definer,
--     has_function_privilege('anon', p.oid, 'EXECUTE')         AS anon,
--     has_function_privilege('public', p.oid, 'EXECUTE')       AS public,
--     has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth
--   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--   WHERE n.nspname = 'public' AND p.proname = 'teacher_clear_security_events';
--
-- B2. The statement now carries an explicit predicate:
--   SELECT position('WHERE true' in p.prosrc) > 0 AS has_explicit_where
--   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--   WHERE n.nspname = 'public' AND p.proname = 'teacher_clear_security_events';
--
-- B3. RLS on security_events is untouched (expect: true):
--   SELECT relrowsecurity FROM pg_class
--   WHERE oid = 'public.security_events'::regclass;
-- ====================================================================


-- ====================================================================
-- END OF MIGRATION 19
-- ====================================================================