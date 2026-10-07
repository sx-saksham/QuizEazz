import React from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '../../context/AuthContext'
import type { UserRole } from '../../types'
import { ShieldAlert, ArrowLeft } from 'lucide-react'
import { Button } from '../common/Button'

interface RoleProtectedRouteProps {
  children: React.ReactNode
  allowedRoles: UserRole[]
}

export const RoleProtectedRoute: React.FC<RoleProtectedRouteProps> = ({
  children,
  allowedRoles,
}) => {
  const { session, profile, profileError, loading, role, signOut, refreshProfile } = useAuth()
  const location = useLocation()

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-50 flex flex-col items-center justify-center p-4">
        <div className="flex flex-col items-center gap-3">
          <div className="w-9 h-9 border-3 border-indigo-600 border-t-transparent rounded-full animate-spin"></div>
          <p className="text-sm font-medium text-slate-600">Verifying academic credentials...</p>
        </div>
      </div>
    )
  }

  // Not authenticated
  if (!session) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  // A session exists but the authoritative profile could not be read.
  // Surface it instead of guessing a role.
  if (profileError) {
    return (
      <div className="min-h-screen bg-slate-50 flex items-center justify-center p-4">
        <div className="w-full max-w-md bg-white border border-slate-200 rounded-xl p-6 shadow-sm text-center">
          <div className="w-12 h-12 rounded-full bg-amber-50 text-amber-600 flex items-center justify-center mx-auto mb-4 border border-amber-100">
            <ShieldAlert className="w-6 h-6" />
          </div>
          <h2 className="text-lg font-bold text-slate-900">Profile Verification Failed</h2>
          <p className="text-xs text-slate-600 mt-2 leading-relaxed">{profileError}</p>
          <div className="mt-6 flex items-center justify-center gap-2">
            <Button
              variant="primary"
              size="sm"
              onClick={() => void refreshProfile()}
            >
              Retry
            </Button>
            <Button variant="outline" size="sm" icon={<ArrowLeft className="w-4 h-4" />} onClick={() => void signOut()}>
              Sign Out
            </Button>
          </div>
        </div>
      </div>
    )
  }

  // If authenticated but the profile is still resolving, keep waiting.
  if (!profile) {
    return (
      <div className="min-h-screen bg-slate-50 flex flex-col items-center justify-center p-4">
        <div className="flex flex-col items-center gap-3">
          <div className="w-9 h-9 border-3 border-indigo-600 border-t-transparent rounded-full animate-spin"></div>
          <p className="text-sm font-medium text-slate-600">Loading your profile...</p>
        </div>
      </div>
    )
  }

  const currentRole = role ?? profile.role
  if (!allowedRoles.includes(currentRole)) {
    return (
      <div className="min-h-screen bg-slate-50 flex items-center justify-center p-4">
        <div className="w-full max-w-md bg-white border border-slate-200 rounded-xl p-6 shadow-sm text-center">
          <div className="w-12 h-12 rounded-full bg-rose-50 text-rose-600 flex items-center justify-center mx-auto mb-4 border border-rose-100">
            <ShieldAlert className="w-6 h-6" />
          </div>
          <h2 className="text-lg font-bold text-slate-900">Access Restricted</h2>
          <p className="text-xs text-slate-600 mt-2 leading-relaxed">
            Your authenticated account (<span className="font-semibold text-slate-800">{profile.full_name}</span>) is assigned the{' '}
            <span className="font-semibold uppercase text-blue-700">{currentRole}</span> role and does not have permissions to view this portal area.
          </p>
          <p className="text-2xs text-slate-400 mt-3">
            Access is enforced by the database. Contact the examination administrator if you believe this is incorrect.
          </p>
          <div className="mt-6 flex justify-center">
            <Button
              variant="outline"
              size="sm"
              icon={<ArrowLeft className="w-4 h-4" />}
              onClick={() => {
                window.location.href = currentRole === 'student' ? '/student/exams' : '/teacher'
              }}
            >
              Return to My Workspace
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
