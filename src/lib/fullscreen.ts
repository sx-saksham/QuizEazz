/**
 * Browser Fullscreen API wrapper + runtime diagnostics for the exam workspace.
 *
 * ===================================================================
 * WHAT FULLSCREEN IS, AND WHAT IT IS NOT
 * ===================================================================
 *
 * The Fullscreen API hides the browser's own chrome (tab strip, address bar,
 * bookmarks). That is ALL it does.
 *
 * It is NOT a kiosk and it is NOT an anti-cheat control. It does not prevent
 * Alt+Tab, virtual desktops, a second monitor, a phone, or a screenshot.
 * JavaScript cannot intercept Alt+Tab — the operating system consumes it before
 * any page code runs. All this application can do is DETECT the resulting loss
 * of focus and pause. That is what it does.
 *
 * ===================================================================
 * WHY EVERY ENTRY POINT MUST SIT INSIDE A USER GESTURE
 * ===================================================================
 *
 * Chrome requires transient user activation for `requestFullscreen()` and
 * rejects it with a `NotAllowedError` otherwise. Transient activation does not
 * survive an awaited network round trip. The exam workspace needs three awaited
 * RPCs (attempt, paper, saved answers) before it can render anything, so a
 * request issued from a mount effect can never succeed. It is not a matter of
 * timing luck — it is refused by policy.
 *
 * `document.fullscreenElement` is also NULL immediately after the call returns;
 * the transition is queued and settles on a later task. So "did it work?" can
 * only be answered by reading `document.fullscreenElement` afterwards, or by
 * waiting for `fullscreenchange`. This module never reports success on the
 * strength of the call alone.
 */

interface FullscreenDocument extends Document {
  webkitFullscreenElement?: Element | null
  webkitExitFullscreen?: () => Promise<void>
}

interface FullscreenElement extends HTMLElement {
  webkitRequestFullscreen?: () => Promise<void>
}

export type FullscreenFailureReason =
  | 'unsupported'
  | 'no-user-activation'
  | 'denied-by-browser'
  | 'missing-element'
  | 'unknown-error'

export interface FullscreenOutcome {
  /** True only after document.fullscreenElement has been confirmed non-null. */
  entered: boolean
  /** Empty string when entered. Otherwise a precise, non-sensitive reason. */
  reason: FullscreenFailureReason | ''
  /** Raw DOMException name, when one was thrown. Never contains page data. */
  errorName: string
}

// ---------------------------------------------------------------------------
// Runtime diagnostics
// ---------------------------------------------------------------------------
// A short ring buffer of the fullscreen transition sequence. Contains only
// browser capability facts — never exam content, identity, or answer data.
// It exists so a real-device failure can be diagnosed from the console instead
// of guessed at from source. `getFullscreenDiagnostics()` is what you print
// when reporting that fullscreen did not activate.

export interface FullscreenDiagEntry {
  t: number
  event: string
  detail: string
}

const DIAG_MAX = 40
const diag: FullscreenDiagEntry[] = []

function record(event: string, detail = ''): void {
  diag.push({ t: Math.round(performance.now()), event, detail })
  if (diag.length > DIAG_MAX) diag.shift()
  // Always visible: a silent fullscreen failure is the single most expensive
  // bug to diagnose remotely, and this line contains no exam data.
  console.info(`[QuizEazz][fullscreen] ${event}${detail ? ` — ${detail}` : ''}`)
}

export function getFullscreenDiagnostics(): readonly FullscreenDiagEntry[] {
  return diag
}

export function clearFullscreenDiagnostics(): void {
  diag.length = 0
}

// ---------------------------------------------------------------------------

export function isFullscreenSupported(): boolean {
  if (typeof document === 'undefined') return false
  const el = document.documentElement as FullscreenElement
  return (
    typeof el.requestFullscreen === 'function' ||
    typeof el.webkitRequestFullscreen === 'function'
  )
}

/** The single source of truth. Never infer fullscreen from anything else. */
export function isFullscreenActive(): boolean {
  if (typeof document === 'undefined') return false
  const doc = document as FullscreenDocument
  return !!(doc.fullscreenElement ?? doc.webkitFullscreenElement ?? null)
}

function classify(err: unknown): FullscreenFailureReason {
  const name = (err as { name?: string } | null)?.name ?? ''
  switch (name) {
    case 'NotAllowedError':
      // Chrome's answer to "no transient user activation", and also to a
      // permission-policy or iframe restriction.
      return 'no-user-activation'
    case 'TypeError':
      return 'missing-element'
    case 'AbortError':
      return 'denied-by-browser'
    case 'NotSupportedError':
      return 'unsupported'
    default:
      return 'unknown-error'
  }
}

const MESSAGES: Record<FullscreenFailureReason, string> = {
  unsupported: 'This browser does not support the Fullscreen API.',
  'no-user-activation':
    'Your browser requires Fullscreen to be requested from a direct click and refused the request. Click the button again while the page is in the foreground.',
  'denied-by-browser': 'Your browser refused the Fullscreen request.',
  'missing-element': 'The page element could not be made fullscreen.',
  'unknown-error': 'The Fullscreen request failed for an unknown reason.',
}

/**
 * Requests fullscreen on `document.documentElement`.
 *
 * MUST be invoked synchronously from a real user gesture (a click or key
 * press). Never call this from an effect, a timer, or after an `await`.
 *
 * Resolves `entered: true` ONLY when `document.fullscreenElement` is confirmed
 * non-null afterwards.
 */
export async function requestFullscreen(): Promise<FullscreenOutcome> {
  const startedAt = Math.round(performance.now())
  record(
    'request:start',
    `supported=${isFullscreenSupported()} alreadyActive=${isFullscreenActive()} userActivation=${readActivation()}`,
  )

  const el = typeof document !== 'undefined' ? (document.documentElement as FullscreenElement) : null
  if (!el) {
    record('request:fail', 'no documentElement')
    return { entered: false, reason: 'missing-element', errorName: '' }
  }

  const native =
    typeof el.requestFullscreen === 'function'
      ? el.requestFullscreen.bind(el)
      : typeof el.webkitRequestFullscreen === 'function'
        ? el.webkitRequestFullscreen.bind(el)
        : null

  if (!native) {
    record('request:fail', 'unsupported')
    return { entered: false, reason: 'unsupported', errorName: '' }
  }

  try {
    await native()
  } catch (err) {
    const reason = classify(err)
    const errorName = (err as { name?: string } | null)?.name ?? 'Error'
    record('request:rejected', `${errorName} → ${reason}`)
    return { entered: false, reason, errorName }
  }

  // The promise resolved, but Chrome can resolve it before the transition has
  // been applied to the document. Confirm against the document, never against
  // the promise.
  const confirmed = isFullscreenActive()
  record(
    confirmed ? 'request:confirmed' : 'request:resolved-but-not-active',
    `fullscreenElement=${describeElement()} (+${Math.round(performance.now()) - startedAt}ms)`,
  )

  return confirmed
    ? { entered: true, reason: '', errorName: '' }
    : { entered: false, reason: 'denied-by-browser', errorName: 'NoElementAfterResolve' }
}

export async function exitFullscreen(): Promise<boolean> {
  if (typeof document === 'undefined') return false
  const doc = document as FullscreenDocument
  if (!isFullscreenActive()) return false

  try {
    if (typeof doc.exitFullscreen === 'function') await doc.exitFullscreen()
    else if (typeof doc.webkitExitFullscreen === 'function') await doc.webkitExitFullscreen()
    else return false
    record('exit:confirmed', `fullscreenElement=${describeElement()}`)
    return !isFullscreenActive()
  } catch (err) {
    record('exit:failed', (err as { name?: string } | null)?.name ?? 'Error')
    return false
  }
}

/** Fires on entering AND leaving fullscreen. */
export function onFullscreenChange(handler: (active: boolean) => void): () => void {
  if (typeof document === 'undefined') return () => undefined
  const listener = () => {
    const active = isFullscreenActive()
    record(`event:${active ? 'entered' : 'exited'}`, `fullscreenElement=${describeElement()}`)
    handler(active)
  }
  document.addEventListener('fullscreenchange', listener)
  document.addEventListener('webkitfullscreenchange', listener)
  return () => {
    document.removeEventListener('fullscreenchange', listener)
    document.removeEventListener('webkitfullscreenchange', listener)
  }
}

export function fullscreenFailureMessage(reason: FullscreenFailureReason): string {
  return MESSAGES[reason] ?? MESSAGES['unknown-error']
}

/**
 * One-call proof of what the browser is actually doing right now.
 *
 * Paste this into the DevTools console while an exam is running:
 *
 *   document.fullscreenElement
 *   navigator.userActivation?.isActive
 *
 * or, with the dev server running:
 *
 *   (await import('/src/lib/fullscreen.ts')).fullscreenReport()
 *
 * It reports observed state only. It never asserts that fullscreen "should"
 * be active, and it contains no exam content, identity or answer data.
 */
export function fullscreenReport(): Record<string, unknown> {
  const nav = typeof navigator !== 'undefined' ? navigator : null
  return {
    isFullscreenSupported: isFullscreenSupported(),
    // The single authoritative signal. If this is null the browser is NOT in
    // fullscreen, whatever the page layout looks like.
    fullscreenElement: describeElement(),
    documentFullscreenElement:
      typeof document !== 'undefined' ? (document.fullscreenElement?.tagName ?? null) : null,
    documentHasFocus: typeof document !== 'undefined' ? document.hasFocus() : null,
    documentHidden: typeof document !== 'undefined' ? document.hidden : null,
    visibilityState: typeof document !== 'undefined' ? document.visibilityState : null,
    userActivationIsActive: nav?.userActivation?.isActive ?? null,
    userActivationHasBeenActive: nav?.userActivation?.hasBeenActive ?? null,
    recentEvents: diag.slice(-12),
  }
}

// --------------------------------------------------------------- internals

function describeElement(): string {
  if (typeof document === 'undefined') return 'n/a'
  const doc = document as FullscreenDocument
  const el = doc.fullscreenElement ?? doc.webkitFullscreenElement ?? null
  if (!el) return 'null'
  return `<${el.tagName.toLowerCase()}>`
}

/**
 * Transient user activation, when the browser exposes it. Purely diagnostic —
 * used only to explain a rejection, never to gate behaviour.
 */
function readActivation(): string {
  const nav = typeof navigator !== 'undefined' ? (navigator as Navigator & {
    userActivation?: { isActive: boolean; hasBeenActive: boolean }
  }) : null
  if (!nav?.userActivation) return 'n/a'
  return `active=${nav.userActivation.isActive}`
}
