import React, { useCallback, useEffect, useRef } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  CheckCircle2,
  XCircle,
  Clock3,
  AlertCircle,
  FileText,
  Hourglass,
} from 'lucide-react'
import { fetchAttemptReview, type ReviewReport } from '../../lib/studentApi'
import {
  attemptEvaluationState,
  ATTEMPT_EVALUATION_LABEL,
} from '../../lib/attemptStatus'
import { useAsyncData } from '../../hooks/useAsyncData'
import { languageLabel } from '../../lib/languages'

/** Verdict values that mean "a real, objective outcome exists for this question". */
const CONCLUSIVE = new Set(['Correct', 'Wrong Answer', 'Not Attempted'])

/**
 * Programming answers are graded after the attempt is finalized, by the
 * server, in an isolated execution environment. That takes a few seconds, so
 * this page refetches on a short interval while anything is still pending and
 * stops as soon as the server reports a conclusive verdict.
 *
 * Bounded on purpose: at most POLL_MAX_ATTEMPTS refetches, then the page stops
 * and shows the report as it stands. A student must never be left on a timer
 * that runs forever, and a report that is already complete is never polled.
 */
const POLL_INTERVAL_MS = 3_000
const POLL_MAX_ATTEMPTS = 10

const outcomeStyle = (outcome: string, marksAwarded: number, marksTotal: number) => {
  if (outcome === 'Correct') {
    return { cls: 'bg-emerald-50 text-emerald-700', Icon: CheckCircle2 }
  }
  if (outcome === 'Wrong Answer' || outcome === 'Compilation Error') {
    return { cls: 'bg-rose-50 text-rose-700', Icon: XCircle }
  }
  if (outcome === 'Pending Evaluation') {
    return { cls: 'bg-amber-50 text-amber-700', Icon: Clock3 }
  }
  if (marksAwarded > 0 && marksAwarded < marksTotal) {
    return { cls: 'bg-amber-50 text-amber-700', Icon: CheckCircle2 }
  }
  return { cls: 'bg-slate-100 text-slate-600', Icon: AlertCircle }
}

/**
 * Display-only label for the verdict badge.
 *
 * A candidate sees a plain pass/fail, so a compilation failure is shown as
 * "WRONG" rather than as compiler jargon. This is presentation ONLY: the
 * stored verdict, the grading logic, the marks and every other outcome string
 * are completely untouched.
 */
const outcomeLabel = (outcome: string) => (outcome === 'Compilation Error' ? 'WRONG' : outcome)

export const StudentResultReviewPage: React.FC = () => {
  const { attemptId } = useParams<{ attemptId: string }>()
  const navigate = useNavigate()

  // Server-authoritative report. There is no local cache authority.
  const load = useCallback(
    () => (attemptId ? fetchAttemptReview(attemptId) : Promise.reject(new Error('No attempt specified.'))),
    [attemptId],
  )
  const { data: report, error, loading, reload } = useAsyncData<ReviewReport | null>(load, null, {
    errorFallback: 'Unable to load the evaluation report.',
  })

  // Refetch budget for this attempt. A ref, not state: it must not itself
  // trigger a render, and it must survive the effect re-arming between polls
  // so the cap is a total, not a per-render one.
  const pollAttemptsRef = useRef(0)

  useEffect(() => {
    // Nothing to wait for until the first report has arrived.
    if (!report) return

    // Attempt-level state is authoritative: a finalized report is never
    // polled, however stale individual answer outcomes look. Only a live
    // evaluation still in progress may refetch.
    if (attemptEvaluationState(report.status) !== 'evaluating') return

    // Only poll while a programming answer is genuinely unresolved.
    const pendingProgramming = report.evaluations.some(
      (e) => e.question_type === 'coding' && !CONCLUSIVE.has(e.outcome),
    )
    if (!pendingProgramming) return

    // Hard stop after the budget is spent.
    if (pollAttemptsRef.current >= POLL_MAX_ATTEMPTS) return

    // A single timeout, re-armed only after the previous reload settles. The
    // cleanup guarantees there is never more than one timer alive, and that it
    // is cleared on unmount.
    const timer = setTimeout(() => {
      pollAttemptsRef.current += 1
      reload()
    }, POLL_INTERVAL_MS)

    return () => clearTimeout(timer)
  }, [report, reload])

  if (loading) {
    return <div className="py-16 text-center text-xs text-slate-500">Loading evaluation report...</div>
  }

  if (!report) {
    return (
      <div className="space-y-4 max-w-xl mx-auto py-12 text-center">
        <FileText className="h-8 w-8 text-slate-400 mx-auto" />
        <h3 className="text-sm font-semibold text-slate-900">Evaluation Report Unavailable</h3>
        <p className="text-xs text-slate-500">{error ?? 'This attempt could not be loaded.'}</p>
        <button
          onClick={() => navigate('/student/results')}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-700 hover:bg-slate-50"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          <span>Back to Results</span>
        </button>
      </div>
    )
  }

  const percentage =
    report.total_marks > 0 ? Math.round((report.score / report.total_marks) * 100) : 0

  // ---------------------------------------------------------------
  // Attempt-level state is authoritative (shared helper, same as the
  // results list): exam_attempts.status alone decides. Per-question
  // outcomes below remain visible as evidence, but they can never flip
  // this page-level state — an `evaluated` attempt is final even when a
  // legacy answer row still carries a stale pending outcome.
  // ---------------------------------------------------------------
  const evalState = attemptEvaluationState(report.status)
  const evaluationLabel = ATTEMPT_EVALUATION_LABEL[evalState]
  const evaluationBadge =
    evalState === 'evaluated'
      ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
      : evalState === 'in_progress'
      ? 'bg-blue-50 text-blue-700 border-blue-200'
      : 'bg-amber-50 text-amber-800 border-amber-200'

  return (
    <div className="space-y-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between">
        <button
          onClick={() => navigate('/student/results')}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-700 hover:bg-slate-50 transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          <span>Back to Results</span>
        </button>
        <span className="text-xs text-slate-500">
          Submitted: {new Date(report.submitted_at).toLocaleString()}
        </span>
      </div>

      {/* ---------------- TOP CARD: submission / evaluation / score ------------- */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-2xs space-y-4">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-500 block mb-1">
              {report.exam_title}
            </span>
            <div className="flex items-baseline gap-2">
              <span className="text-2xl font-bold text-slate-900">
                {report.score} / {report.total_marks}
              </span>
              <span className="text-sm font-semibold text-indigo-600">({percentage}%)</span>
            </div>
            <p className="text-xs text-slate-500 mt-1">
              {evalState === 'evaluated'
                ? 'Final score across every question in this paper.'
                : 'Your score will be final once evaluation is complete.'}
            </p>
          </div>

          <span
            className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-semibold border ${evaluationBadge}`}
          >
            {evalState === 'evaluated' ? (
              <CheckCircle2 className="h-4 w-4" />
            ) : (
              <Hourglass className="h-4 w-4" />
            )}
            {evaluationLabel}
          </span>
        </div>

        <p className="text-xs text-slate-500 border-t border-slate-100 pt-3">
          {evalState === 'evaluated'
            ? 'Final score across every question in this paper.'
            : evalState === 'evaluating'
            ? 'Your score will be final once evaluation is complete.'
            : 'This examination has not been submitted yet.'}
        </p>
        <p className="text-xs text-slate-500">
          Submitted work cannot be changed after submission.
        </p>
      </div>

      {evalState === 'evaluating' && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/70 p-3.5 text-xs text-amber-900 flex items-start gap-2.5">
          <Clock3 className="h-4 w-4 shrink-0 mt-0.5" />
          <div>
            <p className="font-semibold">Evaluation in progress</p>
            <p className="mt-0.5 leading-relaxed">
              Your answer has been submitted successfully. Your result will appear here once
              evaluation is complete.
            </p>
          </div>
        </div>
      )}

      {evalState === 'in_progress' && (
        <div className="rounded-xl border border-blue-200 bg-blue-50/70 p-3.5 text-xs text-blue-900">
          This examination is still in progress. Return to your exams to continue.
        </div>
      )}

      {report.evaluations.length === 0 ? (
        evalState === 'evaluated' ? (
          <div className="rounded-xl border border-slate-200 bg-white p-10 text-center">
            <p className="text-xs text-slate-500">
              Your final result has been recorded.
            </p>
          </div>
        ) : null
      ) : (
        <div className="space-y-4">
          <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500">
            Question-by-Question Review
          </h2>

          {report.evaluations.map((ev, idx) => {
            const { cls, Icon } = outcomeStyle(ev.outcome, ev.marks_awarded, ev.marks_total)

            return (
              <div
                key={ev.question_id || idx}
                className="rounded-xl border border-slate-200 bg-white p-5 space-y-3 shadow-2xs"
              >
                <div className="flex items-center justify-between pb-2 border-b border-slate-100 flex-wrap gap-2">
              <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-bold text-slate-800">Question {idx + 1}</span>
                    <span className="text-xs font-semibold text-slate-600 truncate">
                      {ev.question_title}
                    </span>
                    <span className="text-xs uppercase font-bold text-slate-500 bg-slate-100 px-1.5 py-0.5 rounded">
                      {ev.question_type}
                    </span>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    <span
                      className={`inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded ${cls}`}
                    >
                      <Icon className="h-3 w-3" />
                      <span>{outcomeLabel(ev.outcome)}</span>
                    </span>
                    <span className="text-sm font-bold text-slate-800">
                      {ev.marks_awarded} / {ev.marks_total}
                    </span>
                  </div>
                </div>

                <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-wrap">
                  {ev.question_text}
                </p>

                {ev.question_type === 'mcq' && ev.options && (
                  <div className="pt-2 border-t border-slate-100 space-y-2 text-sm">
                    {ev.options.map((opt, oi) => {
                      const letter = String.fromCharCode(65 + oi)
                      const isChosen = ev.student_option_id === opt.id
                      const isKey = ev.correct_option_id === opt.id

                      let borderCls = 'border-slate-200 bg-slate-50/50 text-slate-700'
                      if (isKey) borderCls = 'border-emerald-300 bg-emerald-50/70 text-emerald-900 font-medium'
                      else if (isChosen) borderCls = 'border-rose-300 bg-rose-50/70 text-rose-900'

                      return (
                        <div
                          key={opt.id}
                          className={`p-2.5 rounded-lg border flex items-center justify-between gap-3 ${borderCls}`}
                        >
                          <span className="flex items-center gap-2">
                            <span className="font-bold">{letter}.</span>
                            <span>{opt.text}</span>
                          </span>
                          <span className="flex items-center gap-1.5 text-xs font-semibold shrink-0">
                            {isChosen && (
                              <span className="px-1.5 py-0.5 rounded bg-slate-200 text-slate-800">
                                Your Choice
                              </span>
                            )}
                            {isKey && (
                              <span className="px-1.5 py-0.5 rounded bg-emerald-600 text-white">
                                Correct Answer
                              </span>
                            )}
                          </span>
                        </div>
                      )
                    })}
                    {!ev.student_option_id && (
                      <p className="text-xs text-slate-500">You did not answer this question.</p>
                    )}
                  </div>
                )}

                {ev.question_type === 'coding' && (
                  <div className="pt-2 border-t border-slate-100 space-y-2.5 text-xs">
                    <div>
                      <span className="text-xs font-semibold uppercase text-slate-500 block mb-1">
                        Submitted code · {languageLabel(ev.language)}
                      </span>
                      <pre className="p-3 rounded-lg bg-slate-900 text-slate-100 font-mono text-xs leading-relaxed overflow-x-auto max-h-80">
                        {ev.student_code || '# No code was submitted for this problem'}
                      </pre>
                    </div>

                    {/* Answer-level evidence only: shown while evaluation is
                        genuinely unfinished, never on a final attempt. */}
                    {evalState !== 'evaluated' && !CONCLUSIVE.has(ev.outcome) && (
                      <p className="p-2.5 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900">
                        Evaluation in progress.
                      </p>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
