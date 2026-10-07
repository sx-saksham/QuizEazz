import React, { useEffect, useState } from 'react'
import { Modal } from '../common/Modal'
import { Button } from '../common/Button'
import { NotificationBanner } from '../common/NotificationBanner'
import { FlaskConical, Plus, Trash2, CheckCircle2, XCircle, Loader2 } from 'lucide-react'
import {
  fetchTestCases,
  saveTestCases,
  validateReferenceSolution,
  type ReferenceValidationResult,
} from '../../lib/teacherApi'
import { toErrorMessage } from '../../lib/supabase'

interface ManageTestsModalProps {
  questionId: string | null
  questionTitle: string
  onClose: () => void
}

const MAX_CASES = 20

/**
 * Faculty hidden-test management for one C++ coding question.
 *
 * Teachers author stdin values only. Expected outputs are NEVER typed here:
 * they are produced exclusively by running the stored faculty reference
 * solution through Judge0 (server-side) via "Validate". A question joins an
 * exam paper only once at least one case carries a validated expected
 * output. Students have no path to any of this data.
 */
export const ManageTestsModal: React.FC<ManageTestsModalProps> = ({
  questionId,
  questionTitle,
  onClose,
}) => {
  const [stdins, setStdins] = useState<string[]>([])
  const [validatedCount, setValidatedCount] = useState(0)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [validating, setValidating] = useState(false)
  const [validation, setValidation] = useState<ReferenceValidationResult[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  useEffect(() => {
    if (!questionId) return
    let cancelled = false
    const load = async () => {
      setLoading(true)
      setError(null)
      setValidation(null)
      try {
        const data = await fetchTestCases(questionId)
        if (cancelled) return
        setStdins((data?.cases ?? []).map((c) => c.stdin ?? ''))
        setValidatedCount((data?.cases ?? []).filter((c) => c.validated).length)
      } catch (err) {
        if (!cancelled) setError(toErrorMessage(err, 'Unable to load the hidden test cases.'))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [questionId])

  const setStdinAt = (index: number, value: string) => {
    setStdins((prev) => prev.map((s, i) => (i === index ? value : s)))
  }

  const addCase = () => {
    if (stdins.length >= MAX_CASES) return
    setStdins((prev) => [...prev, ''])
    setValidation(null)
  }

  const removeCase = (index: number) => {
    setStdins((prev) => prev.filter((_, i) => i !== index))
    setValidation(null)
  }

  const handleSave = async () => {
    if (!questionId || saving) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const result = await saveTestCases(questionId, stdins)
      setSuccess(
        `${result?.saved ?? stdins.length} hidden test case${(result?.saved ?? 0) === 1 ? '' : 's'} saved. Run Validate to compute expected outputs from the reference solution.`,
      )
    } catch (err) {
      setError(toErrorMessage(err, 'Unable to save the hidden test cases.'))
    } finally {
      setSaving(false)
    }
  }

  const handleValidate = async () => {
    if (!questionId || validating) return
    setValidating(true)
    setError(null)
    setSuccess(null)
    try {
      const report = await validateReferenceSolution(questionId)
      setValidation(report?.results ?? [])
      if (report?.ok) {
        setValidatedCount(report.validated ?? 0)
        setSuccess(
          `Reference validated ${report.validated ?? 0} of ${report.total ?? 0} cases. Expected outputs are stored server-side.`,
        )
      } else {
        setError(report?.error ?? 'Reference validation failed.')
      }
    } catch (err) {
      setError(toErrorMessage(err, 'Reference validation could not be completed.'))
    } finally {
      setValidating(false)
    }
  }

  return (
    <Modal
      isOpen={!!questionId}
      onClose={onClose}
      title="Hidden Test Cases"
      subtitle={questionTitle}
      maxWidth="xl"
      footer={
        <div className="flex items-center justify-between w-full gap-2">
          <span className="text-2xs text-slate-500">
            {validatedCount} of {stdins.length} cases validated
          </span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onClose} disabled={saving || validating}>
              Close
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void handleSave()}
              disabled={saving || validating || loading}
              isLoading={saving}
            >
              {saving ? 'Saving...' : 'Save Cases'}
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => void handleValidate()}
              disabled={validating || saving || loading || stdins.length === 0}
              isLoading={validating}
              icon={<FlaskConical className="h-3.5 w-3.5" />}
            >
              {validating ? 'Validating...' : 'Validate with Reference'}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-3">
        {error && <NotificationBanner type="error" message={error} onClose={() => setError(null)} />}
        {success && (
          <NotificationBanner type="success" message={success} onClose={() => setSuccess(null)} />
        )}

        <p className="text-2xs text-slate-500 leading-relaxed">
          Enter one input per case. Expected outputs are computed by running the stored faculty
          reference solution — they are never typed here and never visible to students. A question
          can join an exam paper only once at least one case is validated.
        </p>

        {loading ? (
          <div className="py-10 flex items-center justify-center gap-2 text-xs text-slate-500">
            <Loader2 className="h-4 w-4 animate-spin text-indigo-600" />
            Loading hidden test cases...
          </div>
        ) : (
          <div className="space-y-2.5">
            {stdins.map((stdin, i) => {
              const verdict = validation?.find((r) => r.case_order === i)
              return (
                <div key={i} className="rounded-xl border border-slate-200 p-3 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-bold text-slate-800">Case {i + 1}</span>
                    <div className="flex items-center gap-2">
                      {verdict &&
                        (verdict.ok ? (
                          <span className="inline-flex items-center gap-1 text-2xs font-semibold text-emerald-700">
                            <CheckCircle2 className="h-3.5 w-3.5" /> Validated
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-2xs font-semibold text-rose-700">
                            <XCircle className="h-3.5 w-3.5" /> Failed
                          </span>
                        ))}
                      <button
                        type="button"
                        onClick={() => removeCase(i)}
                        disabled={saving || validating}
                        aria-label={`Remove case ${i + 1}`}
                        className="text-slate-400 hover:text-rose-600 transition-colors disabled:opacity-50"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                  <textarea
                    value={stdin}
                    onChange={(e) => setStdinAt(i, e.target.value)}
                    disabled={saving || validating}
                    placeholder="Standard input for this case (leave empty for no input)…"
                    rows={2}
                    className="w-full rounded-lg border border-slate-300 bg-white p-2.5 font-mono text-xs text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50"
                  />
                  {verdict && !verdict.ok && verdict.error && (
                    <p className="text-2xs text-rose-700">{verdict.error}</p>
                  )}
                  {verdict?.ok && verdict.stdout_preview !== null && (
                    <p className="text-2xs text-slate-500 font-mono truncate">
                      Expected: {verdict.stdout_preview || '(empty output)'}
                    </p>
                  )}
                </div>
              )
            })}

            {stdins.length === 0 && (
              <p className="py-6 text-center text-xs text-slate-500">
                No hidden test cases yet. Add the first input below.
              </p>
            )}

            {stdins.length < MAX_CASES && (
              <button
                type="button"
                onClick={addCase}
                disabled={saving || validating}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-dashed border-slate-300 text-xs font-semibold text-slate-600 hover:bg-slate-50 transition-colors disabled:opacity-50"
              >
                <Plus className="h-3.5 w-3.5" /> Add test case
              </button>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}
