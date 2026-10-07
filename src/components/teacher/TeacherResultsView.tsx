import React, { useCallback, useState } from 'react'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { EmptyState } from '../common/EmptyState'
import { Modal } from '../common/Modal'
import { NotificationBanner } from '../common/NotificationBanner'
import { Award, RefreshCw, FileCode, AlertCircle, CheckCircle2, XCircle, Clock3 } from 'lucide-react'
import { fetchAttemptDetail, fetchAttempts, type AttemptDetail, type AttemptRow } from '../../lib/teacherApi'
import { useAsyncData } from '../../hooks/useAsyncData'
import { toErrorMessage } from '../../lib/supabase'

const outcomeBadge = (outcome: string) => {
  if (outcome === 'Correct') return { variant: 'success' as const, Icon: CheckCircle2 }
  if (outcome === 'Wrong Answer') return { variant: 'danger' as const, Icon: XCircle }
  if (outcome === 'Pending Evaluation') return { variant: 'warning' as const, Icon: Clock3 }
  return { variant: 'neutral' as const, Icon: AlertCircle }
}

export const TeacherResultsView: React.FC = () => {
  const [detail, setDetail] = useState<AttemptDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  // RPC-only: exam_attempts is not directly readable by any client role.
  const loadAttempts = useCallback(() => fetchAttempts(), [])
  const { data: attempts, error: fetchError, loading, reload } = useAsyncData<AttemptRow[]>(
    loadAttempts,
    [],
    { errorFallback: 'Unable to load candidate submissions.' },
  )

  const inspect = async (attemptId: string) => {
    setDetailLoading(true)
    setDetailError(null)
    setDetail(null)
    try {
      setDetail(await fetchAttemptDetail(attemptId))
    } catch (err) {
      setDetailError(toErrorMessage(err, 'Unable to load the submission evidence.'))
    } finally {
      setDetailLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900 sm:text-2xl">
            Results &amp; Submissions
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            Objective evaluation evidence recorded by the server after final submission
          </p>
        </div>

        <Button
          variant="outline"
          size="sm"
          icon={<RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />}
          onClick={reload}
          disabled={loading}
        >
          Refresh
        </Button>
      </div>

      {loading ? (
        <div className="rounded-xl border border-slate-200 bg-white p-12 text-center">
          <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-indigo-600 border-t-transparent" />
          <p className="mt-3 text-xs font-medium text-slate-500">Loading submissions...</p>
        </div>
      ) : fetchError ? (
        <div className="rounded-xl border border-red-200 bg-red-50/50 p-8 text-center">
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-red-100 text-red-600 mb-3">
            <AlertCircle className="h-5 w-5" />
          </div>
          <h3 className="text-sm font-semibold text-red-900">Failed to Load Submissions</h3>
          <p className="mt-1 text-xs text-red-600 max-w-md mx-auto">{fetchError}</p>
          <div className="mt-4">
            <Button variant="secondary" size="sm" onClick={reload}>
              Retry
            </Button>
          </div>
        </div>
      ) : attempts.length === 0 ? (
        <EmptyState
          icon={<Award className="h-6 w-6 text-slate-400" />}
          title="No Submissions Yet"
          description="Once candidates submit an assigned examination, their server-side evaluation will appear here."
        />
      ) : (
        <div className="rounded-xl border border-slate-200 bg-white overflow-hidden shadow-2xs">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs text-slate-600">
              <thead className="bg-slate-50 text-2xs font-semibold uppercase tracking-wider text-slate-500 border-b border-slate-200">
                <tr>
                  <th className="py-3 px-4">Candidate</th>
                  <th className="py-3 px-4">Examination</th>
                  <th className="py-3 px-4">Status</th>
                  <th className="py-3 px-4">Submitted</th>
                  <th className="py-3 px-4 text-right">Score</th>
                  <th className="py-3 px-4 text-right">Evidence</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {attempts.map((a) => (
                  <tr key={a.attempt_id} className="hover:bg-slate-50/80">
                    <td className="py-3 px-4">
                      <span className="block font-semibold text-slate-900">
                        {a.student_name || 'Unknown candidate'}
                      </span>
                      <span className="block font-mono text-2xs text-slate-400">{a.student_id}</span>
                    </td>
                    <td className="py-3 px-4 font-medium text-slate-900">{a.exam_title}</td>
                    <td className="py-3 px-4">
                      <Badge
                        variant={
                          a.status === 'evaluated'
                            ? 'success'
                            : a.status === 'in_progress'
                            ? 'info'
                            : 'warning'
                        }
                        size="sm"
                      >
                        {a.status.replace('_', ' ').toUpperCase()}
                      </Badge>
                    </td>
                    <td className="py-3 px-4 text-slate-400">
                      {a.submitted_at ? new Date(a.submitted_at).toLocaleString() : 'Not submitted'}
                    </td>
                    <td className="py-3 px-4 text-right font-bold text-slate-900">
                      {a.score !== null ? `${a.score} / ${a.total_marks}` : '—'}
                    </td>
                    <td className="py-3 px-4 text-right">
                      <Button variant="outline" size="sm" onClick={() => void inspect(a.attempt_id)}>
                        Inspect
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <Modal
        isOpen={detailLoading || !!detail || !!detailError}
        onClose={() => {
          setDetail(null)
          setDetailError(null)
        }}
        title="Submission Evidence"
        subtitle={detail ? `${detail.total_marks} marks paper` : undefined}
        maxWidth="xl"
        footer={
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setDetail(null)
              setDetailError(null)
            }}
          >
            Close
          </Button>
        }
      >
        {detailLoading ? (
          <div className="py-8 text-center text-slate-500">Loading evidence...</div>
        ) : detailError ? (
          <NotificationBanner type="error" message={detailError} />
        ) : detail ? (
          <div className="space-y-4 text-xs">
            <div className="grid grid-cols-3 gap-3 p-3 rounded-xl bg-slate-50 border border-slate-200 text-center">
              <div>
                <span className="text-2xs uppercase text-slate-400 font-semibold block">Status</span>
                <span className="font-bold text-slate-800 capitalize">{detail.status}</span>
              </div>
              <div>
                <span className="text-2xs uppercase text-slate-400 font-semibold block">Score</span>
                <span className="font-bold text-indigo-700 text-sm">
                  {detail.score} / {detail.total_marks}
                </span>
              </div>
              <div>
                <span className="text-2xs uppercase text-slate-400 font-semibold block">Submitted</span>
                <span className="font-medium text-slate-600 text-2xs">
                  {detail.submitted_at ? new Date(detail.submitted_at).toLocaleString() : '—'}
                </span>
              </div>
            </div>

            {detail.items.length === 0 ? (
              <p className="py-6 text-center text-slate-500">No questions are assigned to this paper.</p>
            ) : (
              <div className="space-y-3">
                {detail.items.map((item, idx) => {
                  const badge = outcomeBadge(item.outcome)
                  const Icon = badge.Icon
                  return (
                    <div
                      key={item.question_id}
                      className="p-3.5 rounded-xl border border-slate-200 bg-white space-y-2"
                    >
                      <div className="flex items-center justify-between gap-2 pb-2 border-b border-slate-100">
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="font-bold text-slate-900 shrink-0">Q{idx + 1}</span>
                          <span className="text-slate-700 font-medium truncate">{item.title}</span>
                          <span className="text-[10px] uppercase font-bold text-slate-400 bg-slate-100 px-1.5 py-0.5 rounded shrink-0">
                            {item.question_type}
                          </span>
                        </div>

                        <div className="flex items-center gap-2 shrink-0">
                          <Badge variant={badge.variant} size="sm">
                            <Icon className="h-3 w-3" />
                            {item.outcome}
                          </Badge>
                          <span className="font-bold text-slate-800">
                            {item.marks_awarded} / {item.marks_total}
                          </span>
                        </div>
                      </div>

                      {item.question_type === 'coding' ? (
                        <div className="space-y-1.5">
                          <div className="flex items-center justify-between text-2xs text-slate-500">
                            <span className="flex items-center gap-1 font-semibold text-slate-700">
                              <FileCode className="h-3 w-3 text-indigo-600" />
                              Submitted code ({item.language || 'text'})
                            </span>
                          </div>
                          <pre className="p-2.5 rounded-lg bg-slate-900 text-emerald-300 font-mono text-2xs overflow-x-auto max-h-56">
                            {item.code || '# No code submitted'}
                          </pre>
                          {item.outcome === 'Pending Evaluation' && (
                            <p className="text-2xs text-amber-700 bg-amber-50 border border-amber-200 p-2 rounded">
                              Awaiting Judge0 CE execution. No marks are awarded until an isolated
                              runner produces a verdict.
                            </p>
                          )}
                        </div>
                      ) : (
                        <div className="space-y-1">
                          {item.options?.map((opt, oi) => {
                            const isChosen = item.student_option_id === opt.id
                            const isKey = item.correct_option_id === opt.id
                            const cls = isKey
                              ? 'border-emerald-300 bg-emerald-50/70'
                              : isChosen
                              ? 'border-rose-300 bg-rose-50/70'
                              : 'border-slate-200 bg-white'
                            return (
                              <div
                                key={opt.id}
                                className={`p-2 rounded-lg border flex items-center justify-between ${cls}`}
                              >
                                <span className="flex items-center gap-2">
                                  <span className="font-bold text-2xs uppercase text-slate-400">
                                    {String.fromCharCode(65 + oi)}
                                  </span>
                                  <span className="text-slate-700">{opt.text}</span>
                                </span>
                                <span className="flex items-center gap-1.5 text-2xs font-semibold">
                                  {isChosen && (
                                    <span className="px-1.5 py-0.5 rounded bg-slate-200 text-slate-700">
                                      Chosen
                                    </span>
                                  )}
                                  {isKey && (
                                    <span className="px-1.5 py-0.5 rounded bg-emerald-600 text-white">
                                      Key
                                    </span>
                                  )}
                                </span>
                              </div>
                            )
                          })}
                          {!item.student_option_id && (
                            <p className="text-2xs text-slate-400">Not answered.</p>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        ) : null}
      </Modal>
    </div>
  )
}
