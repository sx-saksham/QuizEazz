import React, { useState } from 'react'
import { GraduationCap, AlertCircle } from 'lucide-react'
import { setMyClassBatch } from '../../lib/studentApi'
import { CLASS_OPTIONS, BATCH_OPTIONS } from '../../lib/cohort'
import { toErrorMessage } from '../../lib/supabase'
import { useAuth } from '../../context/AuthContext'

/**
 * One-shot cohort onboarding for students whose authoritative profile has no
 * class/batch yet. Rendered INSTEAD of the portal until onboarding succeeds,
 * so an unassigned student can reach no exam, result, or review screen.
 *
 * The browser only offers the fixed vocabulary; the database RPC owns the
 * write (self-row only, once-only, validated server-side).
 */
export const StudentOnboarding: React.FC = () => {
  const { refreshProfile } = useAuth()
  const [className, setClassName] = useState<string>('')
  const [batch, setBatch] = useState<string>('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const canSave = !!className && !!batch && !saving

  const handleSave = async () => {
    if (!canSave) return
    setSaving(true)
    setError(null)
    try {
      await setMyClassBatch(className, batch)
      // Reload the authoritative profile; the gate re-evaluates from it.
      await refreshProfile()
    } catch (err) {
      setError(toErrorMessage(err, 'Unable to save your class and batch.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="min-h-screen bg-slate-50 flex flex-col items-center justify-center p-4">
      <div className="w-full max-w-md bg-white border border-slate-200 rounded-2xl p-6 sm:p-8 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="h-11 w-11 rounded-xl bg-indigo-600 text-white flex items-center justify-center shrink-0">
            <GraduationCap className="h-6 w-6" />
          </div>
          <div>
            <h1 className="text-base font-bold text-slate-900 tracking-tight">
              Select your Class and Batch
            </h1>
            <p className="text-xs text-slate-500 mt-0.5">
              This determines which examinations are assigned to you.
            </p>
          </div>
        </div>

        <div className="mt-6 space-y-4">
          <div>
            <label
              htmlFor="onboarding-class"
              className="block text-xs font-bold uppercase tracking-wider text-slate-500 mb-1.5"
            >
              Class
            </label>
            <select
              id="onboarding-class"
              value={className}
              onChange={(e) => setClassName(e.target.value)}
              disabled={saving}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-sm font-semibold text-slate-800 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50"
            >
              <option value="">Choose your class…</option>
              {CLASS_OPTIONS.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label
              htmlFor="onboarding-batch"
              className="block text-xs font-bold uppercase tracking-wider text-slate-500 mb-1.5"
            >
              Batch
            </label>
            <select
              id="onboarding-batch"
              value={batch}
              onChange={(e) => setBatch(e.target.value)}
              disabled={saving}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-sm font-semibold text-slate-800 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50"
            >
              <option value="">Choose your batch…</option>
              {BATCH_OPTIONS.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </div>

          {error && (
            <div className="flex items-start gap-2 rounded-lg bg-rose-50 border border-rose-200 p-3 text-xs text-rose-700">
              <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          <button
            type="button"
            onClick={() => void handleSave()}
            disabled={!canSave}
            className="w-full py-2.5 px-4 rounded-xl bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saving ? 'Saving…' : 'Confirm Class & Batch'}
          </button>

          <p className="text-2xs text-slate-500 leading-relaxed text-center">
            This can be set only once. If it is wrong, contact your examination
            administrator — it cannot be changed from this device afterwards.
          </p>
        </div>
      </div>
    </div>
  )
}
