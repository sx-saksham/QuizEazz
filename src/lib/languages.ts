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

/** Canonical internal identifier -> student-facing label. */
export const LANGUAGE_LABELS: Readonly<Record<string, string>> = {
  python: 'Python',
  cpp: 'C++',
  java: 'Java',
  c: 'C',
}

/** Selector order shown to the candidate. Values are canonical ids. */
export const LANGUAGE_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'python', label: LANGUAGE_LABELS.python },
  { value: 'cpp', label: LANGUAGE_LABELS.cpp },
  { value: 'java', label: LANGUAGE_LABELS.java },
  { value: 'c', label: LANGUAGE_LABELS.c },
]

/**
 * Resolves a stored language identifier to its display label.
 * Unknown or missing identifiers are surfaced verbatim rather than hidden,
 * so an unexpected value is visible instead of silently mislabelled.
 */
export function languageLabel(identifier: string | null | undefined): string {
  if (!identifier) return 'Text'
  return LANGUAGE_LABELS[identifier] ?? identifier
}
