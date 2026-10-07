import React from 'react'
import { useAuth } from '../../context/AuthContext'
import {
  GraduationCap,
  Mail,
  User,
  Calendar,
  KeyRound,
  CheckCircle2,
  Lock,
} from 'lucide-react'
import { Badge } from '../common/Badge'

export const StudentProfilePage: React.FC = () => {
  const { user, profile } = useAuth()

  const displayName =
    profile?.full_name || (user?.user_metadata?.full_name as string | undefined) || 'Candidate'
  const email = user?.email || '—'
  const isFaculty = profile?.role === 'teacher' || profile?.role === 'admin'
  const roleName = profile?.role === 'admin' ? 'Administrator' : isFaculty ? 'Faculty' : 'Student Candidate'
  const registrationDate = profile?.created_at || user?.created_at

  const initials =
    displayName
      .split(' ')
      .filter(Boolean)
      .map((n: string) => n[0])
      .join('')
      .substring(0, 2)
      .toUpperCase() || 'ST'

  return (
    <div className="space-y-6 max-w-3xl mx-auto pb-8">
      <div className="rounded-2xl border border-indigo-100 bg-gradient-to-r from-purple-50/80 via-indigo-50/50 to-blue-50/40 p-6 sm:p-7 shadow-2xs">
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 rounded-xl bg-indigo-600 text-white flex items-center justify-center shrink-0">
            <GraduationCap className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight text-slate-900 sm:text-2xl">
              Candidate Profile
            </h1>
            <p className="mt-1 text-xs text-slate-500 font-medium">
              Registered account details for your examination sessions
            </p>
          </div>
        </div>
      </div>

      <div className="rounded-2xl border border-slate-200/90 bg-white shadow-2xs overflow-hidden">
        <div className="p-6 sm:p-7 border-b border-slate-100 bg-slate-50/40 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div className="flex items-center gap-4">
            <div className="h-16 w-16 rounded-2xl bg-gradient-to-tr from-indigo-600 to-purple-600 text-white flex items-center justify-center text-xl font-bold tracking-wider shadow-sm ring-4 ring-indigo-50 shrink-0">
              {initials}
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-lg font-bold text-slate-900">{displayName}</h2>
                <Badge variant={isFaculty ? 'purple' : 'success'} size="sm">
                  <CheckCircle2 className="w-3 h-3 mr-1" />
                  {isFaculty ? 'Faculty' : 'Registered Candidate'}
                </Badge>
              </div>
              <p className="text-xs text-slate-500 font-medium mt-0.5">{email}</p>
            </div>
          </div>

          <div className="flex sm:flex-col sm:items-end justify-between border-t border-slate-200/60 sm:border-0 pt-3 sm:pt-0">
            <span className="text-2xs font-semibold uppercase tracking-wider text-slate-400">
              Platform Role
            </span>
            <span className="text-xs font-bold text-indigo-700 bg-indigo-50 px-2.5 py-1 rounded-md border border-indigo-100/80 mt-1">
              {roleName}
            </span>
          </div>
        </div>

        <div className="p-6 sm:p-7 grid gap-5 sm:grid-cols-2">
          <div className="p-4 rounded-xl border border-slate-100 bg-slate-50/60">
            <div className="flex items-center gap-2 text-2xs font-semibold uppercase tracking-wider text-slate-400 mb-1">
              <User className="h-3.5 w-3.5 text-slate-500" />
              Full Name
            </div>
            <p className="text-sm font-semibold text-slate-800">{displayName}</p>
          </div>

          <div className="p-4 rounded-xl border border-slate-100 bg-slate-50/60">
            <div className="flex items-center gap-2 text-2xs font-semibold uppercase tracking-wider text-slate-400 mb-1">
              <Mail className="h-3.5 w-3.5 text-slate-500" />
              Email Address
            </div>
            <p className="text-sm font-semibold text-slate-800 break-all">{email}</p>
          </div>

          <div className="p-4 rounded-xl border border-slate-100 bg-slate-50/60">
            <div className="flex items-center gap-2 text-2xs font-semibold uppercase tracking-wider text-slate-400 mb-1">
              <KeyRound className="h-3.5 w-3.5 text-slate-500" />
              Candidate ID
            </div>
            <p className="text-xs font-mono font-medium text-slate-700 break-all">{user?.id || '—'}</p>
          </div>

          <div className="p-4 rounded-xl border border-slate-100 bg-slate-50/60">
            <div className="flex items-center gap-2 text-2xs font-semibold uppercase tracking-wider text-slate-400 mb-1">
              <GraduationCap className="h-3.5 w-3.5 text-slate-500" />
              Class / Batch
            </div>
            <p className="text-sm font-semibold text-slate-800">
              {profile?.class_name && profile?.batch
                ? `${profile.class_name} · ${profile.batch}`
                : 'Not assigned yet'}
            </p>
          </div>

          <div className="p-4 rounded-xl border border-slate-100 bg-slate-50/60">
            <div className="flex items-center gap-2 text-2xs font-semibold uppercase tracking-wider text-slate-400 mb-1">
              <Calendar className="h-3.5 w-3.5 text-slate-500" />
              Account Created
            </div>
            <p className="text-sm font-semibold text-slate-800">
              {registrationDate
                ? new Date(registrationDate).toLocaleDateString(undefined, {
                    year: 'numeric',
                    month: 'long',
                    day: 'numeric',
                  })
                : '—'}
            </p>
          </div>
        </div>

        <div className="p-4 sm:p-5 border-t border-slate-100 bg-slate-50/80 flex items-start gap-3">
          <div className="p-2 rounded-lg bg-indigo-50 border border-indigo-100 text-indigo-700 shrink-0">
            <Lock className="h-4 w-4" />
          </div>
          <div className="space-y-0.5">
            <p className="text-xs font-semibold text-slate-800 flex items-center gap-1.5">
              <span>Role enforcement</span>
              <span className="text-[10px] font-semibold text-emerald-700 bg-emerald-50 px-1.5 py-0.2 rounded border border-emerald-200">
                Database enforced
              </span>
            </p>
            <p className="text-2xs text-slate-500 leading-relaxed">
              This page is read-only. Your role is read from the database and cannot be changed from
              any browser, and authorization for every action is re-checked server-side. For
              credential changes, contact your examination administrator.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
