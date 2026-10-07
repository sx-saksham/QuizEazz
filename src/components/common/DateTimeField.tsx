import React, { useCallback, useMemo } from 'react'
import { Calendar, Clock, ChevronUp, ChevronDown } from 'lucide-react'

interface DateTimeFieldProps {
  label?: string
  required?: boolean
  /** 'YYYY-MM-DDTHH:mm' in the viewer's local time. Empty means unset. */
  value: string
  onChange: (value: string) => void
  minDateTime?: string
  error?: string | null
  hint?: string
}

const pad = (n: number) => n.toString().padStart(2, '0')

const toLocalInput = (date: Date): string =>
  `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`

const QUICK_WINDOWS: { label: string; minutes: number }[] = [
  { label: 'In 15 min', minutes: 15 },
  { label: 'In 1 hour', minutes: 60 },
  { label: 'Tomorrow 9:00', minutes: 60 * 24 },
  { label: 'Next week', minutes: 60 * 24 * 7 },
]

/**
 * Intentional date/time picker for examination scheduling.
 *
 * Date and time are derived from `value` rather than mirrored into local
 * state, so the displayed fields can never drift from the submitted value.
 * Avoids the inconsistent browser-native datetime-local chrome while keeping
 * native date and time inputs for full keyboard and screen-reader support.
 */
export const DateTimeField: React.FC<DateTimeFieldProps> = ({
  label,
  required,
  value,
  onChange,
  minDateTime,
  error,
  hint,
}) => {
  const separatorIndex = value.indexOf('T')
  const datePart = separatorIndex === -1 ? '' : value.slice(0, separatorIndex)
  const timePart = separatorIndex === -1 ? '' : value.slice(separatorIndex + 1)

  const minDate = useMemo(() => {
    const base = minDateTime ?? toLocalInput(new Date())
    return base.slice(0, 10)
  }, [minDateTime])

  const emit = useCallback(
    (nextDate: string, nextTime: string) => {
      if (!nextDate) {
        onChange('')
        return
      }
      onChange(`${nextDate}T${nextTime || '00:00'}`)
    },
    [onChange],
  )

  const applyQuick = useCallback(
    (minutes: number) => {
      const d = new Date(Date.now() + minutes * 60 * 1000)
      if (minutes === 60 * 24) d.setHours(9, 0, 0, 0)
      onChange(toLocalInput(d))
    },
    [onChange],
  )

  const fieldClass = error
    ? 'border-rose-300 focus:border-rose-400 focus:ring-rose-300'
    : 'border-slate-300 hover:border-slate-400 focus:border-indigo-500 focus:ring-indigo-500'

  return (
    <div className="space-y-2">
      {label && (
        <label className="block text-xs font-semibold text-slate-700">
          {label} {required && <span className="text-rose-500">*</span>}
        </label>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-[1fr_120px] gap-2">
        <div className="relative">
          <Calendar className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400 pointer-events-none" />
          <input
            type="date"
            aria-label={label ? `${label} date` : 'Start date'}
            required={required}
            min={minDate}
            value={datePart}
            onChange={(e) => emit(e.target.value, timePart)}
            className={`w-full rounded-lg border bg-white pl-9 pr-3 py-2 text-xs text-slate-900 transition-colors focus:outline-none focus:ring-1 ${fieldClass}`}
          />
        </div>

        <div className="relative">
          <Clock className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400 pointer-events-none" />
          <input
            type="time"
            aria-label={label ? `${label} time` : 'Start time'}
            required={required}
            value={timePart}
            onChange={(e) => emit(datePart, e.target.value)}
            className={`w-full rounded-lg border bg-white pl-9 pr-2 py-2 text-xs text-slate-900 transition-colors focus:outline-none focus:ring-1 ${fieldClass}`}
          />
        </div>
      </div>

      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-2xs text-slate-400">Quick set:</span>
        {QUICK_WINDOWS.map((q) => (
          <button
            key={q.label}
            type="button"
            onClick={() => applyQuick(q.minutes)}
            className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 text-2xs font-medium text-slate-600 hover:bg-indigo-50 hover:border-indigo-200 hover:text-indigo-700 transition-colors"
          >
            {q.label.includes('Tomorrow') ? (
              <ChevronUp className="h-2.5 w-2.5" />
            ) : (
              <ChevronDown className="h-2.5 w-2.5" />
            )}
            {q.label}
          </button>
        ))}
      </div>

      {hint && !error && <p className="text-2xs text-slate-400">{hint}</p>}
      {error && <p className="text-2xs text-rose-600 font-medium">{error}</p>}
    </div>
  )
}

export default DateTimeField
