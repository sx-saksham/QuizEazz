/**
 * SINGLE authoritative attempt-level evaluation state for every result and
 * review surface (student list, student review, teacher rows).
 *
 * The ONLY authority is `exam_attempts.status` as served by the RPCs:
 *   evaluated   → evaluation is COMPLETE. Score and status are final.
 *   in_progress → the exam has not been submitted yet. No submitted
 *                 timestamp, no score, nothing final.
 *   anything else (submitted, timed_out, evaluating) → evaluation is
 *                 genuinely still in progress.
 *
 * Stale `attempt.metadata` hints (programming_pending / programming_evaluated)
 * and per-answer `evaluation_status` values must NEVER determine
 * attempt-level state. Per-question outcomes remain visible as evidence
 * where genuinely useful, but they cannot contradict a final attempt.
 */

/** Attempt-level evaluation state. One value, every surface. */
export type AttemptEvaluationState = 'evaluated' | 'evaluating' | 'in_progress'

export function attemptEvaluationState(
  status: string | null | undefined,
): AttemptEvaluationState {
  if (status === 'evaluated') return 'evaluated'
  if (status === 'in_progress') return 'in_progress'
  return 'evaluating'
}

export const ATTEMPT_EVALUATION_LABEL: Record<AttemptEvaluationState, string> = {
  evaluated: 'Evaluated',
  evaluating: 'Evaluation in progress',
  in_progress: 'In Progress',
}
