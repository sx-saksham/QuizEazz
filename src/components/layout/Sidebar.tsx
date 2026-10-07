import React from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useAuth } from '../../context/AuthContext'
import {
  LayoutDashboard,
  FileSpreadsheet,
  Award,
  ShieldCheck,
  User,
  Database,
  Users,
  ShieldAlert,
} from 'lucide-react'

export const Sidebar: React.FC = () => {
  const location = useLocation()
  const { profile } = useAuth()

  // Faculty tools are shown to faculty and admins only. Students never see
  // them. This is presentation only; the database remains the authority.
  const activeRole = profile?.role ?? 'student'

  const studentNavigation = [
    { name: 'My Exams', href: '/student/exams', icon: FileSpreadsheet },
    { name: 'Results', href: '/student/results', icon: Award },
    { name: 'Guidelines', href: '/student/guidelines', icon: ShieldCheck },
    { name: 'Profile', href: '/student/profile', icon: User },
  ]

  const teacherNavigation = [
    { name: 'Dashboard', href: '/teacher', icon: LayoutDashboard },
    { name: 'Question Bank', href: '/teacher/questions', icon: Database },
    { name: 'Examinations', href: '/teacher/exams', icon: FileSpreadsheet },
    { name: 'Students', href: '/teacher/students', icon: Users },
    { name: 'Results', href: '/teacher/results', icon: Award },
    { name: 'Security Logs', href: '/teacher/analytics', icon: ShieldAlert },
  ]

  const navigation =
    activeRole === 'teacher' || activeRole === 'admin' ? teacherNavigation : studentNavigation

const brandHome = activeRole === 'student' ? '/student/exams' : '/teacher'

  const isActive = (path: string) => {
    if (path === '/teacher' || path === '/student') {
      return location.pathname === path || location.pathname === `${path}/` || location.pathname === `${path}/dashboard`
    }
    return location.pathname.startsWith(path)
  }

  return (
    <aside className="w-56 shrink-0 border-r border-slate-200 bg-white flex flex-col justify-between select-none">
      <div>
        {/* Header Branding */}
        <div className="h-14 px-5 flex items-center border-b border-slate-200">
          <Link
            to={brandHome}
            className="flex items-center gap-2 hover:opacity-85 transition-opacity"
            title={activeRole === 'student' ? 'Return to My Exams' : 'Return to Dashboard'}
          >
            <span className="text-lg font-bold tracking-tight text-slate-900">
              Quiz<span className="text-indigo-600">Eazz</span>
            </span>
          </Link>
        </div>

        {/* Navigation Items */}
        <nav className="p-3 space-y-1">
          {navigation.map((item) => {
            const Icon = item.icon
            const active = isActive(item.href)
            return (
              <Link
                key={item.name}
                to={item.href}
                className={`flex items-center gap-3 px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                  active
                    ? 'bg-indigo-50 text-indigo-700 font-semibold'
                    : 'text-slate-600 hover:text-slate-900 hover:bg-slate-50'
                }`}
              >
                <Icon
                  className={`h-4 w-4 shrink-0 transition-colors ${
                    active ? 'text-indigo-600' : 'text-slate-400'
                  }`}
                />
                <span>{item.name}</span>
              </Link>
            )
          })}
        </nav>
      </div>

      {/* Clean bottom subtle system info */}
      <div className="p-4 border-t border-slate-100 text-[11px] text-slate-400">
        QuizEazz v2.4
      </div>
    </aside>
  )
}
