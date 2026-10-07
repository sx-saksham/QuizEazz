import React from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ShieldAlert } from 'lucide-react'
import { AuthProvider, useAuth } from './context/AuthContext'
import { AuthForm } from './components/auth/AuthForm'
import { RoleProtectedRoute } from './components/auth/RoleProtectedRoute'
import { StudentPortal } from './components/student/StudentPortal'
import { TeacherDashboard } from './components/teacher/TeacherDashboard'
import { AdminDashboard } from './components/admin/AdminDashboard'
import { ExamTakingScreen } from './components/student/ExamTakingScreen'

const FullScreenLoader: React.FC<{ label: string }> = ({ label }) => (
  <div className="min-h-screen bg-slate-50 flex flex-col items-center justify-center p-4">
    <div className="flex flex-col items-center gap-3">
      <div className="w-8 h-8 border-3 border-indigo-600 border-t-transparent rounded-full animate-spin" />
      <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">{label}</p>
    </div>
  </div>
)

/**
 * Shown when a valid Supabase session exists but the authoritative profile
 * could not be read. QuizEazz never silently downgrades a role to student.
 */
const ProfileUnavailable: React.FC<{ message: string }> = ({ message }) => {
  const { user, signOut, refreshProfile } = useAuth()

  return (
    <div className="min-h-screen bg-slate-50 flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-md bg-white border border-slate-200 rounded-xl p-6 shadow-sm text-center">
        <div className="w-12 h-12 rounded-full bg-amber-50 text-amber-600 flex items-center justify-center mx-auto mb-4 border border-amber-100">
          <ShieldAlert className="w-6 h-6" />
        </div>
        <h2 className="text-lg font-bold text-slate-900">Profile Verification Failed</h2>
        <p className="text-xs text-slate-600 mt-2 leading-relaxed">{message}</p>
        {user?.email && (
          <p className="text-2xs text-slate-400 mt-3 font-mono break-all">
            Signed in as {user.email}
          </p>
        )}
        <div className="mt-6 flex items-center justify-center gap-2">
          <button
            onClick={() => void refreshProfile()}
            className="px-4 py-2 rounded-xl text-xs font-semibold bg-indigo-600 text-white hover:bg-indigo-700 transition-colors"
          >
            Retry
          </button>
          <button
            onClick={() => void signOut()}
            className="px-4 py-2 rounded-xl border border-slate-300 text-xs font-semibold text-slate-700 hover:bg-slate-50 transition-colors"
          >
            Sign Out
          </button>
        </div>
      </div>
    </div>
  )
}

const homeFor = (role: string): string => {
  if (role === 'teacher') return '/teacher'
  if (role === 'admin') return '/admin'
  return '/student/exams'
}

// Redirect authenticated users to their authorized workspace
const RootRedirect: React.FC = () => {
  const { session, profile, loading, profileError } = useAuth()

  if (loading) return <FullScreenLoader label="Initializing Examination Portal..." />
  if (!session) return <Navigate to="/login" replace />
  if (profileError) return <ProfileUnavailable message={profileError} />
  if (!profile) return <FullScreenLoader label="Loading your profile..." />

  return <Navigate to={homeFor(profile.role)} replace />
}

// Keep already-authenticated users away from the auth screens
const PublicOnlyRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { session, profile, loading } = useAuth()

  if (loading) return <FullScreenLoader label="Loading..." />
  if (session && profile) return <Navigate to={homeFor(profile.role)} replace />

  return <>{children}</>
}

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/" element={<RootRedirect />} />

          <Route
            path="/login"
            element={
              <PublicOnlyRoute>
                <AuthForm initialMode="login" />
              </PublicOnlyRoute>
            }
          />
          <Route
            path="/signup"
            element={
              <PublicOnlyRoute>
                <AuthForm initialMode="signup" />
              </PublicOnlyRoute>
            }
          />

          {/* Full-screen examination workspace (no portal chrome) */}
          <Route
            path="/student/exam/:id"
            element={
              <RoleProtectedRoute allowedRoles={['student', 'admin']}>
                <ExamTakingScreen />
              </RoleProtectedRoute>
            }
          />

          <Route
            path="/student/*"
            element={
              <RoleProtectedRoute allowedRoles={['student', 'admin']}>
                <StudentPortal />
              </RoleProtectedRoute>
            }
          />

          <Route
            path="/teacher/*"
            element={
              <RoleProtectedRoute allowedRoles={['teacher', 'admin']}>
                <TeacherDashboard />
              </RoleProtectedRoute>
            }
          />

          <Route
            path="/admin/*"
            element={
              <RoleProtectedRoute allowedRoles={['admin']}>
                <AdminDashboard />
              </RoleProtectedRoute>
            }
          />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}

export default App
