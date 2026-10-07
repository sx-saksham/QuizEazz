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

export async function setExamAssignments(
  examId: string,
  studentIds: string[],
): Promise<{ assigned_count: number; added: number; removed: number }> {
  return callRpc(
    () =>
      supabase.rpc('teacher_set_exam_assignments', {
        p_exam_id: examId,
        p_student_ids: studentIds,
      }),
    'Unable to save the assigned cohort.',
  )
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

