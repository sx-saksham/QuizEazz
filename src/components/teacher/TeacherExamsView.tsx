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
import { createExam, fetchExams, setExamStatus, updateExam, publishExamNow, deleteExam, setExamArchived } from '../../lib/teacherApi'
import { useAsyncData } from '../../hooks/useAsyncData'
import { toErrorMessage } from '../../lib/supabase'

type Banner = { type: 'success' | 'error' | 'info'; message: string } | null

/** Module scope: impure clock read kept out of the component render path. */
function isFutureDate(when: Date): boolean {
  return when.getTime() > Date.now()
}

export const TeacherExamsView: React.FC = () => {
  const [banner, setBanner] = useState<Banner>(null)

  const [showCreate, setShowCreate] = useState(false)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [durationMinutes, setDurationMinutes] = useState(60)
  const [status, setStatus] = useState<'published' | 'draft'>('published')
  const [startsAt, setStartsAt] = useState('')
  const [endsAt, setEndsAt] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const [assignFor, setAssignFor] = useState<Exam | null>(null)
  const [paperFor, setPaperFor] = useState<Exam | null>(null)
  const [scheduleFor, setScheduleFor] = useState<Exam | null>(null)
  const [statusBusyId, setStatusBusyId] = useState<string | null>(null)
  const [publishingExam, setPublishingExam] = useState<Exam | null>(null)
  const [publishBusy, setPublishBusy] = useState(false)
  const [publishError, setPublishError] = useState<string | null>(null)
  const [selectedExamIds, setSelectedExamIds] = useState<Set<string>>(new Set())
  const [examSelectionMode, setExamSelectionMode] = useState(false)
  const [showBulkExamDelete, setShowBulkExamDelete] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [blockedExamIds, setBlockedExamIds] = useState<string[]>([])
  const [examArchiveBusy, setExamArchiveBusy] = useState(false)

  const toggleExamSelected = (id: string) => {
    setSelectedExamIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handlePublishNow = async () => {
    if (!publishingExam || publishBusy) return
    setPublishBusy(true)
    setPublishError(null)
    try {
      await publishExamNow(publishingExam.id)
      setPublishingExam(null)
      setBanner({ type: 'success', message: `"${publishingExam.title}" is now published and available to its assigned students.` })
      reloadExams()
    } catch (err) {
      setPublishError(toErrorMessage(err, 'Unable to publish the examination.'))
    } finally {
      setPublishBusy(false)
    }
  }

  const handleBulkExamDelete = async () => {
    if (selectedExamIds.size === 0 || deleteBusy) return
    setDeleteBusy(true)
    setDeleteError(null)
    setBlockedExamIds([])
    const ids = Array.from(selectedExamIds)
    const blocked: string[] = []
    const blockedIdList: string[] = []
    let deleted = 0
    const titleOf = (id: string) => exams.find((e) => e.id === id)?.title ?? id
    for (const id of ids) {
      try {
        await deleteExam(id)
        deleted += 1
      } catch (err) {
        blocked.push(`${titleOf(id)}: ${toErrorMessage(err, 'Unable to delete the examination.')}`)
        blockedIdList.push(id)
      }
    }
    setDeleteBusy(false)
    setBlockedExamIds(blockedIdList)
    if (blocked.length > 0) {
      setDeleteError(
        `Deleted ${deleted} of ${ids.length}. These have candidate attempts and are protected:\n` +
          blocked.join('\n'),
      )
    } else {
      setShowBulkExamDelete(false)
      setExamSelectionMode(false)
      setSelectedExamIds(new Set())
      setBanner({ type: 'success', message: `Deleted ${deleted} examination${deleted === 1 ? '' : 's'}.` })
    }
    reloadExams()
  }

  const handleArchiveBlockedExams = async () => {
    if (blockedExamIds.length === 0 || examArchiveBusy) return
    setExamArchiveBusy(true)
    try {
      for (const id of blockedExamIds) {
        await setExamArchived(id, true)
      }
      setShowBulkExamDelete(false)
      setExamSelectionMode(false)
      setSelectedExamIds(new Set())
      setBlockedExamIds([])
      setDeleteError(null)
      setBanner({
        type: 'success',
        message: `Archived ${blockedExamIds.length} examination${blockedExamIds.length === 1 ? '' : 's'} from the active list. History preserved.`,
      })
      reloadExams()
    } catch (err) {
      setDeleteError(toErrorMessage(err, 'Unable to archive the protected examinations.'))
    } finally {
      setExamArchiveBusy(false)
    }
  }

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
    setEndsAt('')
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
    if (!isFutureDate(when)) {
      return setFormError('The scheduled start time must be in the future.')
    }

    let endsAtIso: string | null = null
    if (endsAt) {
      const closes = new Date(endsAt)
      if (Number.isNaN(closes.getTime())) {
        return setFormError('Please provide a valid close date and time.')
      }
      if (closes.getTime() <= when.getTime()) {
        return setFormError('The close time must be later than the start time.')
      }
      endsAtIso = closes.toISOString()
    }

    setSubmitting(true)
    try {
      await createExam({
        title: title.trim(),
        description: description.trim() || null,
        duration_minutes: duration,
        status,
        starts_at: when.toISOString(),
        ends_at: endsAtIso,
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
          {examSelectionMode ? (
            <>
              <span className="text-xs font-semibold text-slate-600">
                {selectedExamIds.size} selected
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setDeleteError(null)
                  setShowBulkExamDelete(true)
                }}
                disabled={selectedExamIds.size === 0}
              >
                Delete Selected{selectedExamIds.size > 0 ? ` (${selectedExamIds.size})` : ''}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setExamSelectionMode(false)
                  setSelectedExamIds(new Set())
                  setDeleteError(null)
                }}
              >
                Cancel
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setExamSelectionMode(true)}
              >
                Select
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
            </>
          )}
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
            const isSelected = selectedExamIds.has(exam.id)
            return (
              <Card key={exam.id} hoverEffect className="flex flex-col justify-between">
                <CardHeader>
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-start gap-2 min-w-0">
                      {examSelectionMode && (
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggleExamSelected(exam.id)}
                          aria-label={`Select examination ${exam.title}`}
                          className="mt-1 h-4 w-4 shrink-0 accent-indigo-600"
                        />
                      )}
                      <CardTitle className="text-sm font-semibold line-clamp-1">{exam.title}</CardTitle>
                    </div>
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

                  {exam.ends_at && (
                    <div className="flex items-center justify-between">
                      <span className="flex items-center gap-1 text-slate-400">
                        <Calendar className="h-3.5 w-3.5" /> Closes
                      </span>
                      <span className="font-medium text-slate-700 text-2xs">
                        {new Date(exam.ends_at).toLocaleString()}
                      </span>
                    </div>
                  )}

                  {(exam.submitted_count ?? 0) > 0 && (
                    <p className="pt-1 text-2xs text-emerald-700 bg-emerald-50 border border-emerald-100 rounded-md px-2 py-1">
                      {exam.submitted_count} submission(s) received — paper is locked.
                    </p>
                  )}
                </CardContent>

                <div className="border-t border-slate-100 bg-slate-50/50 px-4 py-3 rounded-b-xl flex flex-col gap-2">
                  {exam.status === 'draft' && (
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => {
                        setPublishError(null)
                        setPublishingExam(exam)
                      }}
                      className="w-full justify-center"
                    >
                      Publish Now
                    </Button>
                  )}
                  <div className="grid grid-cols-2 gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      icon={<Users className="h-3.5 w-3.5" />}
                      onClick={() => setAssignFor(exam)}
                      className="justify-center"
                    >
                      Assign Students
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPaperFor(exam)}
                      icon={<ListChecks className="h-3.5 w-3.5" />}
                      className="justify-center"
                    >
                      Manage Paper
                    </Button>
                  </div>

                  <div className="flex items-center gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => openEditDetails(exam)}
                    >
                      Edit
                    </Button>
                    <span className="flex-1" />
                    <span className="flex items-center gap-1 border-l border-slate-200 pl-2">
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
                    </span>
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
              placeholder="e.g. Coding Midterm Examination"
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

          <DateTimeField
            label="Closes At (End Time)"
            value={endsAt}
            onChange={setEndsAt}
            hint="If set, candidates cannot start after this time. Their attempt timer (Duration) is separate and still applies once started."
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

      {/* PUBLISH NOW */}
      {publishingExam && (
        <Modal
          isOpen={!!publishingExam}
          onClose={() => {
            if (!publishBusy) {
              setPublishingExam(null)
              setPublishError(null)
            }
          }}
          title="Publish this exam now?"
          subtitle={publishingExam.title}
          maxWidth="lg"
          footer={
            <div className="flex items-center justify-end gap-2 w-full">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setPublishingExam(null)
                  setPublishError(null)
                }}
                disabled={publishBusy}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={() => void handlePublishNow()}
                disabled={publishBusy}
                isLoading={publishBusy}
              >
                {publishBusy ? 'Publishing...' : 'Publish Now'}
              </Button>
            </div>
          }
        >
          <div className="space-y-3">
            <p className="text-xs text-slate-700 leading-relaxed">
              This will make the exam available to its assigned students immediately. The paper
              is re-validated and frozen exactly as it stands.
            </p>
            {publishError && (
              <div className="flex items-start gap-2 rounded-lg bg-rose-50 border border-rose-200 p-3 text-xs text-rose-700">
                <AlertCircle className="h-4 w-4 shrink-0 mt-0.5 text-rose-500" />
                <span>{publishError}</span>
              </div>
            )}
          </div>
        </Modal>
      )}

      {/* DELETE EXAMS (bulk). Per-exam dependency errors are reported by
          name; protected exams are never touched. */}
      {showBulkExamDelete && (
        <Modal
          isOpen={showBulkExamDelete}
          onClose={() => {
            if (!deleteBusy) {
              setShowBulkExamDelete(false)
              setDeleteError(null)
            }
          }}
          title={`Delete ${selectedExamIds.size} Selected Examination${selectedExamIds.size === 1 ? '' : 's'}`}
          subtitle="This cannot be undone"
          maxWidth="lg"
          footer={
            <div className="flex items-center justify-end gap-2 w-full">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setShowBulkExamDelete(false)
                  setDeleteError(null)
                }}
                disabled={deleteBusy}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={() => void handleBulkExamDelete()}
                disabled={deleteBusy}
                isLoading={deleteBusy}
              >
                {deleteBusy ? 'Deleting...' : `Delete ${selectedExamIds.size} Selected`}
              </Button>
            </div>
          }
        >
          <div className="space-y-3">
            <p className="text-xs text-slate-700 leading-relaxed">
              This permanently removes the selected examinations, their papers and their
              assignments. An examination with any candidate attempt is protected: it is kept
              untouched, and you can hide it from this list instead without losing history.
            </p>
            {deleteError && (
              <div className="flex items-start gap-2 rounded-lg bg-rose-50 border border-rose-200 p-3 text-xs text-rose-700">
                <AlertCircle className="h-4 w-4 shrink-0 mt-0.5 text-rose-500" />
                <span>{deleteError}</span>
              </div>
            )}
            {blockedExamIds.length > 0 && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void handleArchiveBlockedExams()}
                disabled={examArchiveBusy}
                isLoading={examArchiveBusy}
              >
                {examArchiveBusy
                  ? 'Archiving...'
                  : `Archive ${blockedExamIds.length} Protected Instead`}
              </Button>
            )}
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
