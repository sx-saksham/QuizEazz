import React, { useCallback } from 'react'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { EmptyState } from '../common/EmptyState'
import { ShieldAlert, RefreshCw, AlertCircle } from 'lucide-react'
import type { SecurityEventRow } from '../../types'
import { fetchSecurityEvents } from '../../lib/teacherApi'
import { useAsyncData } from '../../hooks/useAsyncData'

const badgeFor = (type: string) => {
  if (type === 'fullscreen_exit') return 'danger' as const
  if (type === 'tab_switch' || type === 'window_blur') return 'warning' as const
  return 'neutral' as const
}

export const TeacherAnalyticsView: React.FC = () => {
  const load = useCallback(
    () => fetchSecurityEvents() as Promise<SecurityEventRow[]>,
    [],
  )
  const { data: events, error: fetchError, loading, reload } = useAsyncData<SecurityEventRow[]>(
    load,
    [],
    { errorFallback: 'Unable to load the security audit log.' },
  )

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-bold tracking-tight text-slate-900 sm:text-2xl">
              Security Event Log
            </h1>
            <Badge variant="warning" size="sm">
              Objective Records
            </Badge>
          </div>
          <p className="mt-1 text-xs text-slate-500">
            Factual browser and window events recorded during candidate attempts
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

      <div className="rounded-xl border border-blue-100 bg-blue-50/50 p-4 text-xs text-blue-900 flex items-start gap-3">
        <ShieldAlert className="h-5 w-5 text-blue-600 shrink-0 mt-0.5" />
        <div>
          <p className="font-semibold">How to read this log</p>
          <p className="mt-0.5 text-2xs text-blue-800 leading-relaxed">
            These are objective, timestamped facts recorded by the candidate browser: window blur,
            tab switches and fullscreen exits. They are supplied for physical invigilator review.
            QuizEazz does not label any candidate as a cheater and applies no automatic penalty.
            Browser events cannot detect OS-level screenshots or keyboard shortcuts — physical
            invigilation and a locked-down workstation remain the actual controls.
          </p>
        </div>
      </div>

      {loading ? (
        <div className="rounded-xl border border-slate-200 bg-white p-12 text-center">
          <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-indigo-600 border-t-transparent" />
          <p className="mt-3 text-xs font-medium text-slate-500">Loading audit records...</p>
        </div>
      ) : fetchError ? (
        <div className="rounded-xl border border-red-200 bg-red-50/50 p-8 text-center">
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-red-100 text-red-600 mb-3">
            <AlertCircle className="h-5 w-5" />
          </div>
          <h3 className="text-sm font-semibold text-red-900">Failed to Load Audit Log</h3>
          <p className="mt-1 text-xs text-red-600 max-w-md mx-auto">{fetchError}</p>
          <div className="mt-4">
            <Button variant="secondary" size="sm" onClick={reload}>
              Retry
            </Button>
          </div>
        </div>
      ) : events.length === 0 ? (
        <EmptyState
          icon={<ShieldAlert className="h-6 w-6 text-slate-400" />}
          title="No Security Events Recorded"
          description="Objective browser events appear here when a candidate interrupts focus during an assigned examination."
        />
      ) : (
        <div className="rounded-xl border border-slate-200 bg-white overflow-hidden shadow-2xs">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs text-slate-600">
              <thead className="bg-slate-50 text-2xs font-semibold uppercase tracking-wider text-slate-500 border-b border-slate-200">
                <tr>
                  <th className="py-3 px-4">Timestamp</th>
                  <th className="py-3 px-4">Candidate</th>
                  <th className="py-3 px-4">Examination</th>
                  <th className="py-3 px-4">Event</th>
                  <th className="py-3 px-4">Detail</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {events.map((ev) => (
                  <tr key={ev.id} className="hover:bg-slate-50/80">
                    <td className="py-3 px-4 font-mono text-2xs text-slate-700 whitespace-nowrap">
                      {new Date(ev.event_timestamp).toLocaleString()}
                    </td>
                    <td className="py-3 px-4">
                      <span className="block font-medium text-slate-800">
                        {ev.student_name || 'Unknown'}
                      </span>
                      <span className="block font-mono text-2xs text-slate-400">{ev.student_id}</span>
                    </td>
                    <td className="py-3 px-4 text-slate-600">{ev.exam_title || ev.exam_id}</td>
                    <td className="py-3 px-4">
                      <Badge variant={badgeFor(ev.event_type)} size="sm">
                        {ev.event_type.replace(/_/g, ' ')}
                      </Badge>
                    </td>
                    <td className="py-3 px-4 text-slate-500 text-2xs max-w-xs truncate">
                      {typeof (ev.details as { detail?: string })?.detail === 'string'
                        ? ((ev.details as { detail: string }).detail as string)
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
