import type { RealtimeChannel } from '@supabase/supabase-js'
import { supabase } from './supabase'

/**
 * Lightweight live-update plumbing for two screens that were previously
 * stale until the operator pressed F5.
 *
 * Design constraints that shaped this file:
 *
 *  1. NO table data ever travels over these channels. Students read exam
 *     availability through `get_student_exam_overview()`, a SECURITY DEFINER
 *     RPC, and faculty read the audit log through
 *     `teacher_get_security_events()`. A change event is only ever a "something
 *     may have changed, refetch through the RPC" signal, so a subscriber can
 *     never widen its own visibility by listening.
 *
 *  2. Student exam availability CANNOT use `postgres_changes`. `exams` and
 *     `exam_assignments` have no student SELECT policy (`exam_assignments` has
 *     no policy at all), and Supabase Realtime applies RLS to every subscriber.
 *     Making students receive row changes would require granting them direct
 *     table reads, which is exactly what the RPC-first architecture avoids.
 *     So the teacher publishes a bare broadcast signal instead, and students
 *     refetch through the same RPC as before.
 *
 *  3. The faculty audit log DOES use `postgres_changes`: faculty already hold a
 *     SELECT policy on `security_events`, so Realtime delivers their rows and
 *     still refuses students (no student policy exists on that table).
 */

/** Broadcast topic carrying no payload, only "refetch". */
const EXAM_AVAILABILITY_TOPIC = 'quiz:exam-availability'
const EXAM_CHANGED_EVENT = 'changed'

/**
 * Tell every open student tab that exam availability may have moved.
 *
 * Fire-and-forget and deliberately never awaited by its caller: a failed
 * notification must not turn a successful teacher action into an error. The
 * student's next natural refetch (mount, boundary tick, or manual retry) still
 * produces the correct list, so this is an optimisation for freshness rather
 * than the only path to truth.
 */
export function notifyExamAvailabilityChanged(): void {
  const channel = supabase.channel(EXAM_AVAILABILITY_TOPIC, {
    config: { broadcast: { self: false } },
  })

  channel.subscribe((status) => {
    if (status !== 'SUBSCRIBED') return
    void channel
      .send({ type: 'broadcast', event: EXAM_CHANGED_EVENT, payload: {} })
      .catch(() => {
        // Best effort only. See the doc comment above.
      })
      .finally(() => {
        void supabase.removeChannel(channel)
      })
  })
}

/**
 * Run `onChanged` when an open student tab is told exams may have changed.
 *
 * @returns an unsubscribe function; callers must invoke it on unmount so the
 *          channel is torn down instead of leaking a live socket.
 */
export function subscribeToExamAvailability(onChanged: () => void): () => void {
  const channel = supabase.channel(EXAM_AVAILABILITY_TOPIC, {
    config: { broadcast: { self: false } },
  })

  channel
    .on('broadcast', { event: EXAM_CHANGED_EVENT }, () => onChanged())
    .subscribe()

  return () => {
    void supabase.removeChannel(channel)
  }
}

/**
 * Run `onChanged` when the security-event audit log gains or loses rows.
 *
 * Both INSERT and DELETE are observed, so a new proctoring event appears
 * without a refresh and "Clear All Records" empties the table without one
 * either.
 *
 * The callback only signals "refetch"; the component then re-reads through
 * `teacher_get_security_events()`. Deliberately NOT splicing the realtime
 * payload into local state: a payload append plus a concurrent refetch is
 * precisely how duplicate rows appear, whereas refetching has one single
 * source of truth and cannot duplicate.
 *
 * @returns an unsubscribe function for unmount cleanup.
 */
export function subscribeToSecurityEvents(onChanged: () => void): () => void {
  const channel: RealtimeChannel = supabase
    .channel('quiz:security-events')
    .on(
      'postgres_changes',
      { event: 'INSERT', schema: 'public', table: 'security_events' },
      () => onChanged(),
    )
    .on(
      'postgres_changes',
      { event: 'DELETE', schema: 'public', table: 'security_events' },
      () => onChanged(),
    )
    .subscribe()

  return () => {
    void supabase.removeChannel(channel)
  }
}