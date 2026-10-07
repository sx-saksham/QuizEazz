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
  if (outcome === 'Wrong Answer') {
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

/** Human label for the attempt lifecycle value held by the database. */
const lifecycleLabel = (status: string): string => {
  switch (status) {
    case 'submitted':
      return 'Submitted'
    case 'timed_out':
      return 'Time Expired — Submitted'
    case 'evaluating':
      return 'Submitted — Evaluating'
    case 'evaluated':
      return 'Submitted — Objective Grading Complete'
    case 'in_progress':
      return 'In Progress'
    default:
      return status.replace(/_/g, ' ')
  }
}

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

    // Only poll while a programming answer is genuinely unresolved. A report
    // that is already complete is never polled.
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
  // Status ladder — three DISTINCT facts, never collapsed into one.
  //
  // Every value below comes from the server's per-question `outcome`.
  // Nothing is inferred from the code, the score, or the attempt status.
  // ---------------------------------------------------------------

  // 1. EVALUATION status: does a real verdict exist for EVERY question?
  const pendingEvaluations = report.evaluations.filter(
    (e) => !CONCLUSIVE.has(e.outcome),
  )
  const pendingCoding = pendingEvaluations.filter((e) => e.question_type === 'coding')
  const hasPending = pendingCoding.length > 0 || pendingEvaluations.length > 0

  // 2. SCORE status: a score over a paper that still has ungraded questions
  //    is provisional, and must be labelled as such.
  const scoreIsFinal = !hasPending

  const evaluationLabel = hasPending ? 'Evaluation Pending' : 'Evaluated'
  const evaluationBadge = hasPending
    ? 'bg-amber-50 text-amber-800 border-amber-200'
    : 'bg-emerald-50 text-emerald-700 border-emerald-200'

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
              {scoreIsFinal
                ? 'Final score across every question in this paper.'
                : 'Provisional. These are the marks awarded so far; the questions below marked “Pending Evaluation” carry no marks yet and will change this total.'}
            </p>
          </div>

          <span
            className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-semibold border ${evaluationBadge}`}
          >
            {hasPending ? (
              <Hourglass className="h-4 w-4" />
            ) : (
              <CheckCircle2 className="h-4 w-4" />
            )}
            {evaluationLabel}
          </span>
        </div>

        <dl className="grid gap-2.5 sm:grid-cols-2 border-t border-slate-100 pt-4">
          <div className="rounded-lg bg-slate-50 border border-slate-200 px-3 py-2">
            <dt className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              1 · Submission
            </dt>
            <dd className="text-xs text-slate-700 mt-0.5">
              {lifecycleLabel(report.status)} ·{' '}
              {new Date(report.submitted_at).toLocaleString()}
              <span className="block text-slate-500">
                Your paper is frozen. Nothing further can be changed.
              </span>
            </dd>
          </div>

          <div className="rounded-lg bg-slate-50 border border-slate-200 px-3 py-2">
            <dt className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              2 · Evaluation
            </dt>
            <dd className="text-xs text-slate-700 mt-0.5">
              {hasPending ? (
                <>
                  {pendingCoding.length} programming question
                  {pendingCoding.length === 1 ? '' : 's'} awaiting isolated code
                  execution. {report.evaluations.length - pendingEvaluations.length} of{' '}
                  {report.evaluations.length} question
                  {report.evaluations.length === 1 ? ' has' : 's have'} a confirmed verdict.
                </>
              ) : (
                <>
                  Complete. All {report.evaluations.length} question
                  {report.evaluations.length === 1 ? ' has' : 's have'} a confirmed objective
                  verdict.
                </>
              )}
            </dd>
          </div>
        </dl>
      </div>

      {hasPending && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/70 p-3.5 text-xs text-amber-900 flex items-start gap-2.5">
          <Clock3 className="h-4 w-4 shrink-0 mt-0.5" />
          <div>
            <p className="font-semibold">Some programming questions are awaiting evaluation</p>
            <p className="mt-0.5 leading-relaxed">
              Your programming answers are being evaluated now, in an isolated environment that runs
              each submission on its own. This page updates on its own once the evaluation is
              complete, so there is nothing you need to do. Until a result is confirmed, no marks
              have been awarded or estimated for these questions.
            </p>
          </div>
        </div>
      )}

      {report.evaluations.length === 0 ? (
        <div className="rounded-xl border border-slate-200 bg-white p-10 text-center">
          <p className="text-xs text-slate-500">
            Evaluation has not been completed for this attempt yet.
          </p>
        </div>
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
                      <span>{ev.outcome}</span>
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

                    {!CONCLUSIVE.has(ev.outcome) && (
                      <p className="p-2.5 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900">
                        Awaiting evaluation in the isolated execution environment. No marks are
                        awarded and no correctness is inferred until a real verdict is produced.
                      </p>
                    )}
                  </div>
                )}

                {ev.explanation && (
                  <div className="p-3 rounded-lg bg-slate-50 border border-slate-200 text-sm text-slate-700 leading-relaxed">
                    <strong className="text-slate-900">Explanation:</strong> {ev.explanation}
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
