import React, { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { User, Session } from '@supabase/supabase-js'
import type { Profile, UserRole } from '../types'
import { fetchMyProfile } from '../lib/studentApi'
import { supabase, toErrorMessage } from '../lib/supabase'

interface AuthContextType {
  user: User | null
  session: Session | null
  profile: Profile | null
  loading: boolean
  /** Set when a session exists but the authoritative profile cannot be read. */
  profileError: string | null
  role: UserRole | null
  signIn: (email: string, password: string) => Promise<{ error: Error | null; role?: UserRole | null }>
  signUp: (fullName: string, email: string, password: string) => Promise<{ error: Error | null; message?: string }>
  signOut: () => Promise<void>
  refreshProfile: () => Promise<void>
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

const VALID_ROLES: UserRole[] = ['student', 'teacher', 'admin']

function coerceRole(value: unknown): UserRole {
  return VALID_ROLES.includes(value as UserRole) ? (value as UserRole) : 'student'
}

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [session, setSession] = useState<Session | null>(null)
  const [user, setUser] = useState<User | null>(null)
  const [profile, setProfile] = useState<Profile | null>(null)
  const [profileError, setProfileError] = useState<string | null>(null)
  const [loading, setLoading] = useState<boolean>(true)

  /**
   * The role is read from the authoritative database profile via
   * get_my_profile(). The browser never provisions, edits, or defaults a
   * profile, and never supplies a role of its own.
   */
  const loadProfile = useCallback(async (): Promise<Profile | null> => {
    try {
      const row = await fetchMyProfile()
      const nextProfile: Profile = {
        id: row.id,
        full_name: row.full_name || 'Candidate',
        role: coerceRole(row.role),
        class_name: row.class_name ?? null,
        batch: row.batch ?? null,
        created_at: row.created_at ?? undefined,
      }
      setProfile(nextProfile)
      setProfileError(null)
      return nextProfile
    } catch (err) {
      // Never fall back to 'student' on failure. A failed lookup must be
      // visible, otherwise a faculty account can silently appear as a student.
      const message = toErrorMessage(err, 'Unable to verify your platform profile.')
      setProfile(null)
      setProfileError(message)
      console.error('[QuizEazz] profile lookup failed:', message)
      return null
    }
  }, [])

  useEffect(() => {
    let isMounted = true

    const initialize = async () => {
      try {
        const {
          data: { session: initialSession },
        } = await supabase.auth.getSession()

        if (!isMounted) return

        setSession(initialSession)
        setUser(initialSession?.user ?? null)

        if (initialSession?.user) {
          await loadProfile()
        } else {
          setProfile(null)
          setProfileError(null)
        }
      } catch (err) {
        console.error('[QuizEazz] auth initialization error:', err)
      } finally {
        if (isMounted) setLoading(false)
      }
    }

    void initialize()

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange(async (_event, newSession) => {
      if (!isMounted) return

      setSession(newSession)
      setUser(newSession?.user ?? null)

      if (newSession?.user) {
        await loadProfile()
      } else {
        setProfile(null)
        setProfileError(null)
      }

      setLoading(false)
    })

    return () => {
      isMounted = false
      subscription.unsubscribe()
    }
  }, [loadProfile])

  const refreshProfile = useCallback(async () => {
    await loadProfile()
  }, [loadProfile])

  const signIn = async (
    email: string,
    password: string,
  ): Promise<{ error: Error | null; role?: UserRole | null }> => {
    try {
      const { data, error } = await supabase.auth.signInWithPassword({
        email: email.trim(),
        password,
      })

      if (error) return { error }
      if (!data?.user) return { error: new Error('Sign-in did not return an account.') }

      setUser(data.user)
      setSession(data.session)

      const loaded = await loadProfile()
      if (!loaded) {
        return {
          error: new Error(
            profileError ?? 'Your account profile could not be verified. Contact the examination administrator.',
          ),
        }
      }
      return { error: null, role: loaded.role }
    } catch (err) {
      return { error: err instanceof Error ? err : new Error(toErrorMessage(err, 'Sign-in failed.')) }
    }
  }

  const signUp = async (fullName: string, email: string, password: string) => {
    try {
      // Public signup can only ever provision a student. The database
      // trigger owns profile creation, so no client-side write happens here.
      const { data, error } = await supabase.auth.signUp({
        email: email.trim(),
        password,
        options: { data: { full_name: fullName.trim() } },
      })

      if (error) return { error }

      const message = data?.session
        ? 'Registration successful. Your candidate account is ready.'
        : 'Registration successful. Verify your email address, then sign in.'

      return { error: null, message }
    } catch (err) {
      return { error: err instanceof Error ? err : new Error(toErrorMessage(err, 'Registration failed.')) }
    }
  }

  const signOut = useCallback(async () => {
    try {
      const { error } = await supabase.auth.signOut()
      if (error) console.error('[QuizEazz] sign out error:', error.message)
    } finally {
      setSession(null)
      setUser(null)
      setProfile(null)
      setProfileError(null)
    }
  }, [])

  return (
    <AuthContext.Provider
      value={{
        user,
        session,
        profile,
        loading,
        profileError,
        role: profile?.role ?? null,
        signIn,
        signUp,
        signOut,
        refreshProfile,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

// The provider and its consumer hook are intentionally co-located: splitting
// them would only add a module that re-exports the same context reference.
// eslint-disable-next-line react-refresh/only-export-components
export const useAuth = () => {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within an AuthProvider')
  return context
}
