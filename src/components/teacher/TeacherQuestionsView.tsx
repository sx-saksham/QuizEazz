import React, { useCallback, useMemo, useState } from 'react'
import type { Question } from '../../types'
import { Card, CardContent } from '../common/Card'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { Modal } from '../common/Modal'
import { EmptyState } from '../common/EmptyState'
import { NotificationBanner } from '../common/NotificationBanner'
import { CreateQuestionModal } from './CreateQuestionModal'
import { ManageTestsModal } from './ManageTestsModal'
import {
  Database,
  Plus,
  Search,
  Filter,
  Code,
  CheckSquare,
  RefreshCw,
  FileCode,
  ShieldCheck,
  CheckCircle2,
  AlertCircle,
  Info,
} from 'lucide-react'
import { fetchQuestions, deleteQuestion, setQuestionArchived } from '../../lib/teacherApi'
import { languageLabel } from '../../lib/languages'
import { useAsyncData } from '../../hooks/useAsyncData'
import { toErrorMessage } from '../../lib/supabase'

export const TeacherQuestionsView: React.FC = () => {
  const [searchQuery, setSearchQuery] = useState<string>('')
  const [selectedType, setSelectedType] = useState<string>('all')
  const [showCreateModal, setShowCreateModal] = useState<boolean>(false)
  const [inspectingQuestion, setInspectingQuestion] = useState<Question | null>(null)
  const [testsFor, setTestsFor] = useState<Question | null>(null)
  const [editingQuestion, setEditingQuestion] = useState<Question | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [selectionMode, setSelectionMode] = useState(false)
  const [showBulkDelete, setShowBulkDelete] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [blockedIds, setBlockedIds] = useState<string[]>([])
  const [archiveBusy, setArchiveBusy] = useState(false)

  const toggleSelected = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // Bulk delete, executed strictly one question at a time so a protected
  // question reports its own dependency error without aborting the rest.
  // The database remains the authority for what may be deleted.
  // Anything blocked by historical references can be archived instead,
  // which hides it from this list without touching history.
  const handleBulkDelete = async () => {
    if (selectedIds.size === 0 || deleteBusy) return
    setDeleteBusy(true)
    setDeleteError(null)
    setBlockedIds([])
    const ids = Array.from(selectedIds)
    const blocked: string[] = []
    const blockedIdList: string[] = []
    let deleted = 0
    for (const id of ids) {
      try {
        await deleteQuestion(id)
        deleted += 1
      } catch (err) {
        const title = questions.find((q) => q.id === id)?.title ?? id
        blocked.push(`${title}: ${toErrorMessage(err, 'Unable to delete the question.')}`)
        blockedIdList.push(id)
      }
    }
    setDeleteBusy(false)
    setBlockedIds(blockedIdList)
    if (blocked.length > 0) {
      setDeleteError(
        `Deleted ${deleted} of ${ids.length}. These are used by existing examination history and cannot be deleted:\n` +
          blocked.join('\n'),
      )
    } else {
      setShowBulkDelete(false)
      setSelectionMode(false)
      setSelectedIds(new Set())
    }
    reload()
  }

  const handleArchiveBlocked = async () => {
    if (blockedIds.length === 0 || archiveBusy) return
    setArchiveBusy(true)
    try {
      for (const id of blockedIds) {
        await setQuestionArchived(id, true)
      }
      setShowBulkDelete(false)
      setSelectionMode(false)
      setSelectedIds(new Set())
      setBlockedIds([])
      setDeleteError(null)
      reload()
    } catch (err) {
      setDeleteError(toErrorMessage(err, 'Unable to archive the protected questions.'))
    } finally {
      setArchiveBusy(false)
    }
  }

  // RPC-only. There is deliberately no fallback to direct table access.
  const load = useCallback(() => fetchQuestions(), [])
  const { data: questions, error: fetchError, loading, reload } = useAsyncData<Question[]>(
    load,
    [],
    { errorFallback: 'Unable to retrieve the question repository.' },
  )

  const filteredQuestions = useMemo(() => {
    const term = searchQuery.trim().toLowerCase()
    return questions.filter((q) => {
      if (selectedType !== 'all' && q.question_type !== selectedType) return false
      if (!term) return true
      return (
        q.title.toLowerCase().includes(term) ||
        q.question_text.toLowerCase().includes(term) ||
        (q.category || '').toLowerCase().includes(term) ||
        (q.subcategory || '').toLowerCase().includes(term)
      )
    })
  }, [questions, searchQuery, selectedType])

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900 sm:text-2xl">
            Question Bank
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            Create, manage, and review examination questions.
          </p>
        </div>

        <div className="flex items-center gap-2">
          {selectionMode ? (
            <>
              <span className="text-xs font-semibold text-slate-600">
                {selectedIds.size} selected
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setDeleteError(null)
                  setShowBulkDelete(true)
                }}
                disabled={selectedIds.size === 0}
              >
                Delete Selected{selectedIds.size > 0 ? ` (${selectedIds.size})` : ''}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setSelectionMode(false)
                  setSelectedIds(new Set())
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
                onClick={() => setSelectionMode(true)}
              >
                Select
              </Button>
              <Button
                variant="primary"
                size="sm"
                icon={<Plus className="h-3.5 w-3.5" />}
                onClick={() => setShowCreateModal(true)}
              >
                Create Question
              </Button>
            </>
          )}
        </div>
      </div>

      {/* Filter / Search Bar */}
      <Card>
        <CardContent className="p-4 flex flex-col sm:flex-row gap-3 items-center justify-between">
          <div className="relative w-full sm:w-80">
            <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
            <input
              type="text"
              placeholder="Search by title..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-9 pr-4 py-2 text-xs rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
            />
          </div>

          <div className="flex items-center gap-2 w-full sm:w-auto">
            <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1">
              <Filter className="h-3 w-3" /> Type:
            </span>
            <select
              value={selectedType}
              onChange={(e) => setSelectedType(e.target.value)}
              className="text-xs rounded-lg border border-slate-300 py-1.5 px-3 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="all">All Types</option>
              <option value="coding">Programming / Coding</option>
              <option value="mcq">Multiple Choice (MCQ)</option>
            </select>
          </div>
        </CardContent>
      </Card>

      {/* Questions List */}
      {loading ? (
        <div className="rounded-xl border border-slate-200 bg-white p-12 text-center">
          <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-blue-600 border-t-transparent" />
          <p className="mt-3 text-xs font-medium text-slate-500">Loading question bank...</p>
        </div>
      ) : fetchError ? (
        <div className="rounded-xl border border-red-200 bg-red-50/50 p-8 text-center">
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-red-100 text-red-600 mb-3">
            <AlertCircle className="h-5 w-5" />
          </div>
          <h3 className="text-sm font-semibold text-red-900">Failed to Load Question Bank</h3>
          <p className="mt-1 text-xs text-red-600 max-w-md mx-auto">{fetchError}</p>
          <div className="mt-4">
            <Button
              variant="secondary"
              size="sm"
              icon={<RefreshCw className="h-3.5 w-3.5" />}
              onClick={reload}
            >
              Retry
            </Button>
          </div>
        </div>
      ) : filteredQuestions.length === 0 ? (
        <EmptyState
          icon={<Database className="h-6 w-6 text-slate-400" />}
          title="No Questions in Question Bank"
          description={
            searchQuery
              ? 'No questions matched your search criteria.'
              : 'The question bank is currently empty. Question items created here can be assigned to multiple exams.'
          }
          action={
            <Button
              variant="primary"
              size="sm"
              icon={<Plus className="h-3.5 w-3.5" />}
              onClick={() => setShowCreateModal(true)}
            >
              Add Question to Bank
            </Button>
          }
        />
      ) : (
        <div className="grid gap-3">
          {filteredQuestions.map((q) => {
            const isSelected = selectedIds.has(q.id)
            return (
            <div
              key={q.id}
              className={`rounded-xl border bg-white p-4 transition-all shadow-2xs flex flex-col sm:flex-row sm:items-center justify-between gap-4 ${
                isSelected ? 'border-indigo-400 ring-1 ring-indigo-200' : 'border-slate-200 hover:border-slate-300'
              }`}
            >
              <div className="flex items-start gap-3 flex-1 min-w-0">
                {selectionMode && (
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => toggleSelected(q.id)}
                    aria-label={`Select question ${q.title}`}
                    className="mt-1 h-4 w-4 shrink-0 accent-indigo-600"
                  />
                )}
                <div className="space-y-1.5 flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    {q.question_type === 'coding' ? (
                      <span className="flex items-center gap-1 rounded bg-blue-50 px-2 py-0.5 text-2xs font-semibold text-blue-700">
                        <Code className="h-3 w-3" />
                        CODING
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 rounded bg-purple-50 px-2 py-0.5 text-2xs font-semibold text-purple-700">
                        <CheckSquare className="h-3 w-3" />
                        MCQ
                      </span>
                    )}
                    <h3 className="text-sm font-semibold text-slate-900">{q.title}</h3>
                  </div>
                  <p className="text-xs text-slate-500 line-clamp-1">{q.question_text}</p>
                  <div className="flex items-center gap-3 text-2xs text-slate-400">
                    {q.question_type === 'coding' && <span>C++</span>}
                    <span>Marks: {q.marks || 10}</span>
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-2 shrink-0">
                {q.question_type === 'coding' && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => setTestsFor(q)}
                  >
                    Hidden Tests
                  </Button>
                )}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setEditingQuestion(q)}
                >
                  Edit
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setInspectingQuestion(q)}
                >
                  Inspect
                </Button>
              </div>
            </div>
            )
          })}
        </div>
      )}

      {/* Read-Only Inspect Question Modal */}
      {inspectingQuestion && (
        <Modal
          isOpen={!!inspectingQuestion}
          onClose={() => setInspectingQuestion(null)}
          title={inspectingQuestion.title}
          subtitle="Question Specification"
          maxWidth="xl"
          footer={
            <Button variant="outline" size="sm" onClick={() => setInspectingQuestion(null)}>
              Close
            </Button>
          }
        >
          <div className="space-y-4">
            {/* Top Specs Bar */}
            <div className="flex flex-wrap items-center gap-2 pb-3 border-b border-slate-100">
              <Badge variant={inspectingQuestion.question_type === 'coding' ? 'purple' : 'default'} size="sm">
                {inspectingQuestion.question_type === 'coding' ? 'Programming Problem' : 'Multiple Choice Question'}
              </Badge>
              <Badge variant="neutral" size="sm">
                Marks: {inspectingQuestion.marks || 10}
              </Badge>
              {inspectingQuestion.language && (
                <Badge variant="purple" size="sm">
                  Language: {languageLabel(inspectingQuestion.language)}
                </Badge>
              )}
              {inspectingQuestion.question_type === 'coding' && (
                <span className="inline-flex items-center gap-1 text-2xs font-semibold text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
                  <Info className="h-3 w-3" /> Auto-graded in C++ on submission
                </span>
              )}
            </div>

            {/* Problem Statement */}
            <div>
              <h4 className="text-2xs font-bold uppercase tracking-wider text-slate-400 mb-1">
                Problem Description & Statement
              </h4>
              <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 text-xs text-slate-800 whitespace-pre-wrap leading-relaxed">
                {inspectingQuestion.question_text}
              </div>
            </div>

            {/* Coding Specifications */}
            {inspectingQuestion.question_type === 'coding' && (
              <div className="space-y-3 pt-2">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <h5 className="text-2xs font-bold uppercase tracking-wider text-slate-400 mb-1">
                      Input Requirements
                    </h5>
                    <div className="p-2.5 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-700 whitespace-pre-wrap">
                      {inspectingQuestion.input_requirements || 'Standard input stream specification.'}
                    </div>
                  </div>
                  <div>
                    <h5 className="text-2xs font-bold uppercase tracking-wider text-slate-400 mb-1">
                      Output Requirements
                    </h5>
                    <div className="p-2.5 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-700 whitespace-pre-wrap">
                      {inspectingQuestion.output_requirements || 'Standard output stream format.'}
                    </div>
                  </div>
                </div>

                {inspectingQuestion.constraints && (
                  <div>
                    <h5 className="text-2xs font-bold uppercase tracking-wider text-slate-400 mb-1">
                      Constraints & Complexity Bounds
                    </h5>
                    <div className="p-2.5 rounded-lg bg-slate-50 border border-slate-200 text-xs font-mono text-slate-700">
                      {inspectingQuestion.constraints}
                    </div>
                  </div>
                )}

                {/* Candidate Starter Code */}
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <h5 className="text-2xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1">
                      <FileCode className="h-3 w-3" /> Candidate Starter Code
                    </h5>
                    <span className="text-2xs text-slate-400">Presented to candidates</span>
                  </div>
                  <pre className="p-3 rounded-xl bg-slate-900 text-slate-100 font-mono text-2xs overflow-x-auto max-h-48 custom-scrollbar">
                    {inspectingQuestion.starter_code || '// No boilerplate specified'}
                  </pre>
                </div>

                {/* Faculty Reference Solution (CONFIDENTIAL - Teacher Only) */}
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <h5 className="text-2xs font-bold uppercase tracking-wider text-indigo-700 flex items-center gap-1 font-bold">
                      <ShieldCheck className="h-3 w-3 text-indigo-600" /> Faculty Reference Solution (Confidential)
                    </h5>
                    <span className="text-2xs font-bold text-amber-600 bg-amber-50 px-2 py-0.5 rounded border border-amber-200">
                      Teacher Only • Never Exposed to Students
                    </span>
                  </div>
                  <pre className="p-3 rounded-xl bg-slate-900 text-emerald-300 font-mono text-2xs overflow-x-auto max-h-48 custom-scrollbar border border-indigo-200">
                    {inspectingQuestion.reference_solution || '// Reference solution recorded internally'}
                  </pre>
                </div>

              </div>
            )}

            {/* MCQ Options Display */}
            {inspectingQuestion.question_type === 'mcq' && Array.isArray(inspectingQuestion.options) && (
              <div>
                <h5 className="text-2xs font-bold uppercase tracking-wider text-slate-400 mb-2">
                  Answer Choices (Teacher Answer Key)
                </h5>
                <div className="space-y-2">
                  {(Array.isArray(inspectingQuestion.options) ? inspectingQuestion.options : []).map(
                    (opt: { id: string; text: string }) => {
                    // Correctness is derived server-side from the private key.
                    const isCorrect = inspectingQuestion.correct_option_id === opt.id
                    return (
                      <div
                        key={opt.id}
                        className={`p-2.5 rounded-lg border text-xs flex items-center justify-between ${
                          isCorrect
                            ? 'bg-emerald-50/80 border-emerald-300 text-emerald-950 font-medium'
                            : 'bg-slate-50 border-slate-200 text-slate-700'
                        }`}
                      >
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-2xs uppercase text-slate-400">{opt.id}:</span>
                          <span>{opt.text}</span>
                        </div>
                        {isCorrect && (
                          <span className="text-2xs font-bold text-emerald-700 bg-white px-2 py-0.5 rounded border border-emerald-200 flex items-center gap-1">
                            <CheckCircle2 className="h-3 w-3" /> Correct Key
                          </span>
                        )}
                      </div>
                    )
                    },
                  )}
                </div>
              </div>
            )}

            {/* Metadata Footer */}
            <div className="pt-2 border-t border-slate-100 flex items-center justify-between text-2xs text-slate-400">
              <span>Item ID: {inspectingQuestion.id}</span>
              {inspectingQuestion.created_at && (
                <span>Authored: {new Date(inspectingQuestion.created_at).toLocaleDateString()}</span>
              )}
            </div>
          </div>
        </Modal>
      )}

      {/* Automated Evaluation Case Question Creation Modal */}
      <CreateQuestionModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onQuestionCreated={reload}
      />

      {/* Bank editing. Remount per question so the form always starts from
          the stored row. Published exams are unaffected (frozen snapshots). */}
      {editingQuestion && (
        <CreateQuestionModal
          key={editingQuestion.id}
          isOpen={!!editingQuestion}
          onClose={() => setEditingQuestion(null)}
          onQuestionCreated={reload}
          editingQuestion={editingQuestion}
          onQuestionUpdated={() => {
            setEditingQuestion(null)
            reload()
          }}
        />
      )}

      {/* Hidden test cases for one coding question (faculty only) */}
      <ManageTestsModal
        questionId={testsFor?.id ?? null}
        questionTitle={testsFor?.title ?? ''}
        onClose={() => setTestsFor(null)}
      />

      {/* Bulk delete confirmation. The database enforces dependencies per
          question: anything referenced by a paper or answer is protected and
          reported by name; the rest is deleted. */}
      {showBulkDelete && (
        <Modal
          isOpen={showBulkDelete}
          onClose={() => {
            if (!deleteBusy) {
              setShowBulkDelete(false)
              setDeleteError(null)
            }
          }}
          title={`Delete ${selectedIds.size} Selected Question${selectedIds.size === 1 ? '' : 's'}`}
          subtitle="This cannot be undone"
          maxWidth="lg"
          footer={
            <div className="flex items-center justify-end gap-2 w-full">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setShowBulkDelete(false)
                  setDeleteError(null)
                }}
                disabled={deleteBusy}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={() => void handleBulkDelete()}
                disabled={deleteBusy}
                isLoading={deleteBusy}
              >
                {deleteBusy ? 'Deleting...' : `Delete ${selectedIds.size} Selected`}
              </Button>
            </div>
          }
        >
          <div className="space-y-3">
            <p className="text-xs text-slate-700 leading-relaxed">
              This permanently removes the selected questions from the bank. Questions used in
              any exam paper or referenced by any candidate answer are protected: they are kept
              untouched, and you can hide them from this list instead without losing history.
            </p>
            {deleteError && (
              <NotificationBanner type="error" message={deleteError} onClose={() => setDeleteError(null)} />
            )}
            {blockedIds.length > 0 && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void handleArchiveBlocked()}
                disabled={archiveBusy}
                isLoading={archiveBusy}
              >
                {archiveBusy
                  ? 'Archiving...'
                  : `Archive ${blockedIds.length} Protected Instead`}
              </Button>
            )}
          </div>
        </Modal>
      )}
    </div>
  )
}
