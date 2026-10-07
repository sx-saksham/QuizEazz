import React, { useCallback, useMemo, useState } from 'react'
import type { Question } from '../../types'
import { Card, CardContent } from '../common/Card'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { Modal } from '../common/Modal'
import { EmptyState } from '../common/EmptyState'
import { CreateQuestionModal } from './CreateQuestionModal'
import {
  Database,
  Plus,
  Search,
  Filter,
  Code,
  CheckSquare,
  RefreshCw,
  FolderTree,
  FileCode,
  ShieldCheck,
  CheckCircle2,
  BookOpen,
  AlertCircle,
  Info,
} from 'lucide-react'
import { fetchQuestions } from '../../lib/teacherApi'
import { languageLabel } from '../../lib/languages'
import { useAsyncData } from '../../hooks/useAsyncData'

export const TeacherQuestionsView: React.FC = () => {
  const [searchQuery, setSearchQuery] = useState<string>('')
  const [selectedType, setSelectedType] = useState<string>('all')
  const [showCreateModal, setShowCreateModal] = useState<boolean>(false)
  const [inspectingQuestion, setInspectingQuestion] = useState<Question | null>(null)

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
            Departmental Question Repository
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            Standardized question items across Data Structures, Algorithms, OOP, and Core Programming
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            icon={<RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />}
            onClick={reload}
            disabled={loading}
          >
            Refresh
          </Button>
          <Button
            variant="primary"
            size="sm"
            icon={<Plus className="h-3.5 w-3.5" />}
            onClick={() => setShowCreateModal(true)}
          >
            Create Question
          </Button>
        </div>
      </div>

      {/* Filter / Search Bar */}
      <Card>
        <CardContent className="p-4 flex flex-col sm:flex-row gap-3 items-center justify-between">
          <div className="relative w-full sm:w-80">
            <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
            <input
              type="text"
              placeholder="Search by title, topic, category..."
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
          <h3 className="text-sm font-semibold text-red-900">Failed to Load Question Repository</h3>
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
          title="No Questions Found in Repository"
          description={
            searchQuery
              ? 'No questions matched your search criteria.'
              : 'The college question repository is currently empty. Question items created here can be assigned to multiple exams.'
          }
          action={
            <Button
              variant="primary"
              size="sm"
              icon={<Plus className="h-3.5 w-3.5" />}
              onClick={() => setShowCreateModal(true)}
            >
              Add Question to Repository
            </Button>
          }
        />
      ) : (
        <div className="grid gap-3">
          {filteredQuestions.map((q) => (
            <div
              key={q.id}
              className="rounded-xl border border-slate-200 bg-white p-4 transition-all hover:border-slate-300 shadow-2xs flex flex-col sm:flex-row sm:items-center justify-between gap-4"
            >
              <div className="space-y-1.5 flex-1">
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
                  <Badge
                    variant={
                      q.difficulty === 'easy'
                        ? 'success'
                        : q.difficulty === 'medium'
                        ? 'warning'
                        : 'danger'
                    }
                    size="sm"
                  >
                    {q.difficulty}
                  </Badge>

                  {q.question_type === 'coding' && (
                    <span className="text-[10px] font-semibold text-amber-700 bg-amber-50 px-2 py-0.5 rounded-md border border-amber-200/60">
                      Code judging pending (Judge0 CE)
                    </span>
                  )}
                </div>
                <p className="text-xs text-slate-500 line-clamp-1">{q.question_text}</p>
                <div className="flex items-center gap-3 text-2xs text-slate-400">
                  <span className="flex items-center gap-1">
                    <FolderTree className="h-3 w-3" /> {q.category} {q.subcategory && `• ${q.subcategory}`}
                  </span>
                  {q.language && <span>Language: {q.language}</span>}
                  <span>Marks: {q.marks || 10}</span>
                </div>
              </div>

              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setInspectingQuestion(q)}
                >
                  Inspect
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Read-Only Inspect Question Modal */}
      {inspectingQuestion && (
        <Modal
          isOpen={!!inspectingQuestion}
          onClose={() => setInspectingQuestion(null)}
          title={inspectingQuestion.title}
          subtitle={`Question Specification • ${inspectingQuestion.category}${inspectingQuestion.subcategory ? ` / ${inspectingQuestion.subcategory}` : ''}`}
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
                <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
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
                    <span className="text-[10px] text-slate-400">Presented to candidates</span>
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
                    <span className="text-[10px] font-bold text-amber-600 bg-amber-50 px-2 py-0.5 rounded border border-amber-200">
                      Teacher Only • Never Exposed to Students
                    </span>
                  </div>
                  <pre className="p-3 rounded-xl bg-slate-900 text-emerald-300 font-mono text-2xs overflow-x-auto max-h-48 custom-scrollbar border border-indigo-200">
                    {inspectingQuestion.reference_solution || '// Reference solution recorded internally'}
                  </pre>
                </div>

                {/* Programming evaluation honesty notice */}
                <div className="rounded-xl border border-amber-100 bg-amber-50/60 p-3">
                  <p className="text-xs font-bold text-amber-900 flex items-center gap-1.5">
                    <Info className="h-3.5 w-3.5" /> Programming Evaluation Status
                  </p>
                  <p className="text-2xs text-amber-900/80 mt-1 leading-relaxed">
                    This question is fully authored and can be assigned to an examination.
                    Candidate code is executed in an isolated environment after final
                    submission and graded as <strong>Pending Evaluation</strong> with zero
                    marks until a real verdict is produced. Marks are never estimated.
                  </p>
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
                          <span className="text-[11px] font-bold text-emerald-700 bg-white px-2 py-0.5 rounded border border-emerald-200 flex items-center gap-1">
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

            {/* Faculty Notes / Rubric */}
            {inspectingQuestion.explanation && (
              <div className="pt-2">
                <h5 className="text-2xs font-bold uppercase tracking-wider text-slate-400 mb-1 flex items-center gap-1">
                  <BookOpen className="h-3 w-3" /> Faculty Notes / Rubric
                </h5>
                <div className="p-3 rounded-lg bg-amber-50/60 border border-amber-200/80 text-xs text-amber-900 leading-relaxed">
                  {inspectingQuestion.explanation}
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
    </div>
  )
}
