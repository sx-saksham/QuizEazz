import React, { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { FileText, ArrowRight, AlertCircle } from 'lucide-react'
import { Button } from '../common/Button'
import { fetchMyResults, type ResultRow } from '../../lib/studentApi'
import { attemptEvaluationState } from '../../lib/attemptStatus'
import { useAsyncData } from '../../hooks/useAsyncData'

export const StudentResultsPage: React.FC = () => {
  const navigate = useNavigate()

  const load = useCallback(() => fetchMyResults(), [])
  const { data: results, error, loading, reload } = useAsyncData<ResultRow[]>(load, [], {
    errorFallback: 'Unable to load your results.',
  })

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900">Results</h1>
          <p className="mt-1 text-xs text-slate-500">
            Submitted examinations, scores, and evaluation status.
          </p>
        </div>
      </div>

      {loading ? (
        <div className="py-16 text-center text-xs text-slate-500">Loading results...</div>
      ) : error ? (
        <div className="rounded-xl border border-rose-200 bg-rose-50/60 p-8 text-center">
          <AlertCircle className="h-5 w-5 text-rose-500 mx-auto mb-2" />
          <h3 className="text-sm font-semibold text-rose-900">Unable to Load Results</h3>
          <p className="mt-1 text-xs text-rose-700">{error}</p>
          <div className="mt-4">
            <Button variant="secondary" size="sm" onClick={reload}>
              Try Again
            </Button>
          </div>
        </div>
      ) : results.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-200 bg-white p-12 text-center max-w-lg mx-auto">
          <FileText className="h-8 w-8 text-slate-400 mx-auto mb-3" />
          <h3 className="text-sm font-semibold text-slate-900">No results yet</h3>
          <p className="text-xs text-slate-500 mt-1.5 leading-relaxed">
            Once you submit an examination, your score and a full question-by-question review will
            appear here.
          </p>
        </div>
      ) : (
        <div className="rounded-xl border border-slate-200 bg-white overflow-hidden shadow-2xs">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="bg-slate-50 border-b border-slate-200 text-slate-600 font-semibold">
                <tr>
                  <th className="py-3 px-4">Examination</th>
                  <th className="py-3 px-4">Submitted</th>
                  <th className="py-3 px-4 text-right">Score</th>
                  <th className="py-3 px-4 text-right">Percentage</th>
                  <th className="py-3 px-4">Evaluation</th>
                  <th className="py-3 px-4 text-right">Review</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 text-slate-700">
                {results.map((res) => {
                  const pct =
                    res.score !== null && res.total_marks > 0
                      ? Math.round((res.score / res.total_marks) * 100)
                      : null

                  // Authoritative mapping via the shared helper: attempt status
                  // alone decides. `evaluated` is final (score/percentage/
                  // status all final). `in_progress` is live (no submitted
                  // timestamp, no score). Anything else is genuinely
                  // unfinished evaluation.
                  const state = attemptEvaluationState(res.status)
                  const isLive = state === 'in_progress'
                  const evaluation =
                    state === 'evaluated'
                      ? { label: 'Evaluated', cls: 'bg-emerald-50 text-emerald-700' }
                      : state === 'in_progress'
                      ? { label: 'In Progress', cls: 'bg-blue-50 text-blue-700' }
                      : { label: 'Evaluation in progress', cls: 'bg-amber-50 text-amber-800' }

                  return (
                    <tr key={res.id} className="hover:bg-slate-50/80 transition-colors">
                      <td className="py-3.5 px-4 font-semibold text-slate-900">{res.exam_title}</td>
                      <td className="py-3.5 px-4 text-slate-500 whitespace-nowrap">
                        {res.submitted_at
                          ? new Date(res.submitted_at).toLocaleString('en-US', {
                              day: '2-digit',
                              month: '2-digit',
                              year: 'numeric',
                              hour: '2-digit',
                              minute: '2-digit',
                              hour12: true,
                            })
                          : 'Not submitted'}
                      </td>
                      <td className="py-3.5 px-4 text-right font-medium">
                        {isLive || res.score === null ? '—' : `${res.score} / ${res.total_marks}`}
                      </td>
                      <td className="py-3.5 px-4 text-right font-semibold text-slate-900">
                        {isLive || pct === null ? '—' : `${pct}%`}
                      </td>
                      <td className="py-3.5 px-4">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded text-2xs font-semibold ${evaluation.cls}`}
                        >
                          {evaluation.label}
                        </span>
                      </td>
                      <td className="py-3.5 px-4 text-right">
                        <button
                          onClick={() => navigate(`/student/review/${res.id}`)}
                          className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-300 text-slate-700 hover:bg-slate-100 font-semibold text-xs transition-colors"
                        >
                          <span>Review</span>
                          <ArrowRight className="h-3 w-3" />
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
