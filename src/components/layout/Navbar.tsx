import React from 'react'
import { Link } from 'react-router-dom'
import { LogOut } from 'lucide-react'
import { useAuth } from '../../context/AuthContext'

export const Navbar: React.FC = () => {
  const { profile, user, signOut } = useAuth()

  const displayName = profile?.full_name || user?.email?.split('@')[0] || 'Candidate'
  const displayRole =
    profile?.role === 'admin'
      ? 'Administrator'
      : profile?.role === 'teacher'
      ? 'Faculty'
      : 'Student Candidate'
  const initials = displayName
    .split(' ')
    .filter(Boolean)
    .map((n) => n[0])
    .join('')
    .substring(0, 2)
    .toUpperCase() || 'S'

  const homeRoute =
    profile?.role === 'teacher' || profile?.role === 'admin' ? '/teacher' : '/student/exams'

  return (
    <header className="h-14 w-full border-b border-slate-200 bg-white px-6 flex items-center justify-between shrink-0">
      {/* Brand Title */}
      <Link
        to={homeRoute}
        className="flex items-center gap-2 hover:opacity-85 transition-opacity"
        title={homeRoute === '/teacher' ? 'Return to Dashboard' : 'Return to My Exams'}
      >
        <span className="text-base font-bold text-slate-900 tracking-tight">
          Quiz<span className="text-indigo-600">Eazz</span>
        </span>
        <span className="text-slate-300 text-sm font-light">|</span>
        <span className="text-xs font-medium text-slate-500">
          Examination Portal
        </span>
      </Link>

      {/* Right Controls - Minimal Profile & Sign Out */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-100 border border-slate-200 text-slate-700 font-semibold text-xs">
            {initials}
          </div>
          <div className="text-left hidden sm:block">
            <p className="text-xs font-semibold text-slate-800 leading-tight">
              {displayName}
            </p>
            <p className="text-2xs text-slate-400 capitalize">
              {displayRole}
            </p>
          </div>
        </div>

        <button
          onClick={() => signOut()}
          title="Sign Out"
          className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium text-slate-500 hover:text-rose-600 hover:bg-rose-50 transition-colors"
        >
          <LogOut className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">Sign Out</span>
        </button>
      </div>
    </header>
  )
}
