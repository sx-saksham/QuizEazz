import { supabase, rpcErrorMessage, toErrorMessage } from './supabase'
import type { ExamAttempt, Question } from '../types'

// ====================================================================
// DETERMINISTIC RANDOMIZATION
// ====================================================================
// Every candidate sees the SAME question set for an exam (the paper is
// fixed by faculty). Only presentation order is randomized, and that
// randomization is derived from a stable (studentId, examId) seed so a
// reload can never reshuffle the paper.
//
// Correctness is attached to option identity, never to display position.

// FNV-1a style 32-bit string hash. Never returns 0 (0 is a degenerate seed).
function hashString(input: string): number {
  let hash = 0x811c9dc5
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash === 0 ? 0x9e3779b9 : hash
}

// mulberry32: small, fast, fully deterministic 32-bit PRNG.
function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** Deterministic Fisher–Yates shuffle. Same seed in, same order out. */
export function deterministicShuffle<T>(array: T[], seedStr: string): T[] {
  const result = [...array]
  const rng = mulberry32(hashString(seedStr))
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1))
    ;[result[i], result[j]] = [result[j], result[i]]
  }
  return result
}

/**
 * Builds the candidate-visible paper.
 * Strips anything that must never reach a student screen.
 */
export function buildCandidatePaper(
  rawQuestions: Question[],
  studentId: string,
  examId: string,
): Question[] {
  const ordered = deterministicShuffle(rawQuestions, `${studentId}_${examId}_quizeazz`)

  return ordered.map((q) => {
    // Strip confidential fields on EVERY question, not just MCQs.
    // get_exam_questions() already omits them; this is defense in depth.
    const base: Question = {
      ...q,
      reference_solution: undefined,
      explanation: undefined,
    }

    if (q.question_type !== 'mcq' || !Array.isArray(q.options)) return base

    // Only identity + display text survive. Server-side correctness flags
    // are discarded defensively, even though the RPC already strips them.
    const sanitized = (q.options as { id: string; text: string }[]).map((opt) => ({
      id: opt.id,
      text: opt.text,
    }))

    return {
      ...base,
      options: deterministicShuffle(sanitized, `${studentId}_${q.id}_options`),
    }
  })
}

// ====================================================================
// SECURE ANSWER PERSISTENCE
// ====================================================================
// The database RPC is authoritative. Browser-side guards are UX only.
// The browser NEVER writes directly to public.answers.

export type SaveOutcome = 'saved' | 'failed' | 'blocked'

export class SaveRejectedError extends Error {
  readonly blocked: boolean
  constructor(message: string, blocked: boolean) {
    super(message)
    this.name = 'SaveRejectedError'
    this.blocked = blocked
  }
}

export interface PersistAnswerArgs {
  attemptId: string
  questionId: string
  content: string
  language?: string | null
  isPaused?: boolean
  startedAt?: string
  durationMinutes?: number
}

export async function persistAnswer(args: PersistAnswerArgs): Promise<SaveOutcome> {
  const { attemptId, questionId, content, language } = args

  if (args.isPaused) {
    return 'blocked'
  }

  // Local deadline hint only. The database performs the authoritative check.
  if (args.startedAt && args.durationMinutes) {
    const deadline =
      new Date(args.startedAt).getTime() +
      args.durationMinutes * 60 * 1000 +
      15000
    if (Date.now() > deadline) return 'blocked'
  }

  const { error } = await supabase.rpc('save_exam_answer', {
    p_attempt_id: attemptId,
    p_question_id: questionId,
    p_content: content,
    p_language: language ?? null,
  })

  if (error) {
    throw new SaveRejectedError(
      rpcErrorMessage(error, 'The answer could not be saved to the server.'),
      /PAUSED/i.test(error.message ?? ''),
    )
  }

  return 'saved'
}

// ====================================================================
// EXAM PAUSE / RESUME / SECURITY EVENTS
// ====================================================================
// studentId is deliberately NOT forwarded. The RPC derives the
// authenticated candidate from auth.uid() and verifies ownership.

export async function pauseExamSession(
  attemptId: string,
  reason: string,
): Promise<boolean> {
  const { data, error } = await supabase.rpc('pause_exam_attempt', {
    p_attempt_id: attemptId,
    p_reason: reason,
  })
  if (error) throw new Error(rpcErrorMessage(error, 'Unable to record the exam pause.'))
  return data === true
}

export async function resumeExamSession(attemptId: string): Promise<boolean> {
  const { data, error } = await supabase.rpc('resume_exam_attempt', {
    p_attempt_id: attemptId,
  })
  if (error) throw new Error(rpcErrorMessage(error, 'Unable to resume the examination.'))
  return data === true
}

export type SecurityEventType =
  | 'window_blur'
  | 'visibility_hidden'
  | 'fullscreen_exit'
  | 'tab_switch'
  | 'copy_attempt'
  | 'cut_attempt'
  | 'paste_attempt'

export async function logSecurityEvent(
  attemptId: string,
  examId: string,
  eventType: SecurityEventType,
  currentQuestionId?: string,
  detail?: string,
): Promise<boolean> {
  const { data, error } = await supabase.rpc('record_security_event', {
    p_attempt_id: attemptId,
    p_exam_id: examId,
    p_event_type: eventType,
    p_question_id: currentQuestionId || null,
    p_details: {
      user_agent: typeof navigator !== 'undefined' ? navigator.userAgent : 'unknown',
      detail: detail ?? null,
    },
  })
  if (error) {
    // Never silently dropped: surfaced to the console for invigilator forensics.
    console.warn('[QuizEazz] security event was not recorded:', error.message)
    return false
  }
  return data === true
}

// ====================================================================
// FINAL SUBMISSION — server-authoritative, single round trip
// ====================================================================
// submit_exam_attempt() freezes the attempt AND runs objective evaluation
// inside one server transaction. The client no longer triggers grading and
// therefore holds no grading authority of any kind.

const submittingAttempts = new Set<string>()

export interface SubmitResult {
  success: boolean
  attemptId: string
  status: string
  totalMarks: number
  timedOut: boolean
  programmingPending: number
  message: string
}

export async function submitExamAttempt(attemptId: string): Promise<SubmitResult> {
  if (submittingAttempts.has(attemptId)) {
    throw new Error('Examination submission is already in progress.')
  }
  submittingAttempts.add(attemptId)

  try {
    const { data, error } = await supabase.rpc('submit_exam_attempt', {
      p_attempt_id: attemptId,
    })

    if (error) {
      throw new Error(
        rpcErrorMessage(error, 'Unable to submit the examination. Please retry.'),
      )
    }
    if (!data) {
      throw new Error('Examination submission was not confirmed by the server.')
    }

    const payload = data as {
      status?: string
      total_marks?: number
      timed_out?: boolean
      message?: string
      grading?: { programming_pending?: number }
    }

    return {
      success: true,
      attemptId,
      status: payload.status ?? 'submitted',
      totalMarks: payload.total_marks ?? 0,
      timedOut: payload.timed_out === true,
      programmingPending: payload.grading?.programming_pending ?? 0,
      message: payload.message ?? 'Examination submitted.',
    }
  } finally {
    submittingAttempts.delete(attemptId)
  }
}

// ====================================================================
// ATTEMPT LIFECYCLE & ANSWER RESTORATION
// ====================================================================

export interface ExamSessionInitResult {
  exam: {
    id: string
    title: string
    description: string | null
    duration_minutes: number
    status: string
    starts_at: string | null
    ends_at: string | null
    total_marks: number | null
  }
  attempt: ExamAttempt
}

export async function startOrGetExamSession(examId: string): Promise<ExamSessionInitResult> {
  const { data, error } = await supabase.rpc('start_or_get_exam_attempt', {
    p_exam_id: examId,
  })
  if (error || !data) {
    throw new Error(
      rpcErrorMessage(error, 'Failed to initialize the examination session.'),
    )
  }
  return data as ExamSessionInitResult
}

export async function fetchExamQuestions(examId: string): Promise<Question[]> {
  const { data, error } = await supabase.rpc('get_exam_questions', { p_exam_id: examId })
  if (error) {
    throw new Error(rpcErrorMessage(error, 'Unable to load examination questions.'))
  }
  return Array.isArray(data) ? (data as Question[]) : []
}

/**
 * Restores previously saved answers.
 * Returns ONLY { question_id -> selected_option_id | code }.
 * Never exposes is_correct, marks_awarded, or any evaluation internal.
 */
export async function fetchStudentSavedAnswers(
  attemptId: string,
): Promise<Record<string, string>> {
  const { data, error } = await supabase.rpc('get_student_exam_answers', {
    p_attempt_id: attemptId,
  })
  if (error || !data || !Array.isArray(data)) return {}

  const answers: Record<string, string> = {}
  for (const item of data as {
    question_id: string
    code: string | null
    selected_option_id: string | null
  }[]) {
    if (item.selected_option_id) answers[item.question_id] = item.selected_option_id
    else if (item.code) answers[item.question_id] = item.code
  }
  return answers
}

export { toErrorMessage }
