-- ====================================================================
-- QUIZEAZZ — MIGRATION 17: SWEEP STALENESS THRESHOLD
-- --------------------------------------------------------------------
-- Run AFTER migrations 1..16. Do NOT re-run any of those.
--
-- WHY THIS FILE EXISTS
--   A production submission was observed stuck at status 'evaluating' with
--   programming_pending = 1 because the browser's single invocation of the
--   judge0-grade Edge Function failed before the function ever ran (no log
--   entry was produced for that attempt).
--
--   The recovery path already exists: judge0-grade supports `sweep: true`,
--   which calls quizeazz_list_attempts_needing_programming_grading() and
--   re-grades everything still pending. That list currently has NO age
--   filter, so a scheduled sweep could pick up an attempt seconds after it
--   was submitted and race the browser's own (much faster) grading run.
--
--   The Edge Function cannot see submitted_at — it only receives attempt
--   ids from this RPC — so the threshold has to live here. Nothing else in
--   the grading path needs to change.
--
-- WHAT THIS FILE DOES
--   Redefines quizeazz_list_attempts_needing_programming_grading() with an
--   identical signature and an identical return shape, adding one extra
--   predicate: only attempts submitted more than STALE_SWEEP_SECONDS ago
--   are returned.
--
--   Nothing else is touched: no table, no column, no RLS policy, no other
--   function, no grant. CREATE OR REPLACE preserves the existing ACL
--   exactly (service_role only; anon/authenticated/PUBLIC remain revoked).
--
-- WHY 60 SECONDS
--   The browser trigger has a 45s total wall-clock budget
--   (TIMEOUT_MS in src/lib/judge0Client.ts), retries included. Once 60s has
--   elapsed the browser has definitively given up, so a sweep at that point
--   cannot compete with it. Observed real grading latency is ~18s
--   (submitted 06:24:47Z -> graded 06:25:05Z on 2026-10-08), so a real
--   browser grading run is never delayed by this threshold.
--
-- IDEMPOTENT: re-runnable.
-- ====================================================================


-- ====================================================================
-- SECTION A — sweep candidate list, now age-bounded
-- --------------------------------------------------------------------
-- Unchanged guarantees:
--   * only coding questions
--   * only answers still exactly 'Pending Evaluation' with real code
--   * only attempts already finalized (never in_progress)
--
-- New: at least STALE_SWEEP_SECONDS (60) must have passed since submission,
-- so the sweep only ever rescues work the browser did not finish.
-- ====================================================================
CREATE OR REPLACE FUNCTION public.quizeazz_list_attempts_needing_programming_grading()
RETURNS TABLE (attempt_id UUID)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  RETURN QUERY
  SELECT DISTINCT a.attempt_id
  FROM public.answers a
  JOIN public.questions q
    ON q.id = a.question_id
   AND q.question_type = 'coding'
   JOIN public.exam_attempts att
    ON att.id = a.attempt_id
   AND att.status IN ('submitted', 'timed_out', 'evaluating', 'evaluated')
  WHERE a.evaluation_status = 'Pending Evaluation'
    AND a.code IS NOT NULL
    AND btrim(a.code) <> ''
    -- Staleness guard: never race the browser's own grading run.
    AND COALESCE(att.submitted_at, att.created_at)
        < now() - make_interval(secs => 60)
  ORDER BY 1;
END;
$$;

-- ACL is intentionally NOT restated: CREATE OR REPLACE preserves the grant
-- set from migration 7 (EXECUTE to service_role only; anon, authenticated
-- and PUBLIC revoked). Restating it here would be a no-op, so this file adds
-- no privilege surface of its own.


-- ====================================================================
-- SECTION B — VERIFICATION (read-only; run after applying)
-- --------------------------------------------------------------------
-- B1. Signature and return shape are unchanged, ACL is still service_role:
-- SELECT p.proname, pg_get_function_result(p.oid) AS returns,
--   has_function_privilege('anon', p.oid, 'EXECUTE')       AS anon,
--   has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth,
--   has_function_privilege('service_role', p.oid, 'EXECUTE') AS svc
-- FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'public'
--   AND p.proname = 'quizeazz_list_attempts_needing_programming_grading';
-- (expect: anon=false, auth=false, svc=true)
--
-- B2. The threshold works and loses no genuinely stuck work:
-- SELECT count(*) AS sweepable FROM public.quizeazz_list_attempts_needing_programming_grading();
-- (expect the 9 historically stuck attempts; they are all days old)
--
-- B3. A fresh attempt is correctly NOT returned:
-- -- (do not run in production; covered by scripts/test_judge0_integration.py)
-- ====================================================================

-- ====================================================================
-- END OF MIGRATION 17
-- ====================================================================