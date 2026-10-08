import React from 'react'
import {
  ShieldCheck,
  Monitor,
  Smartphone,
  Globe,
  Eye,
  UserCheck,
  AlertTriangle,
  Save,
  FileCheck2,
  Clock,
  ListChecks,
  Code2,
} from 'lucide-react'

interface Guideline {
  id: number
  title: string
  summary: string
  detail: string
  icon: React.ComponentType<{ className?: string }>
}

const guidelines: Guideline[] = [
  {
    id: 1,
    title: 'Assigned Workstation Only',
    summary: 'Sit at your designated computer.',
    detail:
      'Use only the workstation assigned to you for the whole session. Do not swap seats or change machines without permission from the invigilator.',
    icon: Monitor,
  },
  {
    id: 2,
    title: 'Electronic Devices Off',
    summary: 'No phones, smartwatches or external media.',
    detail:
      'Mobile phones, smartwatches, earphones and removable storage must be powered off and stored as instructed before the examination begins.',
    icon: Smartphone,
  },
  {
    id: 3,
    title: 'No External Applications',
    summary: 'Stay inside the QuizEazz window.',
    detail:
      'Do not open other browser tabs, browser developer tools, file managers, or any application outside the examination window. QuizEazz cannot detect these on its own; this is enforced by invigilation and the locked-down workstation.',
    icon: Globe,
  },
  {
    id: 4,
    title: 'No External Material',
    summary: 'No notes, websites or secondary devices.',
    detail:
      'Reference notes, browsers, messaging tools and secondary devices are not permitted. All permitted material is stated in the examination instructions.',
    icon: FileCheck2,
  },
  {
    id: 5,
    title: 'Stay in the Exam Window',
    summary: 'Focus interruptions pause your exam.',
    detail:
      'Switching windows, minimising the browser or changing tabs immediately pauses your examination and records a timestamped event for the invigilator. You must resume manually before continuing.',
    icon: Eye,
  },
  {
    id: 6,
    title: 'Compulsory Questions',
    summary: 'Every question counts.',
    detail:
      'All questions on your paper are compulsory. There is no choice, no negative marking, and no random question pool: your paper is fixed by faculty.',
    icon: ListChecks,
  },
  {
    id: 7,
    title: 'Continuous Autosave',
    summary: 'Your work saves as you type.',
    detail:
      'Answers and code save automatically to the examination server as you work. The indicator in the header shows saving, saved, or a failed save. If you ever see "Save failed", raise your hand immediately.',
    icon: Save,
  },
  {
    id: 8,
    title: 'No Run or Test Buttons',
    summary: 'Coding is written, not debugged live.',
    detail:
      'During a coding question there is no Run, Compile or Test button. You cannot see whether your code produces the expected output before submitting. Write the solution to the specification and submit.',
    icon: Code2,
  },
  {
    id: 9,
    title: 'Timed Session',
    summary: 'The timer does not stop.',
    detail:
      'The countdown shown in the header is derived from your start time and the exam duration. If it reaches zero your attempt is submitted automatically using whatever is saved at that moment.',
    icon: Clock,
  },
  {
    id: 10,
    title: 'Single Final Submission',
    summary: 'Submit once, then it is frozen.',
    detail:
      'Only the Final Submit action records your attempt. After submitting you cannot reopen or edit it. Confirm your answers before submitting.',
    icon: FileCheck2,
  },
  {
    id: 11,
    title: 'Invigilator Authority',
    summary: 'Follow invigilator instructions.',
    detail:
      'Faculty invigilators hold final authority over laboratory conduct and procedure. Their instructions always take precedence over this page.',
    icon: UserCheck,
  },
  {
    id: 12,
    title: 'Report Problems Immediately',
    summary: 'Raise your hand, do not leave your seat.',
    detail:
      'For hardware, network or platform problems, raise your hand quietly. The invigilator will attend to your workstation without disrupting other candidates.',
    icon: AlertTriangle,
  },
]

export const StudentGuidelinesPage: React.FC = () => (
  <div className="space-y-6 max-w-5xl mx-auto pb-8">
    <div className="rounded-2xl border border-indigo-100 bg-gradient-to-r from-purple-50/80 via-indigo-50/50 to-blue-50/40 p-6 sm:p-7 shadow-2xs">
      <div className="flex items-center gap-3">
        <div className="h-10 w-10 rounded-xl bg-indigo-600 text-white flex items-center justify-center shrink-0">
          <ShieldCheck className="h-5 w-5" />
        </div>
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900 sm:text-2xl">
            Examination Guidelines
          </h1>
          <p className="mt-1 text-xs text-slate-500 font-medium">
            Rules that apply to every QuizEazz examination session
          </p>
        </div>
      </div>
    </div>

    <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
      {guidelines.map((g) => {
        const Icon = g.icon
        return (
          <div
            key={g.id}
            className="group relative flex flex-col rounded-xl border border-slate-200/90 bg-white p-5 shadow-2xs hover:border-indigo-200 transition-colors"
          >
            <div>
              <div className="flex items-center justify-between mb-3">
                <span className="flex h-6 w-6 items-center justify-center rounded-md bg-indigo-50 text-indigo-700 font-bold text-xs border border-indigo-100">
                  {g.id}
                </span>
                <div className="h-7 w-7 rounded-lg bg-slate-50 text-slate-500 flex items-center justify-center group-hover:bg-indigo-50 group-hover:text-indigo-600 transition-colors">
                  <Icon className="h-3.5 w-3.5" />
                </div>
              </div>

              <h3 className="text-xs font-bold text-slate-900 tracking-tight">{g.title}</h3>
              <p className="mt-1 text-xs font-medium text-indigo-950/80 leading-snug">{g.summary}</p>
              <p className="mt-2 text-2xs text-slate-500 leading-relaxed border-t border-slate-100 pt-2">
                {g.detail}
              </p>
            </div>
          </div>
        )
      })}
    </div>
  </div>
)
