import React, { useEffect, useMemo, useState } from 'react'
import { Modal } from '../common/Modal'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { NotificationBanner } from '../common/NotificationBanner'
import { Search, Users, Check, Loader2, UserCheck } from 'lucide-react'
import {
  fetchCandidates,
  fetchExamAssignments,
  setExamAssignments,
  type Candidate,
} from '../../lib/teacherApi'
import { toErrorMessage } from '../../lib/supabase'

interface AssignStudentsModalProps {
  isOpen: boolean
  examId: string | null
  examTitle: string
  onClose: () => void
  onSaved: (assignedCount: number) => void
}

/**
 * Real Assign Students workflow.
 * Reads the current cohort and the candidate registry through authorized
 * RPCs, then persists the exact selection. Nothing is reported as saved
 * until the database confirms the write.
 */
export const AssignStudentsModal: React.FC<AssignStudentsModalProps> = ({
  isOpen,
  examId,
  examTitle,
  onClose,
  onSaved,
}) => {
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [initialCount, setInitialCount] = useState(0)
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  useEffect(() => {
    if (!isOpen || !examId) return
    let cancelled = false

    const load = async () => {
      setLoading(true)
      setError(null)
      setSuccess(null)
      setSearch('')
      try {
        const [roster, current] = await Promise.all([
          fetchCandidates(),
          fetchExamAssignments(examId),
        ])
        if (cancelled) return
        setCandidates(roster ?? [])
        setSelected(new Set(current?.assigned_student_ids ?? []))
        setInitialCount(current?.assigned_count ?? 0)
      } catch (err) {
        if (!cancelled) {
          setError(toErrorMessage(err, 'Unable to load the candidate registry.'))
          setCandidates([])
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void load()
    return () => {
      cancelled = true
    }
  }, [isOpen, examId])

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase()
    if (!term) return candidates
    return candidates.filter(
      (c) =>
        c.full_name?.toLowerCase().includes(term) ||
        c.email?.toLowerCase().includes(term) ||
        c.id.toLowerCase().includes(term),
    )
  }, [candidates, search])

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAllVisible = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      filtered.forEach((c) => next.add(c.id))
      return next
    })
  }

  const clearAll = () => setSelected(new Set())

  const handleSave = async () => {
    if (!examId) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const result = await setExamAssignments(examId, Array.from(selected))
      const count = result?.assigned_count ?? selected.size
      onSaved(count)
      setSuccess(
        `Cohort saved. ${count} candidate${count === 1 ? '' : 's'} assigned` +
          (result?.added ? ` (${result.added} added)` : '') +
          (result?.removed ? ` (${result.removed} removed)` : '') +
          '.',
      )
    } catch (err) {
      setError(toErrorMessage(err, 'Unable to save the assigned cohort.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Assign Students"
      subtitle={examTitle}
      maxWidth="lg"
      footer={
        <div className="flex items-center justify-between w-full gap-2">
          <span className="text-2xs text-slate-500">
            {selected.size} selected
            {initialCount > 0 && ` • ${initialCount} currently assigned`}
          </span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onClose} disabled={saving}>
              Close
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={handleSave}
              disabled={saving || loading}
              isLoading={saving}
              icon={<Check className="h-3.5 w-3.5" />}
            >
              {saving ? 'Saving...' : 'Save Assignment'}
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

        <div className="flex flex-col sm:flex-row gap-2 items-center">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search candidates by name, email or ID..."
              className="w-full pl-9 pr-3 py-2 text-xs rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={selectAllVisible} disabled={loading}>
              Select shown
            </Button>
            <Button variant="ghost" size="sm" onClick={clearAll} disabled={loading}>
              Clear
            </Button>
          </div>
        </div>

        {loading ? (
          <div className="py-10 flex items-center justify-center gap-2 text-xs text-slate-500">
            <Loader2 className="h-4 w-4 animate-spin text-indigo-600" />
            Loading candidate registry...
          </div>
        ) : candidates.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 bg-white p-8 text-center">
            <Users className="h-6 w-6 text-slate-300 mx-auto mb-2" />
            <p className="text-xs font-semibold text-slate-700">No candidates registered</p>
            <p className="text-2xs text-slate-500 mt-1">
              Students appear here automatically once they register on the candidate portal.
            </p>
          </div>
        ) : filtered.length === 0 ? (
          <p className="py-8 text-center text-xs text-slate-500">No candidates matched your search.</p>
        ) : (
          <div className="max-h-[45vh] overflow-y-auto custom-scrollbar rounded-xl border border-slate-200 divide-y divide-slate-100">
            {filtered.map((c) => {
              const isSelected = selected.has(c.id)
              return (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => toggle(c.id)}
                  className={`w-full flex items-center gap-3 px-3.5 py-2.5 text-left transition-colors ${
                    isSelected ? 'bg-indigo-50/60' : 'bg-white hover:bg-slate-50'
                  }`}
                >
                  <span
                    className={`h-4 w-4 shrink-0 rounded border flex items-center justify-center ${
                      isSelected ? 'bg-indigo-600 border-indigo-600 text-white' : 'border-slate-300 bg-white'
                    }`}
                  >
                    {isSelected && <Check className="h-3 w-3 stroke-[3]" />}
                  </span>

                  <span className="h-7 w-7 shrink-0 rounded-full bg-slate-100 text-slate-700 flex items-center justify-center font-bold text-2xs border border-slate-200">
                    {c.full_name ? c.full_name.charAt(0).toUpperCase() : 'S'}
                  </span>

                  <span className="min-w-0 flex-1">
                    <span className="block text-xs font-semibold text-slate-800 truncate">
                      {c.full_name || 'Unnamed candidate'}
                    </span>
                    <span className="block text-2xs text-slate-400 truncate">
                      {c.email || c.id}
                    </span>
                  </span>

                  {initialCount > 0 && selected.has(c.id) && (
                    <Badge variant="purple" size="sm">
                      Assigned
                    </Badge>
                  )}
                </button>
              )
            })}
          </div>
        )}

        <p className="text-2xs text-slate-400 flex items-start gap-1.5">
          <UserCheck className="h-3 w-3 mt-0.5 shrink-0" />
          Only assigned candidates can see and start this examination. Assignment is enforced by the
          database, not by this screen.
        </p>
      </div>
    </Modal>
  )
}

export default AssignStudentsModal
