import React, { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { FileText, ArrowRight, AlertCircle } from 'lucide-react'
import { fetchMyResults, type ResultRow } from '../../lib/studentApi'
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
            Your programming answers are being evaluated in an isolated environment. No marks are awarded until a verdict is produced.
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
        <div className="py-16 text-center text-xs text-slate-500">Loading results...</div>
      ) : error ? (
        <div className="rounded-xl border border-rose-200 bg-rose-50/60 p-8 text-center">
          <AlertCircle className="h-5 w-5 text-rose-500 mx-auto mb-2" />
          <h3 className="text-sm font-semibold text-rose-900">Unable to Load Results</h3>
          <p className="mt-1 text-xs text-rose-700">{error}</p>
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

                  // "Evaluated" is only truthful when nothing is still waiting
                  // on a real verdict. This count is supplied by the server.
                  // A missing value is treated as "unknown -> pending" so the
                  // UI never over-claims a completed evaluation.
                  const pending = res.programming_pending
                  const isPending = pending === undefined || pending > 0

                  return (
                    <tr key={res.id} className="hover:bg-slate-50/80 transition-colors">
                      <td className="py-3.5 px-4 font-semibold text-slate-900">{res.exam_title}</td>
                      <td className="py-3.5 px-4 text-slate-500 whitespace-nowrap">
                        {res.date ? new Date(res.date).toLocaleDateString('en-GB') : '—'}
                      </td>
                      <td className="py-3.5 px-4 text-right font-medium">
                        {res.score !== null ? `${res.score} / ${res.total_marks}` : '—'}
                        {isPending && res.score !== null && (
                          <span className="block text-2xs font-normal text-slate-500">
                            provisional
                          </span>
                        )}
                      </td>
                      <td className="py-3.5 px-4 text-right font-semibold text-slate-900">
                        {pct !== null ? `${pct}%` : '—'}
                      </td>
                      <td className="py-3.5 px-4">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold ${
                            isPending
                              ? 'bg-amber-50 text-amber-800'
                              : 'bg-emerald-50 text-emerald-700'
                          }`}
                        >
                          {isPending
                            ? 'Evaluation Pending'
                            : 'Evaluated'}
                        </span>
                        {isPending && (
                          <span className="block text-2xs text-slate-500 mt-0.5">
                            {pending === undefined
                              ? 'Code execution pending'
                              : `${pending} programming question${pending === 1 ? '' : 's'} awaiting execution`}
                          </span>
                        )}
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
