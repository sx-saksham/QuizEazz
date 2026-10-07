export type UserRole = 'student' | 'teacher' | 'admin'

export interface Profile {
  id: string
  full_name: string
  role: UserRole
  created_at?: string
}

export type ExamStatus = 'draft' | 'published' | 'closed'

export interface Exam {
  id: string
  title: string
  description: string | null
  duration_minutes: number
  starts_at: string | null
  ends_at: string | null
  status: ExamStatus
  total_marks?: number
  created_by?: string
  created_at?: string
  updated_at?: string
  total_questions?: number
  assigned_count?: number
  submitted_count?: number
}

export type QuestionType = 'mcq' | 'coding'
export type Difficulty = 'easy' | 'medium' | 'hard'

export interface McqOption {
  id: string
  text: string
  /** Teacher-only. Never sent to a candidate by any student-facing RPC. */
  is_correct?: boolean
}

export interface Question {
  id: string
  title: string
  question_text: string
  question_type: QuestionType
  category: string
  subcategory: string | null
  difficulty: Difficulty
  options?: McqOption[] | Record<string, unknown> | null
  starter_code?: string | null
  reference_solution?: string | null
  input_requirements?: string | null
  output_requirements?: string | null
  constraints?: string | null
  explanation?: string | null
  language?: string | null
  marks?: number
  /** Derived server-side from the private key. Faculty reads only. */
  correct_option_id?: string | null
  created_by?: string
  created_at?: string
}

export type AttemptStatus =
  | 'in_progress'
  | 'submitted'
  | 'timed_out'
  | 'evaluating'
  | 'evaluated'

export interface ExamAttempt {
  id: string
  exam_id: string
  student_id: string
  started_at: string
  submitted_at: string | null
  status: AttemptStatus
  score: number | null
  created_at: string
  is_paused?: boolean
  paused_at?: string | null
  pause_reason?: string | null
  metadata?: Record<string, unknown> | null
}

/**
 * Canonical objective outcome vocabulary.
 * `Pending Evaluation` is used for programming questions whenever no
 * isolated execution runner (Judge0 CE) has produced a verdict. Marks are
 * never awarded for a pending result.
 */
export type EvaluationOutcome =
  | 'Correct'
  | 'Wrong Answer'
  | 'Not Attempted'
  | 'Pending Evaluation'
  | 'Compilation Error'
  | 'Runtime Error'
  | 'Time Limit Exceeded'
  | 'Memory Limit Exceeded'
  | 'System Error'

export interface QuestionEvaluation {
  question_id: string
  question_title: string
  question_type: QuestionType
  question_text: string
  marks_total: number
  marks_awarded: number
  outcome: EvaluationOutcome | string
  is_correct?: boolean | null
  student_option_id?: string | null
  correct_option_id?: string | null
  options?: McqOption[] | null
  student_code?: string | null
  language?: string | null
  explanation?: string | null
}

export interface SecurityEventRow {
  id: string
  attempt_id: string | null
  student_id: string
  student_name?: string | null
  exam_id: string
  exam_title?: string | null
  event_type: string
  details?: Record<string, unknown> | null
  question_id?: string | null
  event_timestamp: string
}
