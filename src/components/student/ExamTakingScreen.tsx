import React, { useState, useEffect, useRef, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import Editor from '@monaco-editor/react'
import {
  Clock,
  Flag,
  ChevronLeft,
  ChevronRight,
  Code2,
  Check,
  PauseCircle,
  AlertCircle,
  CloudOff,
  MonitorCheck,
  Maximize2,
} from 'lucide-react'
import { useAuth } from '../../context/AuthContext'
import type { Question, ExamAttempt } from '../../types'
import {
  buildCandidatePaper,
  fetchExamQuestions,
  fetchStudentSavedAnswers,
  logSecurityEvent,
  pauseExamSession,
  persistAnswer,
  resumeExamSession,
  startOrGetExamSession,
  submitExamAttempt,
  toErrorMessage,
} from '../../lib/examEngine'
import { QUIZEAZZ_CPP_LANGUAGE_ID } from '../../lib/languages'
import {
  isFullscreenActive,
  isFullscreenSupported,
  onFullscreenChange,
  requestFullscreen,
  exitFullscreen,
  fullscreenFailureMessage,
  type FullscreenFailureReason,
} from '../../lib/fullscreen'
import { requestProgrammingGrading } from '../../lib/judge0Client'
import { supabase } from '../../lib/supabase'

const AUTOSAVE_DEBOUNCE_MS = 700

type SaveState = 'idle' | 'saving' | 'saved' | 'failed'

/**
 * Exam lifecycle phases.
 *
 * `gate` is a real, deliberate state: the session data is loaded but the
 * candidate has not yet entered browser fullscreen, so the paper is not yet
 * being taken. Fullscreen can only be requested from a click, and the session
 * data arrives after three awaited RPCs, so the click cannot happen earlier
 * than this point.
 */
type Phase = 'loading' | 'gate' | 'active'

/**
 * How long a `window.blur` may be ignored after WE request fullscreen.
 *
 * Entering fullscreen can move OS focus, which raises a self-inflicted blur
 * that must not pause the exam. This is a timestamp, never a boolean, so the
 * window always expires even if the transition never reports back.
 *
 * 700ms is deliberately short: it covers the queued fullscreen transition and
 * nothing more. Once it lapses, a genuine Alt+Tab / app switch / desktop
 * switch pauses the exam exactly as before. Note that `visibilitychange` is
 * NEVER suppressed, so tab switching and window minimising cannot be masked by
 * this window.
 */
const TRANSITION_BLUR_GRACE_MS = 700

/**
 * QuizEazz V1 evaluates every programming answer as C++ (Judge0 54).
 * There is no language selector: the editor, the persisted answer language
 * and the grader all use this single canonical identifier.
 */
const CODE_LANGUAGE = QUIZEAZZ_CPP_LANGUAGE_ID


export const ExamTakingScreen: React.FC = () => {
  const { id: examId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()

  const [examTitle, setExamTitle] = useState('Examination')
  const [attempt, setAttempt] = useState<ExamAttempt | null>(null)
  const [durationMinutes, setDurationMinutes] = useState(60)
  const [questions, setQuestions] = useState<Question[]>([])
  const [loading, setLoading] = useState(true)
  const [initError, setInitError] = useState<string | null>(null)

  const [currentIndex, setCurrentIndex] = useState(0)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [markedForReview, setMarkedForReview] = useState<Record<string, boolean>>({})
  // No language state: every programming answer is C++. See CODE_LANGUAGE.
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)

  const [timeLeft, setTimeLeft] = useState(0)
  const [isTimeExpired, setIsTimeExpired] = useState(false)

  const [isPaused, setIsPaused] = useState(false)
  const [pausedAt, setPausedAt] = useState<string | null>(null)
  const [pauseReason, setPauseReason] = useState('Examination window lost focus.')
  const [isResuming, setIsResuming] = useState(false)

  const [showSubmitModal, setShowSubmitModal] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  // ---- Fullscreen / phase state -------------------------------------------
  // Fullscreen is not a security boundary by itself (see src/lib/fullscreen.ts)
  // but the workspace requires it to be genuinely active before the candidate
  // begins, and leaving it mid-exam is treated as a focus loss.
  const [phase, setPhase] = useState<Phase>('loading')
  const [isFs, setIsFs] = useState(() => isFullscreenActive())
  const [fsSupported] = useState(() => isFullscreenSupported())
  const [fsReason, setFsReason] = useState<FullscreenFailureReason | ''>('')
  const [isEnteringFs, setIsEnteringFs] = useState(false)
  const [isResumed, setIsResumed] = useState(false)

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingRef = useRef<{ questionId: string; content: string } | null>(null)
  const submittingRef = useRef(false)
  // True once the candidate has actually begun AND fullscreen was confirmed.
  // Gates the fullscreen_exit event so it can only fire for a live exam.
  const attemptLiveRef = useRef(false)
  // Timestamp, never a boolean: a stuck boolean here would silently disable
  // pause-on-focus-loss forever.
  const blurGraceUntilRef = useRef(0)
  // Why the workspace is being torn down. Set synchronously BEFORE any
  // application-initiated fullscreen exit so cleanup is never mistaken for the
  // candidate leaving fullscreen.
  const teardownRef = useRef<'none' | 'submitting' | 'unmount'>('none')
  // Latest `pauseFor`, published by the focus-listener effect so the
  // fullscreen-transition effect can trigger the identical pause path without
  // re-subscribing listeners on every question change.
  const pauseRef = useRef<
    (reason: string, type: 'window_blur' | 'tab_switch' | 'fullscreen_exit') => void
  >(() => undefined)
  // Same-tick de-duplication: one pause + one event per incident, even if both
  // `blur` and `fullscreenchange` arrive from a single Escape press.
  const isPausedRef = useRef(false)
  // Absolute server-authoritative deadline (started_at + duration). Pausing,
  // closing or reopening the exam MUST NOT move this: remaining time is always
  // deadline minus now. The server enforces the same deadline independently.
  const deadlineRef = useRef(0)
  // Throttle for copy/cut/paste violation logging (ms between logged events).
  const lastClipboardEventRef = useRef(0)

  // ---------------------------------------------------------------
  // 1. Initialize: authoritative attempt + candidate-safe paper
  // ---------------------------------------------------------------
  useEffect(() => {
    if (!user || !examId) return
    let cancelled = false

    const initialize = async () => {
      setLoading(true)
      setInitError(null)

      try {
        const session = await startOrGetExamSession(examId)
        if (cancelled) return

        const { exam, attempt: activeAttempt } = session

        if (
          activeAttempt.status !== 'in_progress' &&
          activeAttempt.status !== undefined
        ) {
          navigate(`/student/review/${activeAttempt.id}`, { replace: true })
          return
        }

        const resolvedDuration = exam.duration_minutes || 60
        setExamTitle(exam.title || 'Examination')
        setDurationMinutes(resolvedDuration)
        setAttempt(activeAttempt)

        if (activeAttempt.is_paused) {
          setIsPaused(true)
          setPausedAt(
            activeAttempt.paused_at
              ? new Date(activeAttempt.paused_at).toLocaleTimeString()
              : null,
          )
          setPauseReason(activeAttempt.pause_reason || 'Examination paused')
        }

        const endsAt =
          new Date(activeAttempt.started_at).getTime() + resolvedDuration * 60 * 1000
        deadlineRef.current = endsAt
        setTimeLeft(Math.max(0, Math.floor((endsAt - Date.now()) / 1000)))
        if (endsAt - Date.now() <= 0) {
          // Reopened after the deadline with a still-open attempt: expire
          // immediately so the auto-submit effect finalizes it. The server
          // marks it timed_out; no extra time is granted.
          setIsTimeExpired(true)
        }

        // Distinguishes a genuinely fresh attempt from a resumed one, so the
        // gate never tells a candidate who is already mid-paper that they are
        // "starting". The server attempt and its clock are unaffected either way.
        setIsResumed(Date.now() - new Date(activeAttempt.started_at).getTime() > 5000)

        const rawQuestions = await fetchExamQuestions(examId)
        if (cancelled) return

        if (rawQuestions.length === 0) {
          throw new Error(
            'This examination has no questions assigned yet. Contact your faculty member.',
          )
        }

        // The paper is fixed by faculty. Only order is randomized, and the
        // seed is derived from (studentId, examId) so reloads are stable.
        setQuestions(buildCandidatePaper(rawQuestions, user.id, examId))

        const restored = await fetchStudentSavedAnswers(activeAttempt.id)
        if (!cancelled && restored) setAnswers(restored)

        // The paper is ready, but the exam has NOT begun. There is no user
        // activation here — this code ran after three awaited RPCs, so any
        // requestFullscreen() call from this point is refused by Chrome with
        // NotAllowedError. Attempting it anyway (as a previous revision did)
        // can only ever fail, and swallowed the failure behind a cosmetic
        // toggle. We stop at the gate instead and let a real click decide.
        if (!cancelled) setPhase('gate')
      } catch (err) {
        if (!cancelled) {
          setInitError(toErrorMessage(err, 'Unable to load the examination session.'))
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void initialize()
    return () => {
      cancelled = true
    }
  }, [user, examId, navigate])

  // ---------------------------------------------------------------
  // 2. Absolute countdown from the server-authoritative deadline
  // ---------------------------------------------------------------
  // Starts only once the candidate has genuinely begun, i.e. fullscreen was
  // confirmed. Every tick recomputes remaining time as deadline minus now,
  // so pausing, closing or reopening the exam can never freeze, extend or
  // reset the clock. The interval keeps running while paused: reaching zero
  // auto-submits even mid-pause. The server enforces the same deadline
  // independently, so this countdown is a display of an unmovable fact.
  useEffect(() => {
    if (phase !== 'active' || deadlineRef.current <= 0) return
    const id = setInterval(() => {
      const remainingMs = deadlineRef.current - Date.now()
      if (remainingMs <= 0) {
        clearInterval(id)
        setTimeLeft(0)
        setIsTimeExpired(true)
        return
      }
      setTimeLeft(Math.floor(remainingMs / 1000))
    }, 1000)
    return () => clearInterval(id)
  }, [phase])

  // ---------------------------------------------------------------
  // 3. Autosave
  // ---------------------------------------------------------------
  const flushPendingSave = useCallback(async (ignoreLock = false) => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current)
      debounceRef.current = null
    }
    const pending = pendingRef.current
    // The submission lock refuses flushes, so the submit path must flush
    // BEFORE locking (and re-drain after locking, see performSubmit).
    // The debounce timer always calls with the default: once submission has
    // begun, nothing more may be written.
    if (!pending || !attempt || isPaused || (!ignoreLock && submittingRef.current)) return

    pendingRef.current = null
    setSaveState('saving')

    try {
      const outcome = await persistAnswer({
        attemptId: attempt.id,
        questionId: pending.questionId,
        content: pending.content,
        language: CODE_LANGUAGE,
        isPaused,
        startedAt: attempt.started_at,
        durationMinutes,
      })

      if (outcome === 'saved') {
        setSaveState('saved')
        setSaveError(null)
      } else {
        setSaveState('failed')
        setSaveError(
          'The server rejected this change because the examination is no longer accepting answers.',
        )
      }
    } catch (err) {
      setSaveState('failed')
      setSaveError(toErrorMessage(err, 'The answer could not be saved to the server.'))
    }
  }, [attempt, durationMinutes, isPaused])

  const scheduleSave = useCallback(
    (questionId: string, content: string) => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
      pendingRef.current = { questionId, content }
      setSaveState('saving')
      debounceRef.current = setTimeout(() => {
        void flushPendingSave()
      }, AUTOSAVE_DEBOUNCE_MS)
    },
    [flushPendingSave],
  )

  // Never leave an unsaved edit behind when the workspace unmounts.
  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
      const pending = pendingRef.current
      if (!pending || !attempt) return
      // Fire-and-forget last-chance save. The DB remains the authority; if it
      // fails the candidate is told via the next failed save indicator.
      void persistAnswer({
        attemptId: attempt.id,
        questionId: pending.questionId,
        content: pending.content,
        language: CODE_LANGUAGE,
      }).catch(() => undefined)
    }
  }, [attempt])

  // ---------------------------------------------------------------
  // 4. Security events: window blur + tab hidden + fullscreen exit
  // ---------------------------------------------------------------
  // KNOWN LIMITATION — READ THIS BEFORE ADDING "IMPROVEMENTS".
  //
  // These signals are best-effort and are defeatable by design. Extensions
  // such as "Always Active Window" hold the page focused/focused-visible so
  // that neither `blur` nor `visibilitychange` fires while the candidate
  // copies content elsewhere. There is no in-page JavaScript fix for that:
  // an extension with host permissions can suppress or spoof every event the
  // page can observe.
  //
  // We deliberately do NOT add polling loops, focus-stealing hacks,
  // re-focusing timers, or synthetic event checks to "catch" this. They
  // would be cosmetic, would cause real harm (fighting the student's own
  // window management, spurious pauses, reduced accessibility) and would
  // still not be reliable. Preventing this class of behaviour requires a
  // controlled endpoint: Edge kiosk mode or Windows Assigned Access on a
  // dedicated, non-administrator exam account, with extensions disabled by
  // policy — plus a physical invigilator. See docs/SECURITY_MODEL.md.
  //
  // What we do keep: objective, timestamped facts recorded server-side for
  // invigilator review. No automatic accusation, no automatic penalty.
  useEffect(() => {
    if (phase !== 'active' || isSubmitting || isPaused || !attempt || !examId) return

    const currentQuestionId = questions[currentIndex]?.id

    /**
     * Single place that pauses the attempt. Always records a timestamped,
     * objective security event through the existing RPC; never auto-submits,
     * never flags the candidate as a violator, never applies a penalty.
     */
    const pauseFor = (reason: string, type: 'window_blur' | 'tab_switch' | 'fullscreen_exit') => {
      // The transition grace applies ONLY to window_blur, and ONLY inside a
      // short bounded window armed by our own fullscreen request. It can never
      // mask a genuine event once that window has passed, and it never applies
      // to visibilitychange — so tab switching and minimising always pause.
      if (type === 'window_blur' && Date.now() < blurGraceUntilRef.current) {
        console.info('[QuizEazz][fullscreen] ignored transition blur')
        return
      }
      if (isPausedRef.current) return

      console.info(`[QuizEazz] pausing attempt — ${type}: ${reason}`)
      isPausedRef.current = true
      setPauseReason(reason)
      setPausedAt(new Date().toLocaleTimeString())
      setIsPaused(true)
      void pauseExamSession(attempt.id, reason).catch((err) =>
        console.warn('[QuizEazz] pause not recorded:', toErrorMessage(err, 'unknown')),
      )
      void logSecurityEvent(attempt.id, examId, type, currentQuestionId, reason)
    }

    // Published so the fullscreen-transition handler uses this exact path.
    pauseRef.current = pauseFor

    // Windows Alt+Tab, app switching and virtual-desktop switching are consumed
    // by the OS before any page code runs. JavaScript cannot intercept or
    // prevent them. It can only observe the resulting focus loss, which is
    // exactly what this listener does.
    const onBlur = () => pauseFor('Exam window lost focus.', 'window_blur')
    const onVisibility = () => {
      if (document.hidden) pauseFor('Browser tab or application switched.', 'tab_switch')
    }

    window.addEventListener('blur', onBlur)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.removeEventListener('blur', onBlur)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [phase, isSubmitting, isPaused, attempt, examId, questions, currentIndex])

  // ---------------------------------------------------------------
  // 4b. Fullscreen transitions — the security-relevant state machine
  // ---------------------------------------------------------------
  //
  //   entering fullscreen            -> no pause, clear any failure notice
  //   exiting while the exam is live -> pause + log fullscreen_exit + modal
  //   exiting during app cleanup     -> no pause, no event
  //
  // A single listener is used for both directions so the two cannot disagree.
  useEffect(() => {
    return onFullscreenChange((active) => {
      setIsFs(active)

      if (active) {
        setFsReason('')
        return
      }

      // --- leaving fullscreen -------------------------------------------
      if (teardownRef.current !== 'none') {
        console.info(
          `[QuizEazz][fullscreen] exit ignored — application teardown (${teardownRef.current})`,
        )
        return
      }
      if (!attemptLiveRef.current || !isFullscreenSupported()) {
        // The gate was showing, or the exam never began. Not a security event.
        console.info('[QuizEazz][fullscreen] exit before exam began — ignored')
        setPhase((p) => (p === 'active' ? 'gate' : p))
        return
      }

      // A live exam left fullscreen. This is a real focus loss: the candidate
      // pressed Escape, F11, or used the browser's own exit control.
      console.info('[QuizEazz][fullscreen] exit during live exam — pausing')
      pauseRef.current('Browser fullscreen was exited.', 'fullscreen_exit')
    })
  }, [pauseRef])

  const handleSelectOption = (questionId: string, optionId: string) => {
    if (isPaused || submittingRef.current) return
    setAnswers((prev) => ({ ...prev, [questionId]: optionId }))
    scheduleSave(questionId, optionId)
  }

  const handleCodeChange = (value: string | undefined) => {
    const question = questions[currentIndex]
    if (!question || isPaused || submittingRef.current) return
    const code = value ?? ''
    setAnswers((prev) => ({ ...prev, [question.id]: code }))
    scheduleSave(question.id, code)
  }

  const toggleMarkForReview = () => {
    const question = questions[currentIndex]
    if (!question) return
    setMarkedForReview((prev) => ({ ...prev, [question.id]: !prev[question.id] }))
  }

  // ---------------------------------------------------------------
  // 5. Begin / Resume — both require genuinely-confirmed fullscreen
  // ---------------------------------------------------------------
  /**
   * THE fullscreen entry point. Invoked directly from a click handler so the
   * browser's transient user activation is still live; there is no await, no
   * state update, no navigation and no timer between the click and the
   * request. `requestFullscreen()` reports success ONLY after
   * `document.fullscreenElement` has been confirmed non-null.
   *
   * If the browser refuses, the exam does not begin and the reason is shown.
   * There is no "continue anyway" path and there is no Exit Fullscreen control:
   * the workspace has exactly one direction, into fullscreen.
   */
  const enterFullscreenAndBegin = useCallback(async () => {
    if (isEnteringFs) return
    setIsEnteringFs(true)

    // Arm the transition grace BEFORE requesting, because entering fullscreen
    // can itself raise a self-inflicted window blur. Bounded to 700ms and
    // never applied to visibilitychange.
    blurGraceUntilRef.current = Date.now() + TRANSITION_BLUR_GRACE_MS

    const outcome = await requestFullscreen()
    const confirmed = outcome.entered && isFullscreenActive()

    setIsFs(confirmed)
    if (!confirmed) {
      setFsReason(outcome.reason || 'unknown-error')
      setIsEnteringFs(false)
      return
    }

    setFsReason('')
    teardownRef.current = 'none'
    attemptLiveRef.current = true
    setPhase('active')
    setIsEnteringFs(false)
  }, [isEnteringFs])

  /**
   * Resume. Fullscreen must be genuinely re-entered first — leaving
   * fullscreen pauses the exam, so resuming without it would make the
   * pause meaningless. If the browser refuses, the candidate stays paused
   * with an explicit retry, which is the opposite of an exit hatch.
   */
  const handleResume = async () => {
    if (!attempt || isResuming || isEnteringFs) return

    if (!isFullscreenActive()) {
      setIsEnteringFs(true)
      blurGraceUntilRef.current = Date.now() + TRANSITION_BLUR_GRACE_MS
      // Fullscreen is required before resuming. If the browser refuses, the
      // candidate stays paused with the reason shown. This is deliberately not
      // an escape hatch: there is no "resume without fullscreen".
      const outcome = await requestFullscreen()
      setIsEnteringFs(false)

      if (!outcome.entered || !isFullscreenActive()) {
        setFsReason(outcome.reason || 'unknown-error')
        setIsFs(false)
        return
      }
      setFsReason('')
      setIsFs(true)
    }

    setIsResuming(true)
    try {
      await resumeExamSession(attempt.id)
      isPausedRef.current = false
      setIsPaused(false)
      setPausedAt(null)
      setSaveError(null)
    } catch (err) {
      setSaveError(toErrorMessage(err, 'Unable to resume the examination.'))
    } finally {
      setIsResuming(false)
    }
  }

  // ---------------------------------------------------------------
  // 6. Final submission (single authoritative round trip)
  // ---------------------------------------------------------------
  const performSubmit = useCallback(async () => {
    if (!attempt || submittingRef.current) return

    // Flush FIRST, before the lock: a flush refuses to run once the lock is
    // set, so locking first would silently discard any keystroke still inside
    // the autosave debounce window. This covers both manual Submit and the
    // timer-expiry path, which share this function.
    await flushPendingSave()

    // A concurrent submit may have taken the lock during the flush await.
    // Single-flight is preserved: the loser returns without submitting.
    if (!attempt || submittingRef.current) return

    submittingRef.current = true
    setIsSubmitting(true)
    setSubmitError(null)

    try {
      // Drain anything typed during the flush await. Edit handlers are now
      // blocked by the lock, so pendingRef is stable: after this drain it is
      // guaranteed empty and stays empty. A rejected drain never blocks the
      // submit itself — the server remains authoritative over the deadline.
      await flushPendingSave(true)

      const result = await submitExamAttempt(attempt.id)

      // The attempt is frozen by the server at this point. Only NOW may code
      // be executed. This asks the server to run the programming answers
      // through Judge0; the browser holds no Judge0 token and computes no
      // marks. If the request cannot be made the answers simply stay
      // 'Pending Evaluation' - a failure is never recorded as a verdict.
      //
      // The trigger is AWAITED before navigating on purpose. A fire-and-forget
      // fetch issued in the same tick as a route change can be dropped by the
      // browser before it is ever sent, which leaves zero Edge Function
      // invocations and the attempt stuck at 'Pending Evaluation' forever.
      // Awaiting guarantees the POST has been dispatched (or has observably
      // failed) before the review page loads. Navigation still always happens:
      // a trigger failure is logged and never blocks the student.
      try {
        const gradeTrigger = await requestProgrammingGrading(supabase, attempt.id)
        if (!gradeTrigger.ok) {
          console.warn(
            '[QuizEazz] programming grading trigger failed:',
            gradeTrigger.error ?? 'unknown error',
          )
        }
      } catch (err) {
        console.warn(
          '[QuizEazz] programming grading trigger threw:',
          toErrorMessage(err, 'unknown error'),
        )
      }

      // The attempt is frozen. Leaving fullscreen is now application cleanup,
      // NOT a candidate action, so it must never raise a security event or a
      // pause. Set before exiting and before navigating, and never reset.
      teardownRef.current = 'submitting'
      if (isFullscreenActive()) void exitFullscreen()

      navigate(
        result.timedOut
          ? `/student/review/${attempt.id}`
          : `/student/review/${attempt.id}`,
        { replace: true },
      )
    } catch (err) {
      const message = toErrorMessage(err, 'Unable to submit the examination.')
      setSubmitError(message)
      setShowSubmitModal(true)
    } finally {
      submittingRef.current = false
      setIsSubmitting(false)
    }
  }, [attempt, flushPendingSave, navigate])

  useEffect(() => {
    if (isTimeExpired && attempt && !submittingRef.current) void performSubmit()
  }, [isTimeExpired, attempt, performSubmit])

  // Clipboard guard: outside Monaco, copy/cut/paste are blocked and logged
  // for invigilator review. The code editor itself is exempt so candidates
  // can always edit code. This never pauses and never touches the timer:
  // the absolute deadline keeps running regardless.
  const handleClipboardGuard =
    (action: 'copy' | 'cut' | 'paste') => (e: React.ClipboardEvent) => {
      const target = e.target as HTMLElement | null
      if (target?.closest?.('.monaco-editor')) return
      e.preventDefault()
      const now = Date.now()
      if (now - lastClipboardEventRef.current < 5000) return
      lastClipboardEventRef.current = now
      if (!attempt || !examId) return
      const qid = questions[currentIndex]?.id
      void logSecurityEvent(
        attempt.id,
        examId,
        `${action}_attempt` as 'copy_attempt',
        qid,
        `Clipboard ${action} blocked outside the code editor.`,
      ).catch(() => undefined)
    }

  // Application-initiated teardown on unmount (navigation away, refresh, the
  // post-submit route change). Marked as cleanup so the resulting exit is never
  // reported as the candidate leaving fullscreen. Flag is set BEFORE the exit.
  useEffect(() => {
    return () => {
      teardownRef.current = 'unmount'
      if (isFullscreenActive()) void exitFullscreen()
    }
  }, [])

  const formatTimer = (secs: number) => {
    const h = Math.floor(secs / 3600)
    const m = Math.floor((secs % 3600) / 60)
    const s = secs % 60
    return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s
      .toString()
      .padStart(2, '0')}`
  }

  // ---------------------------------------------------------------
  // Renders
  // ---------------------------------------------------------------
  if (initError) {
    return (
      <div className="h-screen w-screen flex flex-col items-center justify-center bg-slate-50 p-6 text-center">
        <div className="h-12 w-12 rounded-full bg-rose-100 text-rose-600 flex items-center justify-center mb-4">
          <AlertCircle className="h-6 w-6" />
        </div>
        <h2 className="text-base font-bold text-slate-800 mb-1">Unable to Start Examination</h2>
        <p className="text-sm text-slate-600 max-w-md mb-6">{initError}</p>
        <button
          onClick={() => navigate('/student/exams', { replace: true })}
          className="px-4 py-2 rounded-xl text-sm font-semibold bg-indigo-600 text-white hover:bg-indigo-700 transition-colors"
        >
          Return to My Exams
        </button>
      </div>
    )
  }

  if (loading || phase === 'loading') {
    return (
      <div className="h-screen w-screen flex flex-col items-center justify-center bg-slate-50">
        <div className="w-8 h-8 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin" />
        <p className="mt-3 text-sm font-semibold text-slate-600">Loading Examination Session...</p>
      </div>
    )
  }

  // ---------------------------------------------------------------
  // GATE — the paper is loaded, the exam has NOT started.
  //
  // This is where fullscreen is requested, directly from a click. It cannot be
  // requested earlier: the session needs three awaited RPCs to load, and Chrome
  // refuses a request without live user activation. It cannot be requested
  // automatically later for the same reason.
  //
  // The candidate cannot skip this. There is no "continue without fullscreen".
  // ---------------------------------------------------------------
  if (phase === 'gate') {
    const blocked = fsReason !== ''

    return (
      <div className="h-screen w-screen flex items-center justify-center bg-slate-50 p-6">
        <div className="w-full max-w-lg rounded-2xl border border-slate-200 bg-white p-7 shadow-2xl space-y-5">
          <div className="flex items-start gap-3.5">
            <div className="h-11 w-11 rounded-xl bg-indigo-50 text-indigo-600 flex items-center justify-center shrink-0">
              <MonitorCheck className="h-6 w-6" />
            </div>
            <div>
              <h2 className="text-base font-bold text-slate-900 tracking-tight">
                {attempt?.is_paused || isResumed
                  ? 'Continue your examination'
                  : 'Ready to begin?'}
              </h2>
              <p className="text-sm text-slate-600 mt-1">
                {examTitle} · {questions.length} question{questions.length === 1 ? '' : 's'} ·{' '}
                {questions.reduce((sum, q) => sum + (q.marks ?? 0), 0)} marks ·{' '}
                {durationMinutes} minutes
              </p>
            </div>
          </div>

          <ul className="space-y-2 text-sm text-slate-700">
            <li className="flex items-start gap-2">
              <Check className="h-4 w-4 text-emerald-600 shrink-0 mt-0.5" />
              <span>
                The examination runs in <strong>browser fullscreen</strong>, so the tabs,
                address bar and browser toolbars are hidden.
              </span>
            </li>
            <li className="flex items-start gap-2">
              <PauseCircle className="h-4 w-4 text-amber-600 shrink-0 mt-0.5" />
              <span>
                Leaving fullscreen, switching tabs or applications, or changing Windows desktop
                <strong> pauses the exam</strong> and records a timestamped event. You must resume
                manually.
              </span>
            </li>
            <li className="flex items-start gap-2">
              <CloudOff className="h-4 w-4 text-slate-400 shrink-0 mt-0.5" />
              <span>Your answers save automatically as you work.</span>
            </li>
          </ul>

          {!fsSupported ? (
            <div className="rounded-lg border border-rose-200 bg-rose-50 p-3.5 text-sm text-rose-800">
              <p className="font-semibold">This browser cannot run the examination</p>
              <p className="mt-1 leading-relaxed">
                Browser fullscreen is required and is not available here. Use a current version of
                Chrome, Edge or Firefox on a desktop computer. Contact your faculty member if
                this persists.
              </p>
            </div>
          ) : blocked ? (
            <div className="rounded-lg border border-rose-200 bg-rose-50 p-3.5 text-sm text-rose-800">
              <p className="font-semibold">Fullscreen was not activated</p>
              <p className="mt-1 leading-relaxed">{fullscreenFailureMessage(fsReason)}</p>
              <p className="mt-1.5 text-xs text-rose-700">
                The examination has not started. Click below to try again — the browser only
                allows this request from a direct click.
              </p>
            </div>
          ) : null}

          <button
            type="button"
            onClick={() => void enterFullscreenAndBegin()}
            disabled={!fsSupported || isEnteringFs}
            className="w-full inline-flex items-center justify-center gap-2 py-3 px-4 rounded-xl bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Maximize2 className="h-4 w-4" />
            <span>
              {isEnteringFs
                ? 'Entering fullscreen...'
                : blocked
                  ? 'Retry Fullscreen'
                  : attempt?.is_paused || isResumed
                    ? 'Enter Fullscreen & Continue Exam'
                    : 'Enter Fullscreen & Start Exam'}
            </span>
          </button>

          <p className="text-xs text-slate-500 text-center">
            There is no way to continue without fullscreen. Leaving fullscreen during the
            examination pauses it.
          </p>
        </div>
      </div>
    )
  }

  const currentQuestion = questions[currentIndex]
  if (!currentQuestion) {
    return (
      <div className="h-screen w-screen flex flex-col items-center justify-center bg-slate-50">
        <AlertCircle className="h-6 w-6 text-rose-500 mb-2" />
        <p className="text-sm text-slate-600">No question is currently displayed.</p>
      </div>
    )
  }

  const currentAnswer = answers[currentQuestion.id] ?? ''
  const isCurrentMarked = !!markedForReview[currentQuestion.id]
  const answeredCount = questions.filter((q) => !!answers[q.id]?.trim()).length
  const unansweredCount = questions.length - answeredCount
  const markedCount = Object.values(markedForReview).filter(Boolean).length

  return (
    <div
      className="h-screen w-screen overflow-hidden flex flex-col bg-slate-50 font-sans antialiased text-slate-900 select-none"
      onCopy={handleClipboardGuard('copy')}
      onCut={handleClipboardGuard('cut')}
      onPaste={handleClipboardGuard('paste')}
    >
      {/* TOP BAR — timer and submit stay visible at every width. */}
      <header className="w-full border-b border-slate-200 bg-white px-3 sm:px-6 py-2 flex items-center justify-between gap-2 flex-wrap shrink-0">
        <div className="flex items-center gap-2 sm:gap-3 min-w-0">
          <span className="hidden min-[420px]:inline text-base font-bold text-slate-900 tracking-tight shrink-0">
            Quiz<span className="text-indigo-600">Eazz</span>
          </span>
          <span className="hidden min-[420px]:inline text-slate-300 shrink-0">|</span>
          <h1 className="text-xs sm:text-sm font-semibold text-slate-700 truncate">{examTitle}</h1>
        </div>

        <div className="flex items-center gap-2 sm:gap-4 shrink-0">
          {/* Honest autosave indicator */}
          <div className="hidden sm:flex items-center gap-1.5 text-xs">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                saveState === 'saving'
                  ? 'bg-amber-500 animate-pulse'
                  : saveState === 'failed'
                  ? 'bg-rose-500'
                  : 'bg-emerald-500'
              }`}
            />
            <span
              className={
                saveState === 'failed' ? 'text-rose-600 font-semibold' : 'text-slate-400'
              }
            >
              {saveState === 'saving'
                ? 'Saving...'
                : saveState === 'failed'
                ? 'Save failed'
                : 'Autosaved'}
            </span>
          </div>

          {/*
            Status only. There is deliberately NO "Exit Fullscreen" control:
            the candidate must not be able to leave fullscreen by clicking.
            Leaving it via Escape / F11 / the browser menu pauses the exam.
          */}
          <span
            className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-semibold ${
              isFs
                ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                : 'border-rose-200 bg-rose-50 text-rose-700'
            }`}
            role="status"
          >
            <MonitorCheck className="h-3.5 w-3.5" />
            <span className="hidden md:inline">
              {isFs ? 'Fullscreen active' : 'Not fullscreen'}
            </span>
          </span>

          <div
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border font-mono text-sm font-bold ${
              timeLeft < 300
                ? 'bg-rose-50 border-rose-200 text-rose-700'
                : 'bg-slate-50 border-slate-200 text-slate-800'
            }`}
          >
            <Clock className="h-4 w-4 text-slate-500" />
            <span>{formatTimer(timeLeft)}</span>
          </div>

          <button
            onClick={() => {
              setSubmitError(null)
              setShowSubmitModal(true)
            }}
            className="px-4 py-2 sm:py-1.5 rounded-lg text-sm font-semibold bg-indigo-600 text-white hover:bg-indigo-700 transition-colors min-h-[44px] sm:min-h-0"
          >
            Submit Exam
          </button>
        </div>
      </header>

      <div className="flex-1 flex flex-col md:flex-row min-h-0 min-w-0 overflow-hidden">
        {/* MOBILE NAVIGATOR — horizontal strip, same state as the desktop rail. */}
        <div className="md:hidden shrink-0 border-b border-slate-200 bg-white px-3 py-2 flex items-center gap-2 overflow-x-auto custom-scrollbar">
          <span className="text-2xs font-bold uppercase tracking-wider text-slate-400 shrink-0">
            {answeredCount}/{questions.length}
          </span>
          {questions.map((q, idx) => {
            let cls = 'border-slate-200 bg-white text-slate-700'
            if (answers[q.id]?.trim()) cls = 'bg-emerald-600 text-white border-emerald-600 font-bold'
            if (markedForReview[q.id]) cls = 'bg-amber-500 text-white border-amber-500 font-bold'
            if (idx === currentIndex) cls += ' ring-2 ring-indigo-600 ring-offset-1 border-indigo-600'
            return (
              <button
                key={q.id}
                onClick={() => setCurrentIndex(idx)}
                aria-label={`Go to question ${idx + 1}`}
                aria-current={idx === currentIndex}
                className={`h-10 w-10 shrink-0 rounded-lg border text-sm font-medium flex items-center justify-center transition-all ${cls}`}
              >
                {idx + 1}
              </button>
            )
          })}
        </div>

        {/* NAVIGATOR — fixed rail on desktop */}
        <aside className="hidden md:flex w-64 border-r border-slate-200 bg-white flex-col justify-between shrink-0">
          <div className="p-4 space-y-4 overflow-y-auto custom-scrollbar">
            <div>
              <div className="flex items-center justify-between text-sm font-bold text-slate-800 mb-1">
                <span>Questions</span>
                <span className="text-indigo-600 font-semibold">
                  {answeredCount} / {questions.length}
                </span>
              </div>
              <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                <div
                  className="h-full bg-indigo-600 rounded-full transition-all duration-200"
                  style={{ width: `${(answeredCount / questions.length) * 100}%` }}
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2 text-xs font-semibold text-slate-600 border-b border-slate-100 pb-3">
              <div className="flex items-center gap-1.5">
                <span className="h-4 w-4 rounded bg-emerald-600 text-white flex items-center justify-center text-2xs font-bold">
                  ✓
                </span>
                <span>Answered</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="h-4 w-4 rounded border border-slate-300 bg-white" />
                <span>Unanswered</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="h-4 w-4 rounded bg-amber-500 text-white flex items-center justify-center text-2xs font-bold">
                  ★
                </span>
                <span>Review</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="h-4 w-4 rounded border-2 border-indigo-600 bg-indigo-50" />
                <span>Current</span>
              </div>
            </div>

            <div className="grid grid-cols-5 gap-2">
              {questions.map((q, idx) => {
                let cls = 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                if (answers[q.id]?.trim()) cls = 'bg-emerald-600 text-white border-emerald-600 font-bold'
                if (markedForReview[q.id]) cls = 'bg-amber-500 text-white border-amber-500 font-bold'
                if (idx === currentIndex) cls += ' ring-2 ring-indigo-600 ring-offset-1 border-indigo-600'
                return (
                  <button
                    key={q.id}
                    onClick={() => setCurrentIndex(idx)}
                    aria-label={`Go to question ${idx + 1}`}
                    aria-current={idx === currentIndex}
                    className={`h-8 rounded-lg border text-sm font-medium flex items-center justify-center transition-all ${cls}`}
                  >
                    {idx + 1}
                  </button>
                )
              })}
            </div>
          </div>

          <div className="p-3 border-t border-slate-100 text-xs text-slate-500 bg-slate-50/50">
            Answers save automatically
          </div>
        </aside>

        {/* WORKSPACE */}
        <main className="flex-1 flex flex-col justify-between overflow-y-auto custom-scrollbar p-3 sm:p-6 bg-slate-50 min-w-0">
          <div className="max-w-4xl mx-auto w-full min-w-0 space-y-4">
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-2xs space-y-3">
              <div className="flex items-center justify-between pb-3 border-b border-slate-100 flex-wrap gap-2">
                <div className="flex items-center gap-2">
                  <span className="rounded-md bg-indigo-600 px-2.5 py-0.5 text-xs font-bold text-white">
                    Question {currentIndex + 1}
                  </span>
                  <span className="text-xs font-medium text-slate-500">{currentQuestion.category}</span>
                  <span className="text-xs font-semibold text-slate-700">
                    · {currentQuestion.marks ?? 0} Marks
                  </span>
                </div>

                <button
                  onClick={toggleMarkForReview}
                  className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium transition-colors ${
                    isCurrentMarked ? 'bg-amber-100 text-amber-800' : 'text-slate-500 hover:bg-slate-100'
                  }`}
                >
                  <Flag className="h-3.5 w-3.5" />
                  <span>{isCurrentMarked ? 'Marked for Review' : 'Mark for Review'}</span>
                </button>
              </div>

              <div>
                <h2 className="text-sm font-bold text-slate-900 mb-1.5">{currentQuestion.title}</h2>
                <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-wrap">
                  {currentQuestion.question_text}
                </p>
              </div>

              {currentQuestion.question_type === 'coding' && (
                <div className="pt-3 border-t border-slate-100 grid gap-2.5 text-xs">
                  {currentQuestion.input_requirements && (
                    <div className="rounded-lg bg-slate-50 border border-slate-200 p-3">
                      <span className="text-xs font-bold uppercase tracking-wider text-slate-500 block mb-1">
                        Input Requirements
                      </span>
                      <p className="font-mono text-slate-700 text-xs leading-relaxed whitespace-pre-wrap">
                        {currentQuestion.input_requirements}
                      </p>
                    </div>
                  )}
                  {currentQuestion.output_requirements && (
                    <div className="rounded-lg bg-slate-50 border border-slate-200 p-3">
                      <span className="text-xs font-bold uppercase tracking-wider text-slate-500 block mb-1">
                        Output Requirements
                      </span>
                      <p className="font-mono text-slate-700 text-xs leading-relaxed whitespace-pre-wrap">
                        {currentQuestion.output_requirements}
                      </p>
                    </div>
                  )}
                  {currentQuestion.constraints && (
                    <div className="rounded-lg bg-slate-50 border border-slate-200 p-3">
                      <span className="text-xs font-bold uppercase tracking-wider text-slate-500 block mb-1">
                        Constraints
                      </span>
                      <p className="font-mono text-indigo-900 font-semibold text-xs leading-relaxed whitespace-pre-wrap">
                        {currentQuestion.constraints}
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* MCQ */}
            {currentQuestion.question_type === 'mcq' && Array.isArray(currentQuestion.options) && (
              <div className="space-y-2">
                <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                  Select one answer
                </p>
                <div className="grid gap-2">
                  {(currentQuestion.options as { id: string; text: string }[]).map((opt, idx) => {
                    const letter = String.fromCharCode(65 + idx)
                    const isSelected = currentAnswer === opt.id
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        onClick={() => handleSelectOption(currentQuestion.id, opt.id)}
                        aria-pressed={isSelected}
                        className={`rounded-xl border p-3.5 text-left transition-all flex items-center justify-between ${
                          isSelected
                            ? 'border-indigo-600 bg-indigo-50/70 ring-1 ring-indigo-600'
                            : 'border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/50'
                        }`}
                      >
                        <span className="flex items-center gap-3">
                          <span
                            className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-xs font-bold ${
                              isSelected ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-700'
                            }`}
                          >
                            {letter}
                          </span>
                          <span className="text-sm font-medium text-slate-800">{opt.text}</span>
                        </span>
                        <span
                          className={`h-4 w-4 shrink-0 rounded-full border flex items-center justify-center ${
                            isSelected ? 'border-indigo-600 bg-indigo-600 text-white' : 'border-slate-300 bg-white'
                          }`}
                        >
                          {isSelected && <Check className="h-2.5 w-2.5 stroke-[3]" />}
                        </span>
                      </button>
                    )
                  })}
                </div>
              </div>
            )}

            {/* CODING — no Run, Compile, Test or per-question submit */}
            {currentQuestion.question_type === 'coding' && (
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2 text-sm font-bold text-slate-800">
                    <Code2 className="h-4 w-4 text-indigo-600" />
                    <span>Programming Solution</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-slate-500 font-medium">Language:</span>
                    <span
                      aria-label="Programming language"
                      className="text-xs font-bold text-slate-700 bg-slate-100 px-2.5 py-1 rounded-lg border border-slate-200"
                    >
                      C++
                    </span>
                  </div>
                </div>

                <div className="rounded-xl border border-slate-300 overflow-hidden bg-[#1e1e1e] min-w-0 max-w-full">
                  <Editor
                    height="380px"
                    language="cpp"
                    value={currentAnswer || currentQuestion.starter_code || ''}
                    onChange={handleCodeChange}
                    theme="vs-dark"
                    options={{
                      fontSize: 14,
                      minimap: { enabled: false },
                      scrollBeyondLastLine: false,
                      lineNumbers: 'on',
                      wordWrap: 'on',
                      automaticLayout: true,
                      tabSize: 4,
                    }}
                  />
                </div>
              </div>
            )}
          </div>

          <div className="max-w-4xl mx-auto w-full pt-4 mt-4 border-t border-slate-200 flex items-center justify-between">
            <button
              onClick={() => setCurrentIndex((prev) => Math.max(0, prev - 1))}
              disabled={currentIndex === 0}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-300 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="h-4 w-4" />
              <span>Previous</span>
            </button>

            <span className="text-sm text-slate-500 font-medium">
              Question {currentIndex + 1} of {questions.length}
            </span>

            <button
              onClick={() =>
                setCurrentIndex((prev) => Math.min(questions.length - 1, prev + 1))
              }
              disabled={currentIndex === questions.length - 1}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-300 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              <span>Next</span>
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </main>
      </div>

      {/* EXAM PAUSED */}
      {isPaused && (
        <div
          className="fixed inset-0 z-50 bg-slate-900/80 backdrop-blur-sm flex items-center justify-center p-4"
          role="alertdialog"
          aria-modal="true"
        >
          <div className="bg-white rounded-2xl p-6 max-w-md w-full shadow-2xl space-y-4 text-center">
            <div className="h-12 w-12 rounded-full bg-amber-100 text-amber-600 flex items-center justify-center mx-auto">
              <PauseCircle className="h-7 w-7" />
            </div>
            <div className="space-y-1">
              <h3 className="text-base font-bold text-slate-900 tracking-tight">EXAM PAUSED</h3>
              <p className="text-sm text-slate-700">Reason: {pauseReason}</p>
              {pausedAt && <p className="text-xs text-slate-500">Logged at {pausedAt}</p>}
            </div>
            <p className="text-xs text-slate-600 bg-slate-50 p-3 rounded-lg border border-slate-200">
              This event has been recorded for invigilator review. Your answers remain saved. The
              examination clock keeps running and answers cannot be edited until you resume.
            </p>

            {fsReason !== '' && (
              <div className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-left">
                <p className="text-sm font-semibold text-rose-800">
                  Fullscreen is required before you can resume
                </p>
                <p className="text-xs text-rose-700 mt-1 leading-relaxed">
                  {fullscreenFailureMessage(fsReason)}
                </p>
              </div>
            )}

            <button
              type="button"
              onClick={() => void handleResume()}
              disabled={isResuming || isEnteringFs}
              className="w-full inline-flex items-center justify-center gap-2 py-2.5 px-4 rounded-xl text-sm font-semibold bg-indigo-600 text-white hover:bg-indigo-700 transition-colors disabled:opacity-50"
            >
              {!isFullscreenActive() && <Maximize2 className="h-4 w-4" />}
              {isEnteringFs
                ? 'Entering fullscreen...'
                : isResuming
                  ? 'Resuming...'
                  : !isFullscreenActive()
                    ? 'Enter Fullscreen & Resume'
                    : 'Resume Exam'}
            </button>
          </div>
        </div>
      )}

      {/* SUBMIT CONFIRMATION */}
      {showSubmitModal && (
        <div className="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl p-6 max-w-md w-full shadow-2xl space-y-4">
            <div className="flex items-center gap-3">
              <div className="h-10 w-10 rounded-xl bg-indigo-50 text-indigo-600 flex items-center justify-center shrink-0">
                <AlertCircle className="h-5 w-5" />
              </div>
              <div>
                <h3 className="text-sm font-bold text-slate-900">Submit examination?</h3>
                <p className="text-xs text-slate-600 mt-0.5">
                  Your attempt will be frozen and evaluated by the server.
                </p>
              </div>
            </div>

            {submitError && (
              <div className="flex items-start gap-2 rounded-lg bg-rose-50 border border-rose-200 p-3 text-xs text-rose-700">
                <CloudOff className="h-4 w-4 shrink-0 mt-0.5" />
                <span>{submitError}</span>
              </div>
            )}

            <div className="grid grid-cols-3 gap-2 py-3 px-3.5 rounded-xl bg-slate-50 border border-slate-200 text-center text-xs">
              <div>
                <span className="block text-slate-500 text-xs uppercase font-semibold">Answered</span>
                <span className="font-bold text-emerald-600 text-base">{answeredCount}</span>
              </div>
              <div>
                <span className="block text-slate-500 text-xs uppercase font-semibold">Unanswered</span>
                <span className="font-bold text-slate-700 text-base">{unansweredCount}</span>
              </div>
              <div>
                <span className="block text-slate-500 text-xs uppercase font-semibold">Review</span>
                <span className="font-bold text-amber-600 text-base">{markedCount}</span>
              </div>
            </div>

            {saveError && (
              <p className="text-xs text-rose-600 bg-rose-50 border border-rose-200 rounded-lg p-2">
                {saveError}
              </p>
            )}

            <div className="flex items-center gap-2 pt-2">
              <button
                type="button"
                onClick={() => setShowSubmitModal(false)}
                disabled={isSubmitting}
                className="flex-1 py-2 px-3 rounded-xl border border-slate-300 text-sm font-semibold text-slate-700 hover:bg-slate-50 transition-colors disabled:opacity-50"
              >
                Continue Exam
              </button>
              <button
                type="button"
                onClick={() => void performSubmit()}
                disabled={isSubmitting}
                className="flex-1 py-2 px-3 rounded-xl bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 transition-colors disabled:opacity-50"
              >
                {isSubmitting ? 'Submitting...' : 'Confirm Submission'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
