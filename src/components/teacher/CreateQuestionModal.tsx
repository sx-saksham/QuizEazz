import React, { useState } from 'react'
import {
  Code2,
  CheckSquare,
  ShieldCheck,
  AlertCircle,
  FileCode,
  Info,
} from 'lucide-react'
import { Modal } from '../common/Modal'
import { Button } from '../common/Button'
import { Input } from '../common/Input'
import { createQuestion, updateQuestion } from '../../lib/teacherApi'
import { toErrorMessage } from '../../lib/supabase'
import type { Question } from '../../types'

interface CreateQuestionModalProps {
  isOpen: boolean
  onClose: () => void
  /**
   * Called only after the database has confirmed the insert. The parent
   * re-reads the question bank rather than trusting a local object, so the
   * list can never diverge from what is actually persisted.
   */
  onQuestionCreated: (newQuestion: Question) => void
  /**
   * When set, the modal edits this bank question instead of creating one.
   * Published exams, attempts and reviews read frozen snapshots, so no edit
   * — however large — can rewrite history. The parent remounts this modal
   * per question (key) so form state always starts from the stored row.
   */
  editingQuestion?: Question | null
  onQuestionUpdated?: (updatedQuestion: Question) => void
}

function asOptionList(value: unknown): { id: string; text: string }[] {
  if (!Array.isArray(value)) {
    return [
      { id: 'opt_1', text: '' },
      { id: 'opt_2', text: '' },
      { id: 'opt_3', text: '' },
      { id: 'opt_4', text: '' },
    ]
  }
  const mapped = (value as { id?: unknown; text?: unknown }[])
    .filter((o) => o && typeof o.id === 'string')
    .map((o) => ({ id: o.id as string, text: typeof o.text === 'string' ? o.text : '' }))
  return mapped.length >= 2
    ? mapped
    : [
        { id: 'opt_1', text: '' },
        { id: 'opt_2', text: '' },
        { id: 'opt_3', text: '' },
        { id: 'opt_4', text: '' },
      ]
}

function correctIdOf(question: Question | null | undefined): string {
  if (!question) return 'opt_1'
  if (question.correct_option_id) return question.correct_option_id
  const opts = asOptionList(question.options)
  const flagged = (question.options as { id?: unknown; is_correct?: unknown }[] | null)?.find?.(
    (o) => o && typeof o === 'object' && (o as { is_correct?: unknown }).is_correct === true,
  ) as { id?: unknown } | undefined
  if (flagged && typeof flagged.id === 'string') return flagged.id
  return opts[0]?.id ?? 'opt_1'
}

export const CreateQuestionModal: React.FC<CreateQuestionModalProps> = ({
  isOpen,
  onClose,
  onQuestionCreated,
  editingQuestion,
  onQuestionUpdated,
}) => {
  const isEditing = !!editingQuestion
  const [questionType, setQuestionType] = useState<'coding' | 'mcq'>(
    editingQuestion?.question_type ?? 'mcq',
  )

  // Common Fields
  const [title, setTitle] = useState(editingQuestion?.title ?? '')
  const [statement, setStatement] = useState(editingQuestion?.question_text ?? '')
  const [category, setCategory] = useState(editingQuestion?.category ?? 'Data Structures')
  const [subcategory, setSubcategory] = useState(editingQuestion?.subcategory ?? 'Arrays')
  // Difficulty is assigned server-side and is not editable from this form.
  const [marks, setMarks] = useState<number>(editingQuestion?.marks ?? 10)
  const [explanation, setExplanation] = useState(editingQuestion?.explanation ?? '')

  // Coding Specific Fields - QuizEazz V1 is C++ only. There is no language
  // selector: every programming question is authored, stored and graded as C++.
  const language = 'cpp'
  const [inputRequirements, setInputRequirements] = useState(
    editingQuestion?.input_requirements ?? '',
  )
  const [outputRequirements, setOutputRequirements] = useState(
    editingQuestion?.output_requirements ?? '',
  )
  const [constraints, setConstraints] = useState(editingQuestion?.constraints ?? '')
  const [starterCode, setStarterCode] = useState(
    editingQuestion?.starter_code ??
      '#include <bits/stdc++.h>\nusing namespace std;\n\nint main() {\n    // Write your C++ solution below\n    return 0;\n}\n',
  )
  const [referenceSolution, setReferenceSolution] = useState(
    editingQuestion?.reference_solution ??
      '#include <bits/stdc++.h>\nusing namespace std;\n\nint main() {\n    // Optimal verified C++ reference implementation\n    return 0;\n}\n',
  )

  // MCQ Specific Fields
  const [options, setOptions] = useState(() => asOptionList(editingQuestion?.options))
  const [correctOptionId, setCorrectOptionId] = useState(() => correctIdOf(editingQuestion))

  const [validationError, setValidationError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  const validate = (): string | null => {
    if (!title.trim()) return 'Please specify a question title.'
    if (!statement.trim()) return 'Please provide the complete question statement.'

    const m = Number(marks)
    if (!Number.isFinite(m) || m < 1 || m > 100) return 'Marks must be between 1 and 100.'

    if (questionType === 'mcq') {
      const emptyOpt = options.find((o) => !o.text.trim())
      if (emptyOpt) return 'All MCQ answer choices must be provided.'
      const ids = new Set(options.map((o) => o.id))
      if (!ids.has(correctOptionId)) return 'Select which option is the correct answer.'
    }

    if (questionType === 'coding') {
      if (!inputRequirements.trim()) return 'Input requirements are required.'
      if (!outputRequirements.trim()) return 'Output requirements are required.'
      if (!constraints.trim()) return 'Constraints are required (e.g. 1 <= N <= 10^5).'
      if (!referenceSolution.trim()) return 'A faculty reference solution is required.'
    }

    return null
  }

  const handleSaveQuestion = async () => {
    const problem = validate()
    if (problem) {
      setValidationError(problem)
      return
    }

    setIsSubmitting(true)
    setValidationError(null)

    try {
      // Authoritative SECURITY DEFINER RPC. Success is reported ONLY after the
      // database confirms the write. No optimistic local question is created.
      // In edit mode every field is sent; published exams keep reading
      // frozen snapshots regardless, so history cannot change.
      const payload = {
        title: title.trim(),
        question_text: statement.trim(),
        question_type: questionType,
        category,
        subcategory: subcategory.trim() || null,
        marks: Number(marks),
        explanation: explanation.trim() || null,
        language: questionType === 'coding' ? language : null,
        input_requirements: questionType === 'coding' ? inputRequirements.trim() : null,
        output_requirements: questionType === 'coding' ? outputRequirements.trim() : null,
        constraints: questionType === 'coding' ? constraints.trim() : null,
        starter_code: questionType === 'coding' ? starterCode : null,
        reference_solution: questionType === 'coding' ? referenceSolution : null,
        options:
          questionType === 'mcq'
            ? options.map((o) => ({ id: o.id, text: o.text.trim() }))
            : null,
        correct_answer: questionType === 'mcq' ? correctOptionId : null,
      }
      const saved = editingQuestion
        ? await updateQuestion({ question_id: editingQuestion.id, ...payload })
        : await createQuestion(payload)

      if (editingQuestion && onQuestionUpdated) onQuestionUpdated(saved as Question)
      else onQuestionCreated(saved as Question)
      onClose()
    } catch (err) {
      setValidationError(toErrorMessage(err, 'Failed to save the question.'))
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={isEditing ? 'Edit Examination Question' : 'Create Examination Question'}
      subtitle={
        isEditing
          ? 'Published exams keep their frozen snapshot — this edits only the bank version.'
          : 'Define problem specifications • Internal evaluation cases derived automatically'
      }
      maxWidth="xl"
      footer={
        <div className="flex items-center justify-between w-full">
          <span className="text-2xs text-slate-500 font-medium">
            {questionType === 'mcq'
              ? 'The correct answer is stored server-side and never sent to candidates.'
              : 'The reference solution is stored server-side and never sent to candidates.'}
          </span>

          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onClose} disabled={isSubmitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={handleSaveQuestion}
              disabled={isSubmitting}
              isLoading={isSubmitting}
            >
              {isSubmitting ? 'Saving...' : 'Save Question'}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-5 text-xs text-slate-700">
        {/* Type Toggle */}
        <div className="flex rounded-xl bg-slate-100 p-1 border border-slate-200">
          <button
            type="button"
            onClick={() => {
              setQuestionType('coding')
              setValidationError(null)
            }}
            className={`flex-1 py-2 text-xs font-bold rounded-lg transition-all flex items-center justify-center gap-2 ${
              questionType === 'coding'
                ? 'bg-white text-indigo-700 shadow-2xs font-extrabold'
                : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            <Code2 className="h-4 w-4 text-indigo-600" />
            <span>Programming / Coding Problem</span>
          </button>
          <button
            type="button"
            onClick={() => {
              setQuestionType('mcq')
              setValidationError(null)
            }}
            className={`flex-1 py-2 text-xs font-bold rounded-lg transition-all flex items-center justify-center gap-2 ${
              questionType === 'mcq'
                ? 'bg-white text-indigo-700 shadow-2xs font-extrabold'
                : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            <CheckSquare className="h-4 w-4 text-indigo-600" />
            <span>Multiple Choice Question (MCQ)</span>
          </button>
        </div>

        {/* Error Alert */}
        {validationError && (
          <div className="rounded-xl border border-rose-200 bg-rose-50/70 p-3 text-rose-900 flex items-start gap-2 text-xs">
            <AlertCircle className="h-4 w-4 text-rose-600 shrink-0 mt-0.5" />
            <span>{validationError}</span>
          </div>
        )}

        {/* General Metadata Row */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div className="sm:col-span-2">
            <Input
              label="Question Title / Problem Name"
              placeholder="e.g. Reverse a Singly Linked List"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
            />
          </div>

          <div>
            <label className="block text-2xs font-bold uppercase tracking-wider text-slate-500 mb-1.5">
              Marks Allocation
            </label>
            <input
              type="number"
              min={1}
              max={100}
              value={marks}
              onChange={(e) => setMarks(Number(e.target.value))}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
        </div>

        {/* Classification Row (Difficulty Tier removed per requirements) */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="block text-2xs font-bold uppercase tracking-wider text-slate-500 mb-1.5">
              Category
            </label>
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            >
              <option value="Data Structures">Data Structures</option>
              <option value="Algorithms">Algorithms</option>
              <option value="Object Oriented Programming">Object Oriented Programming</option>
              <option value="Core Programming">Core Programming</option>
            </select>
          </div>

          <div>
            <Input
              label="Subcategory / Topic"
              placeholder="e.g. Linked Lists, Two Pointers"
              value={subcategory}
              onChange={(e) => setSubcategory(e.target.value)}
            />
          </div>
        </div>

        {/* Problem Statement */}
        <div>
          <label className="block text-2xs font-bold uppercase tracking-wider text-slate-500 mb-1.5">
            Problem Description & Task Statement
          </label>
          <textarea
            value={statement}
            onChange={(e) => setStatement(e.target.value)}
            placeholder="Describe the technical requirements and task for candidates..."
            className="w-full rounded-xl border border-slate-300 bg-white p-3 text-xs text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 leading-relaxed min-h-[110px] max-h-[110px] overflow-y-auto resize-none custom-scrollbar"
            required
          />
        </div>

        {/* Optional Explanation / Solution Notes */}
        <div>
          <label className="block text-2xs font-bold uppercase tracking-wider text-slate-500 mb-1.5">
            Internal Faculty Notes / Rubric (Optional)
          </label>
          <textarea
            value={explanation}
            onChange={(e) => setExplanation(e.target.value)}
            placeholder="Internal grading notes or theoretical solution summary..."
            className="w-full rounded-xl border border-slate-300 bg-white p-2.5 text-xs text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 min-h-[80px] max-h-[80px] overflow-y-auto resize-none custom-scrollbar"
          />
        </div>

        {/* CODING SPECIFICATIONS & AUTOMATED EVALUATION CASE DERIVATION */}
        {questionType === 'coding' && (
          <div className="space-y-4 pt-2 border-t border-slate-200">
            <div className="rounded-xl border border-amber-100 bg-amber-50/60 p-3.5">
              <div className="flex items-start gap-2">
                <Info className="h-4 w-4 text-amber-600 shrink-0 mt-0.5" />
                <div className="space-y-1">
                  <h4 className="text-xs font-bold text-amber-900">
                    Programming evaluation status
                  </h4>
                  <p className="text-2xs text-amber-900/80 leading-relaxed">
                    The question can be authored and assigned now. Objective code execution requires a
                    connected <strong>Judge0 CE</strong> sandbox, which is not part of this
                    deployment yet. Until then, submitted code is recorded and shown back to faculty
                    and candidates, but it is graded as{' '}
                    <strong>Pending Evaluation</strong> and awards no marks. No marks are ever
                    estimated or inferred.
                  </p>
                </div>
              </div>
            </div>

            {/* Language & Specifications */}
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label className="block text-2xs font-bold uppercase tracking-wider text-slate-500 mb-1.5">
                  Programming Language
                </label>
                <div
                  aria-label="Programming language"
                  className="w-full rounded-xl border border-slate-200 bg-slate-100 px-3 py-2 text-xs font-bold text-slate-700"
                >
                  C++
                </div>
              </div>

              <div className="sm:col-span-2">
                <Input
                  label="Constraints"
                  placeholder="e.g. 1 <= N <= 10^5, -10^9 <= A[i] <= 10^9"
                  value={constraints}
                  onChange={(e) => setConstraints(e.target.value)}
                  required
                />
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="block text-2xs font-bold uppercase tracking-wider text-slate-500 mb-1.5">
                  Input Requirements / Format
                </label>
                <textarea
                  value={inputRequirements}
                  onChange={(e) => setInputRequirements(e.target.value)}
                  placeholder="e.g. First line contains N, followed by N integers."
                  className="w-full rounded-xl border border-slate-300 bg-white p-2.5 text-xs text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 min-h-[80px] max-h-[80px] overflow-y-auto resize-none custom-scrollbar"
                  required
                />
              </div>

              <div>
                <label className="block text-2xs font-bold uppercase tracking-wider text-slate-500 mb-1.5">
                  Output Requirements / Format
                </label>
                <textarea
                  value={outputRequirements}
                  onChange={(e) => setOutputRequirements(e.target.value)}
                  placeholder="e.g. Return the head of the reversed singly linked list."
                  className="w-full rounded-xl border border-slate-300 bg-white p-2.5 text-xs text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 min-h-[80px] max-h-[80px] overflow-y-auto resize-none custom-scrollbar"
                  required
                />
              </div>
            </div>

            {/* Code Sections */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="block text-2xs font-bold uppercase tracking-wider text-slate-500 mb-1.5 flex items-center gap-1">
                  <FileCode className="h-3.5 w-3.5 text-slate-400" />
                  Candidate Starter / Boilerplate Code
                </label>
                <textarea
                  value={starterCode}
                  onChange={(e) => setStarterCode(e.target.value)}
                  className="w-full rounded-xl border border-slate-300 font-mono text-2xs p-3 bg-slate-900 text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500 min-h-[160px] max-h-[160px] overflow-y-auto resize-none custom-scrollbar"
                />
                <p className="text-[10px] text-slate-400 mt-1">
                  This skeleton code is presented to candidates in Monaco Editor during the exam.
                </p>
              </div>

              <div>
                <label className="block text-2xs font-bold uppercase tracking-wider text-indigo-700 mb-1.5 flex items-center gap-1 font-bold">
                  <ShieldCheck className="h-3.5 w-3.5 text-indigo-600" />
                  Verified Faculty Reference Solution
                </label>
                <textarea
                  value={referenceSolution}
                  onChange={(e) => setReferenceSolution(e.target.value)}
                  className="w-full rounded-xl border border-indigo-300 font-mono text-2xs p-3 bg-slate-900 text-emerald-300 focus:outline-none focus:ring-2 focus:ring-indigo-500 min-h-[160px] max-h-[160px] overflow-y-auto resize-none custom-scrollbar"
                  required
                />
                <p className="text-[10px] text-slate-400 mt-1">
                  Stored server-side. Candidates never receive this content.
                </p>
              </div>
            </div>
          </div>
        )}

        {/* MCQ OPTIONS */}
        {questionType === 'mcq' && (
          <div className="space-y-3 pt-2 border-t border-slate-200">
            <p className="text-2xs font-bold uppercase tracking-wider text-slate-500">
              MCQ Options (Select the correct answer key)
            </p>
            <div className="space-y-2">
              {options.map((opt, idx) => {
                const letter = String.fromCharCode(65 + idx)
                const isCorrect = correctOptionId === opt.id
                return (
                  <div key={opt.id} className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setCorrectOptionId(opt.id)}
                      className={`h-9 w-9 shrink-0 rounded-xl font-bold text-xs flex items-center justify-center transition-all ${
                        isCorrect
                          ? 'bg-emerald-600 text-white shadow-2xs'
                          : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                      }`}
                    >
                      {letter}
                    </button>
                    <input
                      type="text"
                      placeholder={`Option ${letter} text...`}
                      value={opt.text}
                      onChange={(e) => {
                        const updated = [...options]
                        updated[idx].text = e.target.value
                        setOptions(updated)
                      }}
                      className="flex-1 rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </div>
    </Modal>
  )
}
