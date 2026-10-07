import { supabase, rpcErrorMessage } from './supabase'
import type { Exam, Question } from '../types'

// ====================================================================
// FACULTY API — every call is an authorized SECURITY DEFINER RPC.
// The browser never writes directly to a sensitive table.
// ====================================================================

export interface Candidate {
  id: string
  full_name: string | null
  email: string | null
  role: string
  /** Authoritative cohort membership. Null until the student onboards. */
  class_name: string | null
  batch: string | null
  created_at: string | null
}

export interface ExamPaperItem {
  question_id: string
  title: string
  question_type: 'mcq' | 'coding'
  category: string | null
  subcategory: string | null
  difficulty: string | null
  language: string | null
  marks: number
  bank_marks: number
}

export type NewPaperItem = Omit<
  ExamPaperItem,
  'title' | 'category' | 'subcategory' | 'difficulty' | 'language' | 'bank_marks'
>

export interface ExamPaper {
  exam: {
    id: string
    title: string
    status: string
    starts_at: string | null
    duration_minutes: number
  }
  items: ExamPaperItem[]
  total_questions: number
  total_marks: number
}

export interface AttemptRow {
  attempt_id: string
  exam_id: string
  exam_title: string
  student_id: string
  student_name: string | null
  status: string
  score: number | null
  total_marks: number
  started_at: string | null
  submitted_at: string | null
  is_paused: boolean | null
  pause_reason: string | null
  created_at: string
}

export interface AttemptDetail {
  attempt_id: string
  exam_id: string
  student_id: string
  status: string
  score: number
  total_marks: number
  started_at: string | null
  submitted_at: string | null
  items: {
    question_id: string
    title: string
    question_type: 'mcq' | 'coding'
    language: string | null
    marks_total: number
    marks_awarded: number
    outcome: string
    is_correct: boolean | null
    student_option_id: string | null
    correct_option_id: string | null
    options: { id: string; text: string }[] | null
    code: string | null
    explanation: string | null
  }[]
}

async function callRpc<T>(
  fn: () => PromiseLike<{ data: unknown; error: { message?: string; details?: string | null } | null }>,
  fallback: string,
): Promise<T> {
  const { data, error } = await fn()
  if (error) throw new Error(rpcErrorMessage(error, fallback))
  return (data ?? null) as T
}

export async function fetchQuestions(): Promise<Question[]> {
  return callRpc<Question[]>(
    () => supabase.rpc('teacher_get_questions'),
    'Unable to load the question bank.',
  )
}

export async function createQuestion(payload: {
  title: string
  question_text: string
  question_type: 'mcq' | 'coding'
  category: string
  subcategory: string | null
  marks: number
  explanation: string | null
  language: string | null
  input_requirements: string | null
  output_requirements: string | null
  constraints: string | null
  starter_code: string | null
  reference_solution: string | null
  options: unknown
  correct_answer: string | null
}): Promise<Question> {
  const created = await callRpc<Question | Question[]>(
    () =>
      supabase.rpc('teacher_create_question', {
        p_title: payload.title,
        p_question_text: payload.question_text,
        p_question_type: payload.question_type,
        p_category: payload.category,
        p_subcategory: payload.subcategory,
        p_marks: payload.marks,
        p_explanation: payload.explanation,
        p_language: payload.language,
        p_input_requirements: payload.input_requirements,
        p_output_requirements: payload.output_requirements,
        p_constraints: payload.constraints,
        p_starter_code: payload.starter_code,
        p_reference_solution: payload.reference_solution,
        p_options: payload.options,
        p_correct_answer: payload.correct_answer,
      }),
    'Unable to save the question.',
  )

  // Server confirmation is mandatory. No optimistic local insertion.
  const question = Array.isArray(created) ? created[0] : created
  if (!question?.id) {
    throw new Error(
      'The database did not confirm the insert. The question was NOT saved — please retry.',
    )
  }
  return question
}

export interface QuestionUpdate {
  question_id: string
  title?: string | null
  question_text?: string | null
  question_type?: 'mcq' | 'coding' | null
  category?: string | null
  subcategory?: string | null
  marks?: number | null
  explanation?: string | null
  language?: string | null
  input_requirements?: string | null
  output_requirements?: string | null
  constraints?: string | null
  starter_code?: string | null
  reference_solution?: string | null
  options?: unknown
  correct_answer?: string | null
}

/**
 * Edits a bank question. Published exams, attempts and reviews read frozen
 * snapshots, so no edit — however large — can rewrite history. Only NULL
 * fields are left unchanged; the database re-validates everything supplied.
 */
export async function updateQuestion(payload: QuestionUpdate): Promise<Question> {
  const updated = await callRpc<Question | Question[]>(
    () =>
      supabase.rpc('teacher_update_question', {
        p_question_id: payload.question_id,
        p_title: payload.title ?? null,
        p_question_text: payload.question_text ?? null,
        p_question_type: payload.question_type ?? null,
        p_category: payload.category ?? null,
        p_subcategory: payload.subcategory ?? null,
        p_marks: payload.marks ?? null,
        p_explanation: payload.explanation ?? null,
        p_language: payload.language ?? null,
        p_input_requirements: payload.input_requirements ?? null,
        p_output_requirements: payload.output_requirements ?? null,
        p_constraints: payload.constraints ?? null,
        p_starter_code: payload.starter_code ?? null,
        p_reference_solution: payload.reference_solution ?? null,
        p_options: payload.options ?? null,
        p_correct_answer: payload.correct_answer ?? null,
      }),
    'Unable to save the question changes.',
  )

  const question = Array.isArray(updated) ? updated[0] : updated
  if (!question?.id) {
    throw new Error(
      'The database did not confirm the update. The question was NOT changed — please retry.',
    )
  }
  return question
}

export interface ExamUpdate {
  exam_id: string
  title?: string | null
  description?: string | null
  duration_minutes?: number | null
}

/**
 * Edits safe exam fields (title/description/duration). Blocked server-side
 * once any finalized attempt exists; schedule keeps its own RPC/guard.
 */
export async function updateExam(payload: ExamUpdate): Promise<Exam> {
  const updated = await callRpc<Exam>(
    () =>
      supabase.rpc('teacher_update_exam', {
        p_exam_id: payload.exam_id,
        p_title: payload.title ?? null,
        p_description: payload.description ?? null,
        p_duration_minutes: payload.duration_minutes ?? null,
      }),
    'Unable to save the examination changes.',
  )

  if (!updated?.id) {
    throw new Error(
      'The database did not confirm the update. The examination was NOT changed — please retry.',
    )
  }
  return updated
}

export async function fetchExams(): Promise<Exam[]> {
  return callRpc<Exam[]>(
    () => supabase.rpc('teacher_get_exams'),
    'Unable to load examination schedules.',
  )
}

export async function createExam(payload: {
  title: string
  description: string | null
  duration_minutes: number
  status: 'draft' | 'published'
  starts_at: string
}): Promise<Exam> {
  const created = await callRpc<Exam>(
    () =>
      supabase.rpc('teacher_create_exam', {
        p_title: payload.title,
        p_description: payload.description,
        p_duration_minutes: payload.duration_minutes,
        p_status: payload.status,
        p_starts_at: payload.starts_at,
      }),
    'Unable to schedule the examination.',
  )

  if (!created?.id) {
    throw new Error(
      'The database did not confirm the insert. The examination was NOT created — please retry.',
    )
  }
  return created
}

export async function setExamStatus(
  examId: string,
  status: 'draft' | 'published' | 'closed',
): Promise<Exam> {
  return callRpc<Exam>(
    () => supabase.rpc('teacher_set_exam_status', { p_exam_id: examId, p_status: status }),
    'Unable to update the examination status.',
  )
}

export async function setExamSchedule(
  examId: string,
  payload: { durationMinutes: number; startsAt: string; endsAt?: string | null },
): Promise<Exam> {
  return callRpc<Exam>(
    () =>
      supabase.rpc('teacher_set_exam_schedule', {
        p_exam_id: examId,
        p_duration_minutes: payload.durationMinutes,
        p_starts_at: payload.startsAt,
        p_ends_at: payload.endsAt ?? null,
      }),
    'Unable to update the examination schedule.',
  )
}

export async function fetchCandidates(): Promise<Candidate[]> {
  return callRpc<Candidate[]>(
    () => supabase.rpc('teacher_get_candidates'),
    'Unable to load the candidate registry.',
  )
}

export async function fetchOverviewStats(): Promise<{
  total_questions: number
  total_exams: number
  active_exams: number
  total_students: number
  total_attempts: number
  recent_exams: {
    id: string
    title: string
    duration_minutes: number
    status: string
    created_at: string
  }[]
}> {
  return callRpc(
    () => supabase.rpc('teacher_get_overview_stats'),
    'Unable to load dashboard statistics.',
  )
}

export async function fetchExamAssignments(
  examId: string,
): Promise<{ exam: ExamPaper['exam']; assigned_student_ids: string[]; assigned_count: number }> {
  return callRpc(
    () => supabase.rpc('teacher_get_exam_assignments', { p_exam_id: examId }),
    'Unable to load the assigned cohort.',
  )
}

export interface AssignmentGroup {
  class_name: string
  batch: string
}

/**
 * Persists the assigned cohort. When `group` is supplied, the backend
 * verifies every id against that exact authoritative membership and rejects
 * the whole write on any mismatch. When null, the legacy individual-pick
 * path applies (membership still validated as registered students).
 */
export async function setExamAssignments(
  examId: string,
  studentIds: string[],
  group?: AssignmentGroup | null,
): Promise<{ assigned_count: number; added: number; removed: number }> {
  return callRpc(
    () =>
      supabase.rpc('teacher_set_exam_assignments', {
        p_exam_id: examId,
        p_student_ids: studentIds,
        p_class: group?.class_name ?? null,
        p_batch: group?.batch ?? null,
      }),
    'Unable to save the assigned cohort.',
  )
}

export interface HiddenTestCase {
  id: string
  case_order: number
  stdin: string
  expected_stdout: string | null
  validated: boolean
}

export interface ReferenceValidationResult {
  case_order: number
  ok: boolean
  status_id: number | null
  stdout_preview: string | null
  error?: string
}

/**
 * Reads hidden test cases for a coding question. Faculty-only RPC; students
 * have no path to this data (no RLS grant, no student RPC returns it).
 */
export async function fetchTestCases(
  questionId: string,
): Promise<{ question_id: string; cases: HiddenTestCase[] }> {
  return callRpc(
    () => supabase.rpc('teacher_get_test_cases', { p_question_id: questionId }),
    'Unable to load the hidden test cases.',
  )
}

/**
 * Replaces the full hidden-test set in one transaction. Only stdin values
 * are sent; expected outputs are produced exclusively by reference
 * validation (never typed by hand into the grading path).
 */
export async function saveTestCases(
  questionId: string,
  stdins: string[],
): Promise<{ question_id: string; saved: number }> {
  return callRpc(
    () =>
      supabase.rpc('teacher_set_test_cases', {
        p_question_id: questionId,
        p_cases: stdins.map((stdin) => ({ stdin })),
      }),
    'Unable to save the hidden test cases.',
  )
}

export interface ReferenceValidationReport {
  ok: boolean
  validated?: number
  total?: number
  error?: string
  results?: ReferenceValidationResult[]
}

/**
 * Runs the stored faculty reference solution through Judge0 (server-side
 * only) once per hidden-test stdin and stores each cleanly-succeeded run's
 * normalized stdout as that case's expected output. Faculty-only; the
 * reference solution and all outputs stay server-side.
 */
export async function validateReferenceSolution(
  questionId: string,
): Promise<ReferenceValidationReport> {
  const { data, error } = await supabase.functions.invoke('judge0-grade', {
    body: { validate_reference: true, question_id: questionId },
  })
  if (error) throw new Error(rpcErrorMessage(error, 'Reference validation could not be started.'))
  return (data ?? { ok: false }) as ReferenceValidationReport
}

export async function fetchExamPaper(examId: string): Promise<ExamPaper> {
  return callRpc<ExamPaper>(
    () => supabase.rpc('teacher_get_exam_paper', { p_exam_id: examId }),
    'Unable to load the examination paper.',
  )
}

export async function setExamPaper(
  examId: string,
  items: { question_id: string; marks: number }[],
): Promise<{ total_questions: number; total_marks: number; removed: number; saved: number }> {
  return callRpc(
    () =>
      supabase.rpc('teacher_set_exam_paper', {
        p_exam_id: examId,
        p_items: items,
      }),
    'Unable to save the examination paper.',
  )
}

export async function fetchAttempts(examId?: string): Promise<AttemptRow[]> {
  return callRpc<AttemptRow[]>(
    () => supabase.rpc('teacher_get_attempts', { p_exam_id: examId ?? null }),
    'Unable to load candidate submissions.',
  )
}

export async function fetchAttemptDetail(attemptId: string): Promise<AttemptDetail> {
  return callRpc<AttemptDetail>(
    () => supabase.rpc('teacher_get_attempt_detail', { p_attempt_id: attemptId }),
    'Unable to load the submission evidence.',
  )
}

export async function fetchSecurityEvents(examId?: string) {
  return callRpc(
    () =>
      supabase.rpc('teacher_get_security_events', {
        p_exam_id: examId ?? null,
        p_limit: 300,
      }),
    'Unable to load the security audit log.',
  )
}

export async function fetchAdminProfiles() {
  return callRpc(
    () => supabase.rpc('admin_get_profiles'),
    'Unable to load the user directory.',
  )
}

