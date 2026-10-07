import React, { useCallback } from 'react'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { Card, CardContent } from '../common/Card'
import { NotificationBanner } from '../common/NotificationBanner'
import { RefreshCw } from 'lucide-react'
import { fetchAdminProfiles } from '../../lib/teacherApi'
import { useAsyncData } from '../../hooks/useAsyncData'

interface AdminProfile {
  id: string
  full_name: string | null
  role: string
  email: string | null
  created_at: string | null
}

export const AdminDashboard: React.FC = () => {
  // RPC-only: public.profiles is not directly readable by a client role.
  const load = useCallback(() => fetchAdminProfiles() as Promise<AdminProfile[]>, [])
  const { data: profiles, error, loading, reload } = useAsyncData<AdminProfile[]>(load, [], {
    errorFallback: 'Unable to load the user directory.',
  })

  return (
    <div className="space-y-6">
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-2xs">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-bold tracking-tight text-slate-900 sm:text-2xl">
                Institution Administration
              </h1>
              <Badge variant="warning" size="sm">
                System Admin
              </Badge>
            </div>
            <p className="mt-1 text-xs text-slate-500">
              Provisioned accounts on this college instance
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
      </div>

      {error && <NotificationBanner type="error" message={error} onClose={reload} />}

      <Card>
        <div className="px-5 py-4 border-b border-slate-100">
          <h3 className="text-base font-semibold text-slate-900 tracking-tight">
            Authorized Accounts
          </h3>
          <p className="text-xs text-slate-500 mt-1">
            Roles are set through the database. No client can escalate a role.
          </p>
        </div>

        <CardContent className="p-0">
          {loading ? (
            <div className="p-10 text-center text-xs text-slate-500">Loading accounts...</div>
          ) : profiles.length === 0 ? (
            <div className="p-10 text-center text-xs text-slate-500">No accounts found.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs text-slate-600">
                <thead className="bg-slate-50 text-2xs font-semibold uppercase tracking-wider text-slate-500 border-b border-slate-200">
                  <tr>
                    <th className="py-3 px-4">Name</th>
                    <th className="py-3 px-4">Email</th>
                    <th className="py-3 px-4">Role</th>
                    <th className="py-3 px-4 text-right">Registered</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {profiles.map((p) => (
                    <tr key={p.id} className="hover:bg-slate-50/80">
                      <td className="py-3 px-4 font-semibold text-slate-900">
                        {p.full_name || 'Unnamed'}
                      </td>
                      <td className="py-3 px-4 text-slate-600">{p.email || '—'}</td>
                      <td className="py-3 px-4">
                        <Badge
                          variant={
                            p.role === 'admin' ? 'warning' : p.role === 'teacher' ? 'purple' : 'default'
                          }
                          size="sm"
                        >
                          {p.role.toUpperCase()}
                        </Badge>
                      </td>
                      <td className="py-3 px-4 text-right text-slate-400 whitespace-nowrap">
                        {p.created_at ? new Date(p.created_at).toLocaleDateString() : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
