import { supabase, rpcErrorMessage } from './supabase'

// ====================================================================
// CANDIDATE API — every call is an authorized SECURITY DEFINER RPC.
// The browser never selects from or writes to a sensitive table.
// ====================================================================

export interface CandidateExam {
  id: string
  title: string
  description: string | null
  duration_minutes: number
  status: string
  starts_at: string | null
  ends_at: string | null
  total_questions: number
  total_marks: number
  attempt: {
    id: string
    exam_id: string
    student_id: string
    status: string
    started_at: string
    submitted_at: string | null
    score: number | null
    is_paused: boolean | null
    created_at: string
  } | null
}

export interface ResultRow {
  id: string
  exam_id: string
  exam_title: string
  date: string
  /** NULL until the attempt is submitted; the UI must not print a
   * submitted timestamp for unsubmitted (live) attempts. */
  submitted_at: string | null
  score: number | null
  total_marks: number
  status: string
  /**
   * How many programming questions still have no objective verdict.
   * Served by get_student_results() as a derived count. The attempt-level
   * status is authoritative: `evaluated` is always final regardless of
   * this hint. Optional so the UI degrades safely against a database
   * that predates the field — it is never used to compute a mark.
   */
  programming_pending?: number
}

export interface ReviewReport {
  attempt_id: string
  exam_id: string
  exam_title: string
  score: number
  total_marks: number
  status: string
  submitted_at: string
  evaluations: {
    question_id: string
    question_title: string
    question_type: 'mcq' | 'coding'
    question_text: string
    marks_total: number
    marks_awarded: number
    outcome: string
    is_correct: boolean | null
    student_option_id: string | null
    correct_option_id: string | null
    student_code: string | null
    language: string | null
    options: { id: string; text: string }[] | null
    explanation: string | null
  }[]
}

async function call<T>(
  run: () => PromiseLike<{ data: unknown; error: { message?: string; details?: string | null } | null }>,
  fallback: string,
): Promise<T> {
  const { data, error } = await run()
  if (error) throw new Error(rpcErrorMessage(error, fallback))
  return (data ?? null) as T
}

export async function fetchMyExams(): Promise<CandidateExam[]> {
  return call<CandidateExam[]>(
    () => supabase.rpc('get_student_exam_overview'),
    'Unable to load your assigned examinations.',
  )
}

export async function fetchMyResults(): Promise<ResultRow[]> {
  return call<ResultRow[]>(
    () => supabase.rpc('get_student_results'),
    'Unable to load your results.',
  )
}

export async function fetchAttemptReview(attemptId: string): Promise<ReviewReport> {
  return call<ReviewReport>(
    () => supabase.rpc('get_student_attempt_review', { p_attempt_id: attemptId }),
    'Unable to load the evaluation report for this attempt.',
  )
}

export async function fetchMyProfile(): Promise<{
  id: string
  full_name: string
  role: string
  class_name: string | null
  batch: string | null
  created_at: string | null
}> {
  return call(
    () => supabase.rpc('get_my_profile'),
    'Unable to load your platform profile.',
  )
}

/**
 * One-shot cohort onboarding. The database owns the write: exactly one row
 * (the caller's own), exactly once (immutable afterwards), exactly the fixed
 * vocabulary. There is no edit path; corrections are an administrator task.
 */
export async function setMyClassBatch(
  className: string,
  batch: string,
): Promise<{ id: string; class_name: string; batch: string }> {
  return call(
    () =>
      supabase.rpc('set_my_class_batch', {
        p_class: className,
        p_batch: batch,
      }),
    'Unable to save your class and batch.',
  )
}
