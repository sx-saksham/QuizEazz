/**
 * Student-facing programming language labels.
 *
 * ===================================================================
 * THIS FILE IS PRESENTATION ONLY. IT MUST NEVER BE USED TO REWRITE A VALUE.
 * ===================================================================
 *
 * The `value` of every entry is the canonical internal identifier. It is the
 * exact string persisted to `answers.language` by `save_exam_answer()` and the
 * exact string future Judge0 CE integration will map to a language_id /
 * compiler id. Do NOT rename these values, do NOT add suffixes such as
 * "(GCC)", and do NOT derive an internal id from a label. Displaying a
 * friendly name must never change what is stored or transmitted.
 */

/**
 * QuizEazz V1 supports exactly one programming language: C++.
 *
 * Canonical internal identifier: `cpp` (Judge0 language_id 54).
 * Python, Java and C were removed permanently: there is no selector, no
 * per-question language, and no mapping for them anywhere in the grading
 * path. Do NOT re-add entries here without also restoring the Judge0
 * mapping, the database validation and the teacher authoring UI.
 */
export const QUIZEAZZ_CPP_LANGUAGE_ID = 'cpp' as const

/** Canonical internal identifier -> student-facing label. */
export const LANGUAGE_LABELS: Readonly<Record<string, string>> = {
  cpp: 'C++',
}

/**
 * Resolves a stored language identifier to its display label.
 * Unknown or missing identifiers are surfaced verbatim rather than hidden,
 * so an unexpected value is visible instead of silently mislabelled.
 */
export function languageLabel(identifier: string | null | undefined): string {
  if (!identifier) return 'Text'
  return LANGUAGE_LABELS[identifier] ?? identifier
}
