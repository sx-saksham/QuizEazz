import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Clock, Calendar, FileQuestion, ArrowRight, CheckCircle2, Lock, AlertCircle } from 'lucide-react'
import { fetchMyExams, type CandidateExam } from '../../lib/studentApi'
import { useAsyncData } from '../../hooks/useAsyncData'

type CardState = 'upcoming' | 'open' | 'in_progress' | 'completed'

const stateOf = (exam: CandidateExam, now: number): CardState => {
  const status = exam.attempt?.status
  if (status === 'submitted' || status === 'evaluated' || status === 'timed_out') {
    return 'completed'
  }
  if (status === 'in_progress') return 'in_progress'
  if (exam.status !== 'published') return 'upcoming'
  if (exam.starts_at && new Date(exam.starts_at).getTime() > now) return 'upcoming'
  if (exam.ends_at && new Date(exam.ends_at).getTime() < now) return 'upcoming'
  return 'open'
}

const STATE_META: Record<CardState, { label: string; className: string }> = {
  upcoming: { label: 'Upcoming', className: 'bg-slate-100 text-slate-600' },
  open: { label: 'Open', className: 'bg-emerald-50 text-emerald-700' },
  in_progress: { label: 'In Progress', className: 'bg-amber-50 text-amber-700' },
  completed: { label: 'Completed', className: 'bg-indigo-50 text-indigo-700' },
}

/**
 * "Start Exam" / "Resume Exam" only navigates.
 *
 * It deliberately does NOT request fullscreen from here. A request issued on
 * this page had to be verified on another page, and its result was discarded,
 * so a rejection was converted into a silent "looks fine" state with nothing
 * checking whether Chrome had actually hidden its chrome. Fullscreen is now
 * owned exclusively by the exam workspace, which requests it from a dedicated
 * click and then verifies `document.fullscreenElement` before allowing the
 * candidate to begin. See src/lib/fullscreen.ts and ExamTakingScreen.tsx.
 */

export const StudentExamsPage: React.FC = () => {
  const navigate = useNavigate()
  const [now, setNow] = useState(() => Date.now())

  const load = useCallback(() => fetchMyExams(), [])
  const { data: exams, error, loading, reload } = useAsyncData<CandidateExam[]>(load, [], {
    errorFallback: 'Unable to load your assigned examinations.',
  })

  // Keeps the Open / Upcoming boundary live without a reload.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [])

  const fmtDate = (v: string | null) =>
    v
      ? new Date(v).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
      : 'Open schedule'

  const fmtTime = (v: string | null) =>
    v ? new Date(v).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—'

  const cards = useMemo(
    () =>
      exams
        .map((exam) => ({ exam, state: stateOf(exam, now) }))
        .sort((a, b) => {
          // Actionable first, then by nearest start time.
          const rank = (s: CardState) => (s === 'in_progress' ? 0 : s === 'open' ? 1 : s === 'upcoming' ? 2 : 3)
          if (rank(a.state) !== rank(b.state)) return rank(a.state) - rank(b.state)
          return (a.exam.starts_at ?? '').localeCompare(b.exam.starts_at ?? '')
        }),
    [exams, now],
  )

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900">My Exams</h1>
          <p className="mt-1 text-xs text-slate-500">
            Examinations assigned to you by your department
          </p>
        </div>
        <button
          onClick={reload}
          disabled={loading}
          className="text-xs font-medium text-slate-500 hover:text-indigo-600 transition-colors disabled:opacity-50"
        >
          {loading ? 'Refreshing...' : 'Refresh'}
        </button>
      </div>

      {loading ? (
        <div className="py-16 text-center text-xs text-slate-500">Loading assigned examinations...</div>
      ) : error ? (
        <div className="rounded-xl border border-rose-200 bg-rose-50/60 p-8 text-center">
          <AlertCircle className="h-5 w-5 text-rose-500 mx-auto mb-2" />
          <h3 className="text-sm font-semibold text-rose-900">Unable to Load Examinations</h3>
          <p className="mt-1 text-xs text-rose-700">{error}</p>
          <button
            onClick={reload}
            className="mt-4 px-3 py-1.5 rounded-lg border border-rose-300 text-xs font-semibold text-rose-700 hover:bg-rose-100"
          >
            Retry
          </button>
        </div>
      ) : cards.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-200 bg-white p-12 text-center max-w-lg mx-auto">
          <FileQuestion className="h-8 w-8 text-slate-400 mx-auto mb-3" />
          <h3 className="text-sm font-semibold text-slate-900">No examinations assigned</h3>
          <p className="text-xs text-slate-500 mt-1.5 leading-relaxed">
            When your faculty assigns you an examination it will appear here with its schedule,
            duration and question count.
          </p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {cards.map(({ exam, state }) => {
            const meta = STATE_META[state]
            const opensAt = exam.starts_at ? new Date(exam.starts_at) : null

            return (
              <div
                key={exam.id}
                className="rounded-xl border border-slate-200 bg-white p-5 flex flex-col justify-between hover:border-slate-300 transition-all shadow-2xs"
              >
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-semibold text-slate-500 truncate">
                      {exam.description || 'Technical Assessment'}
                    </span>
                    <span
                      className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded shrink-0 ${meta.className}`}
                    >
                      {meta.label}
                    </span>
                  </div>

                  <h3 className="text-sm font-bold text-slate-900 leading-snug">{exam.title}</h3>

                  <div className="space-y-1.5 text-xs text-slate-600 pt-1 border-t border-slate-100">
                    <div className="flex items-center gap-1.5">
                      <Calendar className="h-3.5 w-3.5 text-slate-400 shrink-0" />
                      <span>
                        {fmtDate(exam.starts_at)} · {fmtTime(exam.starts_at)}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Clock className="h-3.5 w-3.5 text-slate-400 shrink-0" />
                      <span>
                        {exam.duration_minutes} min · {exam.total_questions} question(s) ·{' '}
                        {exam.total_marks} marks
                      </span>
                    </div>
                  </div>

                  {state === 'upcoming' && opensAt && (
                    <p className="text-2xs text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-2 py-1.5">
                      Available from {opensAt.toLocaleString()}. The server will not open this
                      examination early.
                    </p>
                  )}
                  {state === 'in_progress' && (
                    <p className="text-2xs text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-2 py-1.5">
                      Attempt in progress. The timer continues from when you started.
                    </p>
                  )}
                </div>

                <div className="pt-4 mt-4 border-t border-slate-100">
                  {state === 'open' && (
                    <button
                      onClick={() => navigate(`/student/exam/${exam.id}`)}
                      className="w-full inline-flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-xs font-semibold bg-indigo-600 text-white hover:bg-indigo-700 transition-colors"
                    >
                      <span>Start Exam</span>
                      <ArrowRight className="h-3.5 w-3.5" />
                    </button>
                  )}

                  {state === 'in_progress' && (
                    <button
                      onClick={() => navigate(`/student/exam/${exam.id}`)}
                      className="w-full inline-flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-xs font-semibold bg-amber-600 text-white hover:bg-amber-700 transition-colors"
                    >
                      <span>Resume Exam</span>
                      <ArrowRight className="h-3.5 w-3.5" />
                    </button>
                  )}

                  {state === 'upcoming' && (
                    <div className="w-full py-2 px-3 rounded-lg text-xs font-semibold bg-slate-100 text-slate-400 text-center flex items-center justify-center gap-1.5">
                      <Lock className="h-3 w-3" />
                      Not open yet
                    </div>
                  )}

                  {state === 'completed' && (
                    <button
                      onClick={() =>
                        navigate(
                          exam.attempt ? `/student/review/${exam.attempt.id}` : '/student/results',
                        )
                      }
                      className="w-full py-2 px-3 rounded-lg text-xs font-semibold border border-slate-300 text-slate-700 hover:bg-slate-50 transition-colors inline-flex items-center justify-center gap-2"
                    >
                      {exam.attempt?.score !== null && exam.attempt?.score !== undefined ? (
                        <>
                          <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
                          View Result ({exam.attempt.score} / {exam.total_marks})
                        </>
                      ) : (
                        <>
                          <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
                          View Submission
                        </>
                      )}
                    </button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
