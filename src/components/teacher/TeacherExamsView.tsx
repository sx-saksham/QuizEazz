import React, { useCallback, useState } from 'react'
import type { Exam } from '../../types'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../common/Card'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { EmptyState } from '../common/EmptyState'
import { Modal } from '../common/Modal'
import { Input } from '../common/Input'
import { DateTimeField } from '../common/DateTimeField'
import { NotificationBanner } from '../common/NotificationBanner'
import { AssignStudentsModal } from './AssignStudentsModal'
import { ManagePaperModal } from './ManagePaperModal'
import { ExamScheduleModal } from './ExamScheduleModal'
import {
  FileSpreadsheet,
  Plus,
  Clock,
  Calendar,
  Users,
  RefreshCw,
  AlertCircle,
  ListChecks,
  Lock,
  Unlock,
  CalendarCog,
} from 'lucide-react'
import { createExam, fetchExams, setExamStatus, updateExam } from '../../lib/teacherApi'
import { useAsyncData } from '../../hooks/useAsyncData'
import { toErrorMessage } from '../../lib/supabase'

type Banner = { type: 'success' | 'error' | 'info'; message: string } | null

export const TeacherExamsView: React.FC = () => {
  const [banner, setBanner] = useState<Banner>(null)

  const [showCreate, setShowCreate] = useState(false)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [durationMinutes, setDurationMinutes] = useState(60)
  const [status, setStatus] = useState<'published' | 'draft'>('published')
  const [startsAt, setStartsAt] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const [assignFor, setAssignFor] = useState<Exam | null>(null)
  const [paperFor, setPaperFor] = useState<Exam | null>(null)
  const [scheduleFor, setScheduleFor] = useState<Exam | null>(null)
  const [statusBusyId, setStatusBusyId] = useState<string | null>(null)

  // Edit-details state. The modal remounts per exam (key) so fields always
  // start from the stored row. Blocked server-side once finalized attempts
  // exist; closed exams cannot be edited at all.
  const [editingExam, setEditingExam] = useState<Exam | null>(null)
  const [editTitle, setEditTitle] = useState('')
  const [editDescription, setEditDescription] = useState('')
  const [editDuration, setEditDuration] = useState(60)
  const [editSaving, setEditSaving] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)

  const openEditDetails = (exam: Exam) => {
    setEditTitle(exam.title)
    setEditDescription(exam.description ?? '')
    setEditDuration(exam.duration_minutes)
    setEditError(null)
    setEditingExam(exam)
  }

  const handleSaveDetails = async () => {
    if (!editingExam || editSaving) return
    if (!editTitle.trim()) {
      setEditError('Please provide an examination title.')
      return
    }
    const duration = Math.round(Number(editDuration))
    if (!Number.isFinite(duration) || duration < 1 || duration > 1440) {
      setEditError('Duration must be between 1 and 1440 minutes.')
      return
    }
    setEditSaving(true)
    setEditError(null)
    try {
      await updateExam({
        exam_id: editingExam.id,
        title: editTitle.trim(),
        description: editDescription.trim() || null,
        duration_minutes: duration,
      })
      setEditingExam(null)
      setBanner({ type: 'success', message: 'Examination details saved.' })
      reloadExams()
    } catch (err) {
      setEditError(toErrorMessage(err, 'Unable to save the examination changes.'))
    } finally {
      setEditSaving(false)
    }
  }

  const loadExams = useCallback(() => fetchExams(), [])
  const {
    data: exams,
    error: fetchError,
    loading,
    reload: reloadExams,
  } = useAsyncData<Exam[]>(loadExams, [], {
    errorFallback: 'Unable to retrieve examination schedules.',
  })

  const resetForm = () => {
    setTitle('')
    setDescription('')
    setDurationMinutes(60)
    setStatus('published')
    setStartsAt('')
    setFormError(null)
  }

  const handleCreateExam = async (e: React.FormEvent) => {
    e.preventDefault()
    setFormError(null)

    if (!title.trim()) return setFormError('Please provide an examination title.')

    const duration = Math.round(Number(durationMinutes))
    if (!Number.isFinite(duration) || duration < 5 || duration > 360) {
      return setFormError('Duration must be between 5 and 360 minutes.')
    }

    if (!startsAt) {
      return setFormError('A scheduled start time is required.')
    }

    const when = new Date(startsAt)
    if (Number.isNaN(when.getTime())) {
      return setFormError('Please provide a valid scheduled start date and time.')
    }
    if (when.getTime() <= Date.now()) {
      return setFormError('The scheduled start time must be in the future.')
    }

    setSubmitting(true)
    try {
      await createExam({
        title: title.trim(),
        description: description.trim() || null,
        duration_minutes: duration,
        status,
        starts_at: when.toISOString(),
      })
      // Only report success once the server has confirmed the row.
      setShowCreate(false)
      resetForm()
      setBanner({ type: 'success', message: 'Examination scheduled successfully.' })
      reloadExams()
    } catch (err) {
      setFormError(toErrorMessage(err, 'Failed to schedule the examination.'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleToggleClosed = async (exam: Exam) => {
    const next = exam.status === 'closed' ? 'published' : 'closed'
    setStatusBusyId(exam.id)
    try {
      await setExamStatus(exam.id, next)
      setBanner({
        type: 'success',
        message:
          next === 'closed'
            ? `"${exam.title}" is now closed. Candidates can no longer start it.`
            : `"${exam.title}" is now open again.`,
      })
      reloadExams()
    } catch (err) {
      setBanner({ type: 'error', message: toErrorMessage(err, 'Unable to update status.') })
    } finally {
      setStatusBusyId(null)
    }
  }

  /** "Scheduled" is a UI concept: status='published' AND starts_at in the future. */
  const statusView = (exam: Exam) => {
    if (exam.status === 'draft') return { label: 'Draft', variant: 'warning' as const }
    if (exam.status === 'closed') return { label: 'Closed', variant: 'neutral' as const }
    if (exam.starts_at && new Date(exam.starts_at) > new Date()) {
      return { label: 'Scheduled', variant: 'info' as const }
    }
    return { label: 'Open', variant: 'success' as const }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900 sm:text-2xl">
            Examinations
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            Schedule papers, fix the exact question set, and assign candidates
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            icon={<RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />}
            onClick={reloadExams}
            disabled={loading}
          >
            Refresh
          </Button>
          <Button
            variant="primary"
            size="sm"
            icon={<Plus className="h-3.5 w-3.5" />}
            onClick={() => {
              resetForm()
              setShowCreate(true)
            }}
          >
            Create Examination
          </Button>
        </div>
      </div>

      {banner && (
        <NotificationBanner
          type={banner.type}
          message={banner.message}
          onClose={() => setBanner(null)}
        />
      )}

      {loading ? (
        <div className="rounded-xl border border-slate-200 bg-white p-12 text-center">
          <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-indigo-600 border-t-transparent" />
          <p className="mt-3 text-xs font-medium text-slate-500">Loading examinations...</p>
        </div>
      ) : fetchError ? (
        <div className="rounded-xl border border-red-200 bg-red-50/50 p-8 text-center">
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-red-100 text-red-600 mb-3">
            <AlertCircle className="h-5 w-5" />
          </div>
          <h3 className="text-sm font-semibold text-red-900">Failed to Load Examinations</h3>
          <p className="mt-1 text-xs text-red-600 max-w-md mx-auto">{fetchError}</p>
          <div className="mt-4">
            <Button variant="secondary" size="sm" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={reloadExams}>
              Retry
            </Button>
          </div>
        </div>
      ) : exams.length === 0 ? (
        <EmptyState
          icon={<FileSpreadsheet className="h-6 w-6 text-slate-400" />}
          title="No Examinations Created"
          description="Create your first examination, fix its question paper, then assign candidates."
          action={
            <Button
              variant="primary"
              size="sm"
              icon={<Plus className="h-3.5 w-3.5" />}
              onClick={() => {
                resetForm()
                setShowCreate(true)
              }}
            >
              Create Examination
            </Button>
          }
        />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {exams.map((exam) => {
            const view = statusView(exam)
            return (
              <Card key={exam.id} hoverEffect className="flex flex-col justify-between">
                <CardHeader>
                  <div className="flex items-start justify-between gap-2">
                    <CardTitle className="text-sm font-semibold line-clamp-1">{exam.title}</CardTitle>
                    <Badge variant={view.variant} size="sm">
                      {view.label}
                    </Badge>
                  </div>
                  <CardDescription className="line-clamp-2 mt-1">
                    {exam.description || 'No instructions provided.'}
                  </CardDescription>
                </CardHeader>

                <CardContent className="space-y-2 text-xs text-slate-600">
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1 text-slate-400">
                      <Clock className="h-3.5 w-3.5" /> Duration
                    </span>
                    <span className="font-semibold text-slate-800">{exam.duration_minutes} min</span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1 text-slate-400">
                      <ListChecks className="h-3.5 w-3.5" /> Paper
                    </span>
                    <span className="font-semibold text-slate-800">
                      {exam.total_questions ?? 0} questions • {exam.total_marks ?? 0} marks
                    </span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1 text-slate-400">
                      <Users className="h-3.5 w-3.5" /> Assigned
                    </span>
                    <span className="font-semibold text-slate-800">
                      {exam.assigned_count ?? 0} candidate(s)
                    </span>
                  </div>

                  {exam.starts_at && (
                    <div className="flex items-center justify-between">
                      <span className="flex items-center gap-1 text-slate-400">
                        <Calendar className="h-3.5 w-3.5" /> Starts
                      </span>
                      <span className="font-medium text-slate-700 text-2xs">
                        {new Date(exam.starts_at).toLocaleString()}
                      </span>
                    </div>
                  )}

                  {(exam.submitted_count ?? 0) > 0 && (
                    <p className="pt-1 text-2xs text-emerald-700 bg-emerald-50 border border-emerald-100 rounded-md px-2 py-1">
                      {exam.submitted_count} submission(s) received — paper is locked.
                    </p>
                  )}
                </CardContent>

                <div className="border-t border-slate-100 bg-slate-50/50 px-4 py-3 rounded-b-xl flex items-center justify-between gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    icon={<Users className="h-3.5 w-3.5" />}
                    onClick={() => setAssignFor(exam)}
                  >
                    Assign Students
                  </Button>

                  <div className="flex items-center gap-1.5">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPaperFor(exam)}
                      icon={<ListChecks className="h-3.5 w-3.5" />}
                    >
                      Manage Paper
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => openEditDetails(exam)}
                    >
                      Edit
                    </Button>
                    <button
                      title="Edit schedule"
                      onClick={() => setScheduleFor(exam)}
                      className="p-1.5 rounded-lg text-slate-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"
                    >
                      <CalendarCog className="h-4 w-4" />
                    </button>
                    <button
                      title={exam.status === 'closed' ? 'Reopen examination' : 'Close examination'}
                      disabled={statusBusyId === exam.id}
                      onClick={() => void handleToggleClosed(exam)}
                      className="p-1.5 rounded-lg text-slate-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors disabled:opacity-40"
                    >
                      {exam.status === 'closed' ? (
                        <Unlock className="h-4 w-4" />
                      ) : (
                        <Lock className="h-4 w-4" />
                      )}
                    </button>
                  </div>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      {/* CREATE EXAMINATION */}
      <Modal
        isOpen={showCreate}
        onClose={() => setShowCreate(false)}
        title="Create Examination"
        subtitle="Configure title, duration and schedule"
        maxWidth="lg"
      >
        <form onSubmit={handleCreateExam} className="space-y-4">
          {formError && (
            <div className="flex items-start gap-2 rounded-lg bg-rose-50 border border-rose-200 p-3 text-xs text-rose-700">
              <AlertCircle className="h-4 w-4 shrink-0 mt-0.5 text-rose-500" />
              <span>{formError}</span>
            </div>
          )}

          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">
              Examination Title <span className="text-rose-500">*</span>
            </label>
            <Input
              type="text"
              required
              placeholder="e.g. Data Structures Midterm Examination"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="text-xs"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">
              Description &amp; Instructions
            </label>
            <textarea
              rows={3}
              placeholder="Candidate instructions, allowed libraries, departmental notes..."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-900 placeholder-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Duration (Minutes) <span className="text-rose-500">*</span>
              </label>
              <Input
                type="number"
                min={5}
                max={360}
                required
                value={durationMinutes}
                onChange={(e) => setDurationMinutes(Number(e.target.value))}
                className="text-xs"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">Initial Status</label>
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value as 'published' | 'draft')}
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 hover:border-slate-400"
              >
                <option value="published">Published (opens at the scheduled time)</option>
                <option value="draft">Draft (restricted to faculty)</option>
              </select>
            </div>
          </div>

          <DateTimeField
            label="Scheduled Start Time"
            required
            value={startsAt}
            onChange={setStartsAt}
            error={formError && !startsAt ? null : undefined}
            hint="Candidates can only begin this examination once the scheduled start time arrives. The database enforces this."
          />

          <div className="flex items-center justify-end gap-2 pt-4 border-t border-slate-200">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setShowCreate(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={submitting}
              isLoading={submitting}
              icon={<Plus className="h-3.5 w-3.5" />}
            >
              {submitting ? 'Scheduling...' : 'Schedule Examination'}
            </Button>
          </div>
        </form>
      </Modal>

      {/* EDIT DETAILS — title/description/duration only. Blocked server-side
          once finalized attempts exist; closed exams are frozen. */}
      {editingExam && (
        <Modal
          key={editingExam.id}
          isOpen={!!editingExam}
          onClose={() => setEditingExam(null)}
          title="Edit Examination Details"
          subtitle={editingExam.title}
          maxWidth="lg"
        >
          <div className="space-y-4">
            {editError && (
              <div className="flex items-start gap-2 rounded-lg bg-rose-50 border border-rose-200 p-3 text-xs text-rose-700">
                <AlertCircle className="h-4 w-4 shrink-0 mt-0.5 text-rose-500" />
                <span>{editError}</span>
              </div>
            )}

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Examination Title <span className="text-rose-500">*</span>
              </label>
              <Input
                type="text"
                required
                value={editTitle}
                onChange={(e) => setEditTitle(e.target.value)}
                className="text-xs"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Description &amp; Instructions
              </label>
              <textarea
                rows={3}
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-900 placeholder-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Duration (Minutes) <span className="text-rose-500">*</span>
              </label>
              <Input
                type="number"
                min={1}
                max={1440}
                required
                value={editDuration}
                onChange={(e) => setEditDuration(Number(e.target.value))}
                className="text-xs"
              />
            </div>

            <p className="text-2xs text-slate-500 leading-relaxed">
              Details cannot be changed once submissions exist for this examination. Use the
              schedule control for timing and Manage Paper for questions.
            </p>

            <div className="flex items-center justify-end gap-2 pt-4 border-t border-slate-200">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setEditingExam(null)}
                disabled={editSaving}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="primary"
                size="sm"
                onClick={() => void handleSaveDetails()}
                disabled={editSaving}
                isLoading={editSaving}
              >
                {editSaving ? 'Saving...' : 'Save Changes'}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* ASSIGN STUDENTS */}
      {assignFor && (
        <AssignStudentsModal
          isOpen
          examId={assignFor.id}
          examTitle={assignFor.title}
          onClose={() => setAssignFor(null)}
          onSaved={reloadExams}
        />
      )}

      {/* MANAGE PAPER */}
      {paperFor && (
        <ManagePaperModal
          isOpen
          examId={paperFor.id}
          examTitle={paperFor.title}
          onClose={() => setPaperFor(null)}
          onSaved={reloadExams}
        />
      )}

      {/* EDIT SCHEDULE */}
      {scheduleFor && (
        <ExamScheduleModal
          key={scheduleFor.id}
          isOpen
          exam={scheduleFor}
          onClose={() => setScheduleFor(null)}
          onSaved={reloadExams}
        />
      )}
    </div>
  )
}
