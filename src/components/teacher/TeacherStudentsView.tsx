import React, { useCallback, useMemo, useState } from 'react'
import { Card, CardContent } from '../common/Card'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { EmptyState } from '../common/EmptyState'
import { NotificationBanner } from '../common/NotificationBanner'
import { Users, Search, RefreshCw, AlertCircle } from 'lucide-react'
import { fetchCandidates, type Candidate } from '../../lib/teacherApi'
import { useAsyncData } from '../../hooks/useAsyncData'

export const TeacherStudentsView: React.FC = () => {
  const [searchQuery, setSearchQuery] = useState<string>('')

  // RPC-only. No direct fallback to public.profiles.
  const load = useCallback(() => fetchCandidates(), [])
  const { data: students, error: fetchError, loading, reload } = useAsyncData<Candidate[]>(
    load,
    [],
    { errorFallback: 'Unable to load the candidate registry.' },
  )

  const filteredStudents = useMemo(() => {
    const term = searchQuery.trim().toLowerCase()
    if (!term) return students
    return students.filter(
      (s) =>
        s.full_name?.toLowerCase().includes(term) ||
        s.email?.toLowerCase().includes(term) ||
        s.id.toLowerCase().includes(term),
    )
  }, [students, searchQuery])

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900 sm:text-2xl">
            Candidate Student Registry
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            Registered candidate accounts eligible for technical examination assignment
          </p>
        </div>

        <Button
          variant="outline"
          size="sm"
          icon={<RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />}
          onClick={reload}
          disabled={loading}
        >
          Refresh Roster
        </Button>
      </div>

      {fetchError && (
        <NotificationBanner type="error" message={fetchError} onClose={reload} />
      )}

      <Card>
        <CardContent className="p-4 flex items-center justify-between">
          <div className="relative w-full sm:w-80">
            <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
            <input
              type="text"
              placeholder="Search candidate by name..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-9 pr-4 py-2 text-xs rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
            />
          </div>

          <span className="text-xs font-semibold text-slate-500">
            Total Candidates: {filteredStudents.length}
          </span>
        </CardContent>
      </Card>

      {loading ? (
        <div className="rounded-xl border border-slate-200 bg-white p-12 text-center">
          <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-indigo-600 border-t-transparent" />
          <p className="mt-3 text-xs font-medium text-slate-500">Loading student registry...</p>
        </div>
      ) : fetchError ? (
        <div className="rounded-xl border border-red-200 bg-red-50/50 p-8 text-center">
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-red-100 text-red-600 mb-3">
            <AlertCircle className="h-5 w-5" />
          </div>
          <h3 className="text-sm font-semibold text-red-900">Failed to Load Candidates</h3>
          <p className="mt-1 text-xs text-red-600 max-w-md mx-auto">{fetchError}</p>
        </div>
      ) : filteredStudents.length === 0 ? (
        <EmptyState
          icon={<Users className="h-6 w-6 text-slate-400" />}
          title="No Students Registered Yet"
          description="When students register on the candidate portal, their profiles will automatically appear here ready for exam cohort assignment."
        />
      ) : (
        <div className="rounded-xl border border-slate-200 bg-white overflow-hidden shadow-2xs">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs text-slate-600">
              <thead className="bg-slate-50 text-2xs font-semibold uppercase tracking-wider text-slate-500 border-b border-slate-200">
                <tr>
                  <th className="py-3 px-4">Candidate Full Name</th>
                  <th className="py-3 px-4">Institutional Email</th>
                  <th className="py-3 px-4">Roll / UUID</th>
                  <th className="py-3 px-4">Role Designation</th>
                  <th className="py-3 px-4 text-right">Registration Date</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {filteredStudents.map((s) => (
                  <tr key={s.id} className="hover:bg-slate-50/80">
                    <td className="py-3 px-4 font-semibold text-slate-900">
                      <span className="flex items-center gap-2">
                        <span className="h-7 w-7 shrink-0 rounded-full bg-blue-50 text-blue-700 flex items-center justify-center font-bold text-2xs border border-blue-100">
                          {s.full_name ? s.full_name.charAt(0).toUpperCase() : 'S'}
                        </span>
                        <span className="truncate">{s.full_name || 'Unnamed'}</span>
                      </span>
                    </td>
                    <td className="py-3 px-4 text-slate-600 text-xs font-medium">
                      {s.email || '—'}
                    </td>
                    <td className="py-3 px-4 font-mono text-2xs text-slate-500 break-all">{s.id}</td>
                    <td className="py-3 px-4">
                      <Badge variant="default" size="sm">STUDENT</Badge>
                    </td>
                    <td className="py-3 px-4 text-right text-slate-400 text-2xs whitespace-nowrap">
                      {s.created_at ? new Date(s.created_at).toLocaleDateString() : '—'}
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
