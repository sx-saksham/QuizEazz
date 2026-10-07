/**
 * Class + Batch segregation (QuizEazz V1).
 *
 * There is NO slot system. Class + Batch is the ONLY grouping mechanism:
 * 3 classes x 3 batches = 9 student groups. These constants mirror the
 * database CHECK constraints in migration 10 exactly; the database remains
 * the authority (it re-validates every value), these lists are UX only.
 */

export const CLASS_OPTIONS = ['SY CSDS A', 'SY CSDS B', 'SY CSDS C'] as const
export const BATCH_OPTIONS = ['Batch 1', 'Batch 2', 'Batch 3'] as const

export type ClassName = (typeof CLASS_OPTIONS)[number]
export type BatchName = (typeof BATCH_OPTIONS)[number]

export interface CohortGroup {
  class_name: string
  batch: string
}

/** True when the authoritative profile carries a complete cohort membership. */
export function isOnboarded(profile: {
  class_name?: string | null
  batch?: string | null
} | null): boolean {
  return !!profile && !!profile.class_name && !!profile.batch
}

/** True only for a fully-specified, vocabulary-valid group selection. */
export function isCompleteGroup(
  className: string | null,
  batch: string | null,
): boolean {
  return (
    !!className &&
    !!batch &&
    (CLASS_OPTIONS as readonly string[]).includes(className) &&
    (BATCH_OPTIONS as readonly string[]).includes(batch)
  )
}
