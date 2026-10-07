import React, { useEffect, useMemo, useState } from 'react'
import { Modal } from '../common/Modal'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { NotificationBanner } from '../common/NotificationBanner'
import {
  Search,
  Check,
  Loader2,
  ListChecks,
  Trash2,
  Code2,
  CheckSquare,
  AlertCircle,
  Lock,
} from 'lucide-react'
import {
  fetchExamPaper,
  fetchQuestions,
  setExamPaper,
  type ExamPaperItem,
} from '../../lib/teacherApi'
import type { Question } from '../../types'
import { toErrorMessage } from '../../lib/supabase'

interface ManagePaperModalProps {
  isOpen: boolean
  examId: string | null
  examTitle: string
  onClose: () => void
  onSaved: (totalQuestions: number, totalMarks: number) => void
}

/**
 * Real Manage Paper workflow.
 * Faculty explicitly selects the exact question set. There is no random
 * pool: every candidate receives the identical paper.
 */
export const ManagePaperModal: React.FC<ManagePaperModalProps> = ({
  isOpen,
  examId,
  examTitle,
  onClose,
  onSaved,
}) => {
  const [bank, setBank] = useState<Question[]>([])
  const [items, setItems] = useState<ExamPaperItem[]>([])
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState<'all' | 'mcq' | 'coding'>('all')
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
        const [questions, paper] = await Promise.all([
          fetchQuestions(),
          fetchExamPaper(examId),
        ])
        if (cancelled) return
        setBank(questions ?? [])
        setItems(paper?.items ?? [])
      } catch (err) {
        if (!cancelled) {
          setError(toErrorMessage(err, 'Unable to load the question bank.'))
          setBank([])
          setItems([])
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

  const selectedIds = useMemo(() => new Set(items.map((i) => i.question_id)), [items])

  const available = useMemo(() => {
    const term = search.trim().toLowerCase()
    return bank.filter((q) => {
      if (selectedIds.has(q.id)) return false
      if (typeFilter !== 'all' && q.question_type !== typeFilter) return false
      if (!term) return true
      return (
        q.title.toLowerCase().includes(term) ||
        q.question_text.toLowerCase().includes(term) ||
        (q.category || '').toLowerCase().includes(term) ||
        (q.subcategory || '').toLowerCase().includes(term)
      )
    })
  }, [bank, search, typeFilter, selectedIds])

  const add = (question: Question) => {
    setItems((prev) => [
      ...prev,
      {
        question_id: question.id,
        title: question.title,
        question_type: question.question_type,
        category: question.category ?? null,
        subcategory: question.subcategory ?? null,
        difficulty: question.difficulty ?? null,
        language: question.language ?? null,
        marks: question.marks ?? 10,
        bank_marks: question.marks ?? 10,
      },
    ])
  }

  const remove = (questionId: string) => {
    setItems((prev) => prev.filter((i) => i.question_id !== questionId))
  }

  const setMarks = (questionId: string, marks: number) => {
    setItems((prev) =>
      prev.map((i) => (i.question_id === questionId ? { ...i, marks } : i)),
    )
  }

  const move = (index: number, direction: -1 | 1) => {
    setItems((prev) => {
      const target = index + direction
      if (target < 0 || target >= prev.length) return prev
      const next = [...prev]
      ;[next[index], next[target]] = [next[target], next[index]]
      return next
    })
  }

  const totalMarks = items.reduce((sum, i) => sum + (i.marks || 0), 0)

  const handleSave = async () => {
    if (!examId) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const result = await setExamPaper(
        examId,
        items.map((i) => ({ question_id: i.question_id, marks: i.marks })),
      )
      onSaved(result?.total_questions ?? items.length, result?.total_marks ?? totalMarks)
      setSuccess(
        `Paper saved. ${result?.total_questions ?? items.length} question(s), ${
          result?.total_marks ?? totalMarks
        } total marks.`,
      )
    } catch (err) {
      setError(toErrorMessage(err, 'Unable to save the examination paper.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Manage Paper"
      subtitle={examTitle}
      maxWidth="xl"
      footer={
        <div className="flex items-center justify-between w-full gap-2">
          <span className="text-2xs text-slate-500">
            {items.length} question(s) • {totalMarks} marks
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
              {saving ? 'Saving...' : 'Save Paper'}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-3">
        {error && (
          <NotificationBanner type="error" message={error} onClose={() => setError(null)} />
        )}
        {success && (
          <NotificationBanner type="success" message={success} onClose={() => setSuccess(null)} />
        )}

        <p className="text-2xs text-slate-500 flex items-start gap-1.5">
          <Lock className="h-3 w-3 mt-0.5 shrink-0" />
          Every candidate receives this exact question set. Only the on-screen order and MCQ option
          order vary per candidate. Once a candidate submits, this paper is locked.
        </p>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {/* AVAILABLE BANK */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h4 className="text-2xs font-bold uppercase tracking-wider text-slate-500">
                Question Bank
              </h4>
              <Badge variant="neutral" size="sm">
                {available.length} available
              </Badge>
            </div>

            <div className="relative">
              <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search the bank..."
                className="w-full pl-9 pr-3 py-2 text-xs rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>

            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value as typeof typeFilter)}
              aria-label="Filter question type"
              className="w-full text-xs rounded-lg border border-slate-300 py-1.5 px-3 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
            >
              <option value="all">All types</option>
              <option value="coding">Programming only</option>
              <option value="mcq">MCQ only</option>
            </select>

            {loading ? (
              <div className="py-10 flex items-center justify-center gap-2 text-xs text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin text-indigo-600" />
                Loading question bank...
              </div>
            ) : available.length === 0 ? (
              <div className="rounded-xl border border-dashed border-slate-200 bg-white p-6 text-center">
                <AlertCircle className="h-5 w-5 text-slate-300 mx-auto mb-1.5" />
                <p className="text-xs text-slate-500">
                  {bank.length === 0
                    ? 'The question bank is empty. Create a question first.'
                    : 'No further questions available to add.'}
                </p>
              </div>
            ) : (
              <div className="max-h-[40vh] overflow-y-auto custom-scrollbar rounded-xl border border-slate-200 divide-y divide-slate-100">
                {available.map((q) => (
                  <button
                    key={q.id}
                    type="button"
                    onClick={() => add(q)}
                    className="w-full px-3 py-2.5 text-left bg-white hover:bg-indigo-50/50 transition-colors"
                  >
                    <span className="flex items-start gap-2">
                      <span className="mt-0.5 shrink-0">
                        {q.question_type === 'coding' ? (
                          <Code2 className="h-3.5 w-3.5 text-indigo-600" />
                        ) : (
                          <CheckSquare className="h-3.5 w-3.5 text-purple-600" />
                        )}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-xs font-semibold text-slate-800 truncate">
                          {q.title}
                        </span>
                        <span className="block text-2xs text-slate-400 truncate">
                          {q.category}
                          {q.subcategory ? ` / ${q.subcategory}` : ''} • {q.marks ?? 10} marks
                        </span>
                      </span>
                      <span className="shrink-0 text-2xs font-semibold text-indigo-600">Add</span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* SELECTED PAPER */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h4 className="text-2xs font-bold uppercase tracking-wider text-slate-500">
                This Paper
              </h4>
              <Badge variant="purple" size="sm">
                {items.length} selected • {totalMarks} marks
              </Badge>
            </div>

            {items.length === 0 ? (
              <div className="rounded-xl border border-dashed border-slate-200 bg-white p-8 text-center">
                <ListChecks className="h-6 w-6 text-slate-300 mx-auto mb-2" />
                <p className="text-xs font-semibold text-slate-700">No questions selected</p>
                <p className="text-2xs text-slate-500 mt-1">
                  Add questions from the bank to build this examination paper.
                </p>
              </div>
            ) : (
              <div className="max-h-[45vh] overflow-y-auto custom-scrollbar rounded-xl border border-slate-200 divide-y divide-slate-100">
                {items.map((item, idx) => (
                  <div key={item.question_id} className="px-3 py-2.5 bg-white space-y-2">
                    <div className="flex items-start gap-2">
                      <span className="h-5 w-5 shrink-0 rounded bg-indigo-50 text-indigo-700 flex items-center justify-center text-2xs font-bold mt-0.5">
                        {idx + 1}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-xs font-semibold text-slate-800 truncate">
                          {item.title}
                        </span>
                        <span className="block text-2xs text-slate-400">
                          {item.category}
                          {item.subcategory ? ` / ${item.subcategory}` : ''} •{' '}
                          {item.question_type === 'coding'
                            ? `Coding${item.language ? ` (${item.language})` : ''}`
                            : 'MCQ'}
                        </span>
                      </span>
                      <button
                        type="button"
                        onClick={() => remove(item.question_id)}
                        title="Remove from paper"
                        className="shrink-0 p-1 rounded text-slate-300 hover:text-rose-500 hover:bg-rose-50 transition-colors"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>

                    <div className="flex items-center gap-2 pl-7">
                      <label className="text-2xs font-semibold uppercase tracking-wider text-slate-400">
                        Marks
                      </label>
                      <input
                        type="number"
                        min={1}
                        max={100}
                        value={item.marks}
                        onChange={(e) =>
                          setMarks(item.question_id, Math.max(1, Number(e.target.value) || 1))
                        }
                        aria-label={`Marks for ${item.title}`}
                        className="w-20 rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                      />
                      <span className="text-2xs text-slate-400">bank: {item.bank_marks}</span>

                      <span className="ml-auto flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => move(idx, -1)}
                          disabled={idx === 0}
                          title="Move up"
                          className="px-1.5 py-0.5 rounded text-2xs font-bold text-slate-400 hover:bg-slate-100 disabled:opacity-30"
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          onClick={() => move(idx, 1)}
                          disabled={idx === items.length - 1}
                          title="Move down"
                          className="px-1.5 py-0.5 rounded text-2xs font-bold text-slate-400 hover:bg-slate-100 disabled:opacity-30"
                        >
                          ↓
                        </button>
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </Modal>
  )
}

export default ManagePaperModal
