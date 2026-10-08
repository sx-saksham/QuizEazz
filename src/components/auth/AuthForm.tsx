import React, { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../../context/AuthContext'
import { Button } from '../common/Button'
import { Input } from '../common/Input'
import { NotificationBanner } from '../common/NotificationBanner'
import {
  GraduationCap,
  Lock,
  Mail,
  User,
  ShieldCheck,
  CheckCircle2,
  AlertCircle,
} from 'lucide-react'

interface AuthFormProps {
  initialMode?: 'login' | 'signup'
}

export const AuthForm: React.FC<AuthFormProps> = ({ initialMode = 'login' }) => {
  const [isLogin, setIsLogin] = useState<boolean>(initialMode === 'login')
  const [fullName, setFullName] = useState<string>('')
  const [email, setEmail] = useState<string>('')
  const [password, setPassword] = useState<string>('')
  const [loading, setLoading] = useState<boolean>(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [successMsg, setSuccessMsg] = useState<string | null>(null)

  const { signIn, signUp } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const handleModeSwitch = (mode: 'login' | 'signup') => {
    setIsLogin(mode === 'login')
    setErrorMsg(null)
    setSuccessMsg(null)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setErrorMsg(null)
    setSuccessMsg(null)

    if (!isLogin && !fullName.trim()) {
      setErrorMsg('Candidate full name is required for exam registration.')
      setLoading(false)
      return
    }

    if (!email.trim() || !password) {
      setErrorMsg('Please enter your institutional email and examination password.')
      setLoading(false)
      return
    }

    if (password.length < 6) {
      setErrorMsg('Password must be at least 6 characters long.')
      setLoading(false)
      return
    }

    if (isLogin) {
      const { error, role: loginRole } = await signIn(email, password)
      if (error) {
        setErrorMsg(error.message || 'Authentication failed. Please verify your credentials.')
        setLoading(false)
      } else {
        const fromPath = (location.state as { from?: { pathname?: string } } | null)?.from
          ?.pathname
        let targetPath = fromPath
        if (!targetPath) {
          if (loginRole === 'teacher') {
            targetPath = '/teacher'
          } else if (loginRole === 'admin') {
            targetPath = '/admin'
          } else {
            targetPath = '/student/exams'
          }
        }
        navigate(targetPath, { replace: true })
      }
    } else {
      // Sign up strictly as student
      const { error, message } = await signUp(fullName, email, password)
      if (error) {
        setErrorMsg(error.message || 'Registration failed. Please try again.')
      } else {
        setSuccessMsg(
          message || 'Registration completed. You may now sign in to access scheduled examinations.',
        )
        setIsLogin(true)
      }
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-slate-50/80 flex flex-col justify-center py-6 sm:py-12 sm:px-6 lg:px-8">
      {/* QuizEazz Header */}
      <div className="sm:mx-auto sm:w-full sm:max-w-md text-center">
        <div className="inline-flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-tr from-indigo-600 via-indigo-700 to-purple-600 text-white shadow-sm ring-4 ring-indigo-50/80">
          <GraduationCap className="h-7 w-7" />
        </div>
        <h2 className="mt-3 text-2xl font-black tracking-tight text-slate-900 sm:text-3xl">
          Quiz<span className="text-indigo-600">Eazz</span>
        </h2>
        <p className="mt-1 text-xs text-slate-500 font-semibold tracking-wide">
          Secure College Examination Platform
        </p>
      </div>

      {/* Main Login / Register Card */}
      <div className="mt-6 sm:mx-auto sm:w-full sm:max-w-md px-4 sm:px-0">
        <div className="bg-white py-7 px-6 sm:px-8 shadow-sm rounded-2xl border border-slate-200/90">
          {/* Tabs for Login / Register */}
          <div className="flex border-b border-slate-200 mb-6">
            <button
              type="button"
              onClick={() => handleModeSwitch('login')}
              className={`flex-1 pb-3 text-xs font-bold uppercase tracking-wider text-center transition-colors border-b-2 ${
                isLogin
                  ? 'border-indigo-600 text-indigo-600'
                  : 'border-transparent text-slate-400 hover:text-slate-600'
              }`}
            >
              Candidate Login
            </button>
            <button
              type="button"
              onClick={() => handleModeSwitch('signup')}
              className={`flex-1 pb-3 text-xs font-bold uppercase tracking-wider text-center transition-colors border-b-2 ${
                !isLogin
                  ? 'border-indigo-600 text-indigo-600'
                  : 'border-transparent text-slate-400 hover:text-slate-600'
              }`}
            >
              New Student Signup
            </button>
          </div>

          {/* Feedback messages */}
          {errorMsg && (
            <div className="mb-5">
              <NotificationBanner
                type="error"
                message={errorMsg}
                onClose={() => setErrorMsg(null)}
              />
            </div>
          )}

          {successMsg && (
            <div className="mb-5">
              <NotificationBanner
                type="success"
                message={successMsg}
                onClose={() => setSuccessMsg(null)}
              />
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            {!isLogin && (
              <div>
                <Input
                  label="Candidate Full Name"
                  type="text"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  leftIcon={<User className="h-4 w-4" />}
                  required
                />
                <p className="mt-1 text-2xs text-slate-400">
                  Enter your official name as registered on college rolls.
                </p>
              </div>
            )}

            <div>
              <Input
                label="Institutional Email / ID"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="student@college.edu"
                leftIcon={<Mail className="h-4 w-4" />}
                required
              />
            </div>

            <div>
              <Input
                label="Examination Password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                leftIcon={<Lock className="h-4 w-4" />}
                required
              />
              {!isLogin && (
                <p className="mt-1 text-2xs text-slate-400">
                  Minimum 6 characters. Do not share credentials.
                </p>
              )}
            </div>

            {!isLogin && (
              <div className="rounded-lg bg-slate-50 border border-slate-200 p-3 text-2xs text-slate-600 flex items-start gap-2">
                <CheckCircle2 className="h-4 w-4 text-blue-600 shrink-0 mt-0.5" />
                <span>
                  New registrations are provisioned by the database under the{' '}
                  <strong>Student</strong> role only. Faculty and administrator accounts are
                  provisioned by the examination administrator.
                </span>
              </div>
            )}

            <div className="pt-2">
              <Button
                type="submit"
                variant="primary"
                size="md"
                className="w-full font-semibold"
                isLoading={loading}
              >
                {loading
                  ? 'Verifying...'
                  : isLogin
                  ? 'Access Examination Portal'
                  : 'Register Candidate Account'}
              </Button>
            </div>
          </form>

          {/* Academic notice */}
          <div className="mt-6 border-t border-slate-100 pt-4">
            <div className="flex items-start gap-2 text-2xs text-slate-500">
              <ShieldCheck className="h-3.5 w-3.5 text-slate-400 shrink-0 mt-0.5" />
              <span>
                Authorized laboratory access only. Role assignment and all examination
                authorization are enforced by the database, not by this browser.
              </span>
            </div>
          </div>
        </div>

        {/* Support footer */}
        <div className="mt-6 text-center text-xs text-slate-500 flex items-center justify-center gap-1.5">
          <AlertCircle className="h-3.5 w-3.5 text-slate-400" />
          <span>Account or access problems? Contact the examination administrator.</span>
        </div>
      </div>
    </div>
  )
}
