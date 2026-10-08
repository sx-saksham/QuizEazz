/**
 * Pure exam-schedule time logic. No imports, no side effects.
 *
 * Kept separate from the Realtime plumbing on purpose: this module is pure
 * arithmetic over timestamps, so it can be executed directly by the test suite
 * without standing up a Supabase client or a browser environment.
 */

export interface ScheduleBounds {
  starts_at?: string | null
  ends_at?: string | null
}

/**
 * Milliseconds until the next exam time boundary, or null when there is none.
 *
 * Supabase Realtime cannot observe the arrival of a wall-clock instant: no row
 * is necessarily written when `starts_at` passes, so nothing is emitted at the
 * moment an examination actually opens. This computes the delay to the nearest
 * upcoming `starts_at` / `ends_at` so the caller can schedule ONE precise
 * wake-up and refetch exactly then, instead of polling the server on a timer.
 *
 * Boundaries already in the past — including one exactly equal to `now` —
 * return null, so the caller cannot build a zero-delay loop that refetches in
 * a tight spin.
 */
export function msUntilNextBoundary(
  exams: ReadonlyArray<ScheduleBounds>,
  now: number,
): number | null {
  const upcoming: number[] = []

  for (const exam of exams) {
    for (const raw of [exam.starts_at, exam.ends_at]) {
      if (!raw) continue
      const at = new Date(raw).getTime()
      if (Number.isFinite(at) && at > now) upcoming.push(at)
    }
  }

  if (upcoming.length === 0) return null
  return Math.min(...upcoming) - now
}