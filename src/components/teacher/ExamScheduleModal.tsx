import React, { useCallback, useState } from 'react'
import { Modal } from '../common/Modal'
import { Button } from '../common/Button'
import { DateTimeField } from '../common/DateTimeField'
import { NotificationBanner } from '../common/NotificationBanner'
import { AlertCircle, Save } from 'lucide-react'
import type { Exam } from '../../types'
import { setExamSchedule } from '../../lib/teacherApi'
import { toErrorMessage } from '../../lib/supabase'

interface ExamScheduleModalProps {
  isOpen: boolean
  exam: Exam | null
  onClose: () => void
  onSaved: () => void
}

const toLocalInput = (iso: string | null): string => {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`
}

/**
 * Edit the schedule of an existing examination.
 *
 * Locked once a candidate has submitted or has an attempt in progress, because
 * the deadline the database enforces is derived from these values. The RPC
 * enforces that too; this only avoids a round trip that would fail.
 */
export const ExamScheduleModal: React.FC<ExamScheduleModalProps> = ({
  isOpen,
  exam,
  onClose,
  onSaved,
}) => {
  // Seeded from the exam prop at mount. The caller renders this modal with a
  // `key` of the exam id, so opening a different exam remounts it with fresh
  // initial state and no effect is needed to sync fields.
  const [durationMinutes, setDurationMinutes] = useState(exam?.duration_minutes || 60)
  const [startsAt, setStartsAt] = useState(() => toLocalInput(exam?.starts_at ?? null))
  const [endsAt, setEndsAt] = useState(() => toLocalInput(exam?.ends_at ?? null))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  const locked = !!exam && exam.status === 'closed'
  const hasActivity = !!exam && (exam.submitted_count ?? 0) > 0

  const submit = useCallback(async () => {
    if (!exam) return
    setError(null)
    setSuccess(null)

    const duration = Math.round(Number(durationMinutes))
    if (!Number.isFinite(duration) || duration < 1 || duration > 1440) {
      setError('Duration must be between 1 and 1440 minutes.')
      return
    }
    if (!startsAt) {
      setError('A scheduled start time is required.')
      return
    }
    const starts = new Date(startsAt)
    if (Number.isNaN(starts.getTime())) {
      setError('Please provide a valid start date and time.')
      return
    }
    if (endsAt) {
      const ends = new Date(endsAt)
      if (Number.isNaN(ends.getTime())) {
        setError('Please provide a valid close date and time.')
        return
      }
      if (ends.getTime() <= starts.getTime()) {
        setError('The close time must be later than the start time.')
        return
      }
    }

    setSaving(true)
    try {
      await setExamSchedule(exam.id, {
        durationMinutes: duration,
        startsAt: starts.toISOString(),
        endsAt: endsAt ? new Date(endsAt).toISOString() : null,
      })
      setSuccess('Schedule updated. The new deadline applies to attempts started from now on.')
      onSaved()
    } catch (err) {
      setError(toErrorMessage(err, 'Unable to update the examination schedule.'))
    } finally {
      setSaving(false)
    }
  }, [exam, durationMinutes, startsAt, endsAt, onSaved])

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Edit Schedule"
      subtitle={exam?.title}
      maxWidth="lg"
      footer={
        <div className="flex items-center justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onClose} disabled={saving}>
            Close
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => void submit()}
            disabled={saving || locked || hasActivity}
            isLoading={saving}
            icon={<Save className="h-3.5 w-3.5" />}
          >
            {saving ? 'Saving...' : 'Save Schedule'}
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        {error && (
          <NotificationBanner type="error" message={error} onClose={() => setError(null)} />
        )}
        {success && (
          <NotificationBanner
            type="success"
            message={success}
            onClose={() => setSuccess(null)}
          />
        )}

        {locked && (
          <div className="flex items-start gap-2 rounded-lg bg-slate-50 border border-slate-200 p-3 text-xs text-slate-600">
            <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
            <span>This examination is closed. Its schedule cannot be changed.</span>
          </div>
        )}

        {!locked && hasActivity && (
          <div className="flex items-start gap-2 rounded-lg bg-amber-50 border border-amber-200 p-3 text-xs text-amber-900">
            <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
            <span>
              {exam?.submitted_count} candidate submission(s) already exist for this examination.
              The schedule is locked because changing it would alter an enforced deadline.
              Create a new examination instead.
            </span>
          </div>
        )}

        <div>
          <label className="block text-xs font-semibold text-slate-700 mb-1">
            Duration (Minutes) <span className="text-rose-500">*</span>
          </label>
          <input
            type="number"
            min={1}
            max={1440}
            required
            disabled={locked || hasActivity}
            value={durationMinutes}
            onChange={(e) => setDurationMinutes(Number(e.target.value))}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-50 disabled:text-slate-500"
          />
        </div>

        <DateTimeField
          label="Scheduled Start Time"
          required
          value={startsAt}
          onChange={setStartsAt}
          minDateTime={undefined}
          hint="Candidates cannot begin before this time. The database refuses an early attempt."
        />

        <DateTimeField
          label="Close Time (optional)"
          value={endsAt}
          onChange={setEndsAt}
          hint="If set, candidates cannot start the examination after this time."
        />
      </div>
    </Modal>
  )
}

export default ExamScheduleModal
