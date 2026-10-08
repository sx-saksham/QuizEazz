import React, { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../common/Card'
import { Button } from '../common/Button'
import { Badge } from '../common/Badge'
import { NotificationBanner } from '../common/NotificationBanner'
import {
  Database,
  FileSpreadsheet,
  Users,
  Award,
  Plus,
  ShieldAlert,
  ArrowRight,
  AlertCircle,
} from 'lucide-react'
import { fetchOverviewStats } from '../../lib/teacherApi'
import { useAsyncData } from '../../hooks/useAsyncData'

interface RecentExam {
  id: string
  title: string
  duration_minutes: number
  status: string
  starts_at?: string | null
  created_at: string
}

/** "Scheduled" is a UI concept: published + future starts_at. */
const examStatusView = (e: RecentExam) => {
  if (e.status === 'draft') return { label: 'Draft', variant: 'warning' as const }
  if (e.status === 'closed') return { label: 'Closed', variant: 'neutral' as const }
  if (e.starts_at && new Date(e.starts_at) > new Date()) {
    return { label: 'Scheduled', variant: 'info' as const }
  }
  return { label: 'Open', variant: 'success' as const }
}

const EMPTY_OVERVIEW = {
  total_questions: 0,
  total_exams: 0,
  active_exams: 0,
  total_students: 0,
  total_attempts: 0,
  recent_exams: [] as RecentExam[],
}

export const TeacherOverview: React.FC = () => {
  // RPC-only. A failure is reported rather than masked with fallback data.
  const loadStats = useCallback(() => fetchOverviewStats(), [])
  const { data: overview, error, reload } = useAsyncData(loadStats, EMPTY_OVERVIEW, {
    errorFallback: 'Unable to load dashboard statistics.',
  })

  const stats = {
    totalQuestions: overview.total_questions ?? 0,
    totalExams: overview.total_exams ?? 0,
    totalStudents: overview.total_students ?? 0,
    totalAttempts: overview.total_attempts ?? 0,
    activeExams: overview.active_exams ?? 0,
  }
  const recentExams: RecentExam[] = overview.recent_exams ?? []

  return (
    <div className="space-y-6">
      {/* Faculty Console Header */}
      <div className="rounded-2xl border border-indigo-100/90 bg-gradient-to-r from-purple-50/90 via-indigo-50/60 to-blue-50/40 p-6 sm:p-7 shadow-2xs">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="flex items-center gap-2.5">
              <h1 className="text-xl font-black tracking-tight text-slate-900 sm:text-2xl">
                Faculty Examination Console
              </h1>
              <Badge variant="purple" size="sm">
                Faculty Workspace
              </Badge>
            </div>
            <p className="mt-1 text-xs text-slate-500 font-medium">
              Departmental assessment management, question bank control, and candidate performance tracking
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Link to="/teacher/exams">
              <Button variant="primary" size="sm" icon={<Plus className="h-3.5 w-3.5" />}>
                Schedule Exam
              </Button>
            </Link>
          </div>
        </div>

        {/* Stats Grid */}
        <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-xl border border-slate-200 bg-white p-4 flex flex-col gap-2 shadow-sm">
            <div className="flex items-center justify-between">
              <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">
                Question Bank
              </span>
              <Database className="h-4 w-4 shrink-0 text-blue-600" />
            </div>
            <p className="text-2xl font-bold text-slate-900 leading-tight">{stats.totalQuestions}</p>
            <p className="text-2xs text-slate-500 leading-normal">Verified questions in repository</p>
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-4 flex flex-col gap-2 shadow-sm">
            <div className="flex items-center justify-between">
              <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">
                Examinations
              </span>
              <FileSpreadsheet className="h-4 w-4 shrink-0 text-indigo-600" />
            </div>
            <p className="text-2xl font-bold text-slate-900 leading-tight">{stats.totalExams}</p>
            <p className="text-2xs text-slate-500 leading-normal">{stats.activeExams} active or scheduled</p>
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-4 flex flex-col gap-2 shadow-sm">
            <div className="flex items-center justify-between">
              <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">
                Registered Students
              </span>
              <Users className="h-4 w-4 shrink-0 text-emerald-600" />
            </div>
            <p className="text-2xl font-bold text-slate-900 leading-tight">{stats.totalStudents}</p>
            <p className="text-2xs text-slate-500 leading-normal">Candidate roll profiles</p>
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-4 flex flex-col gap-2 shadow-sm">
            <div className="flex items-center justify-between">
              <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">
                Submissions
              </span>
              <Award className="h-4 w-4 shrink-0 text-purple-600" />
            </div>
            <p className="text-2xl font-bold text-slate-900 leading-tight">{stats.totalAttempts}</p>
            <p className="text-2xs text-slate-500 leading-normal">Total recorded attempts</p>
          </div>
        </div>
      </div>

      {error && (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="flex-1">
            <NotificationBanner type="error" message={error} onClose={reload} />
          </div>
          <Button variant="outline" size="sm" onClick={reload}>
            Try Again
          </Button>
        </div>
      )}

      {/* Two Column Layout */}
      <div className="grid gap-6 lg:grid-cols-3">
        {/* Left 2 Cols: Recent Examination Papers */}
        <div className="lg:col-span-2 space-y-6">
          <Card>
            <CardHeader className="flex items-center justify-between">
              <div>
                <CardTitle>Recent Examination Papers</CardTitle>
                <CardDescription>Scheduled and active department test papers</CardDescription>
              </div>
              <Link to="/teacher/exams">
                <Button variant="ghost" size="sm">
                  View All Papers
                </Button>
              </Link>
            </CardHeader>
            <CardContent className="p-0">
              {recentExams.length === 0 ? (
                <div className="p-8 text-center text-xs text-slate-500">
                  No examination papers have been created yet. Click on "Schedule Exam" to set up your first paper.
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs text-slate-600">
                    <thead className="bg-slate-50 text-2xs font-semibold uppercase tracking-wider text-slate-500 border-b border-slate-200">
                      <tr>
                        <th className="py-3 px-4">Title</th>
                        <th className="py-3 px-4">Duration</th>
                        <th className="py-3 px-4">Status</th>
                        <th className="py-3 px-4 text-right">Created</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {recentExams.map((e) => {
                        const view = examStatusView(e)
                        return (
                          <tr key={e.id} className="hover:bg-slate-50/80">
                            <td className="py-3 px-4 font-semibold text-slate-900">{e.title}</td>
                            <td className="py-3 px-4 text-slate-600">{e.duration_minutes} mins</td>
                            <td className="py-3 px-4">
                              <Badge variant={view.variant} size="sm">
                                {view.label}
                              </Badge>
                            </td>
                            <td className="py-3 px-4 text-right text-slate-400 whitespace-nowrap">
                              {new Date(e.created_at).toLocaleDateString()}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right Col: Faculty Navigation & Proctoring Telemetry */}
        <div className="space-y-6">
          {/* Quick Actions Panel */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-bold text-slate-900">
                Department Management
              </CardTitle>
              <CardDescription>
                Quick access to core examination workflows
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2.5 p-4 pt-0">
              <Link
                to="/teacher/questions"
                className="group flex items-center gap-3 p-3 rounded-xl border border-slate-200 bg-white shadow-2xs hover:bg-indigo-50/60 hover:border-indigo-200 transition-colors"
              >
                <Database className="h-5 w-5 shrink-0 text-blue-600 group-hover:text-indigo-600 transition-colors" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold text-slate-800 leading-snug group-hover:text-indigo-900">
                    Question Repository
                  </p>
                  <p className="text-2xs text-slate-400 mt-0.5 leading-normal">
                    Author MCQs and coding questions
                  </p>
                </div>
                <ArrowRight className="h-3.5 w-3.5 shrink-0 text-slate-300 group-hover:text-indigo-600 group-hover:translate-x-0.5 transition-all" />
              </Link>

              <Link
                to="/teacher/exams"
                className="group flex items-center gap-3 p-3 rounded-xl border border-slate-200 bg-white shadow-2xs hover:bg-indigo-50/60 hover:border-indigo-200 transition-colors"
              >
                <FileSpreadsheet className="h-5 w-5 shrink-0 text-indigo-600 group-hover:text-indigo-600 transition-colors" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold text-slate-800 leading-snug group-hover:text-indigo-900">
                    Examinations & Schedules
                  </p>
                  <p className="text-2xs text-slate-400 mt-0.5 leading-normal">
                    Configure papers and timers
                  </p>
                </div>
                <ArrowRight className="h-3.5 w-3.5 shrink-0 text-slate-300 group-hover:text-indigo-600 group-hover:translate-x-0.5 transition-all" />
              </Link>

              <Link
                to="/teacher/students"
                className="group flex items-center gap-3 p-3 rounded-xl border border-slate-200 bg-white shadow-2xs hover:bg-indigo-50/60 hover:border-indigo-200 transition-colors"
              >
                <Users className="h-5 w-5 shrink-0 text-emerald-600 group-hover:text-emerald-700 transition-colors" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold text-slate-800 leading-snug group-hover:text-emerald-900">
                    Candidate Registry
                  </p>
                  <p className="text-2xs text-slate-400 mt-0.5 leading-normal">
                    Review registered examinees
                  </p>
                </div>
                <ArrowRight className="h-3.5 w-3.5 shrink-0 text-slate-300 group-hover:text-emerald-700 group-hover:translate-x-0.5 transition-all" />
              </Link>

              <Link
                to="/teacher/results"
                className="group flex items-center gap-3 p-3 rounded-xl border border-slate-200 bg-white shadow-2xs hover:bg-indigo-50/60 hover:border-indigo-200 transition-colors"
              >
                <Award className="h-5 w-5 shrink-0 text-purple-600 group-hover:text-purple-700 transition-colors" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold text-slate-800 leading-snug group-hover:text-purple-900">
                    Results & Submissions
                  </p>
                  <p className="text-2xs text-slate-400 mt-0.5 leading-normal">
                    Inspect scores and student answers
                  </p>
                </div>
                <ArrowRight className="h-3.5 w-3.5 shrink-0 text-slate-300 group-hover:text-purple-700 group-hover:translate-x-0.5 transition-all" />
              </Link>
            </CardContent>
          </Card>

          {/* Session Event Recording */}
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-sm font-bold text-slate-900 flex items-center gap-2">
                  <ShieldAlert className="h-4 w-4 text-amber-500" />
                  Session Event Recording
                </CardTitle>
                <span className="inline-flex items-center gap-1 text-2xs font-semibold text-emerald-600">
                  <AlertCircle className="h-3.5 w-3.5" />
                  During attempts
                </span>
              </div>
              <CardDescription>
                Objective browser events captured for invigilator review
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 pt-0">
              <p className="text-xs text-slate-600 leading-relaxed">
                During an attempt, window blur and tab switches are timestamped and recorded, and the
                examination pauses until the candidate resumes. These records are factual only: no
                candidate is labelled a violator and no automatic penalty is applied. Browser events
                cannot observe OS-level screenshots or keyboard shortcuts, so physical invigilation
                and a locked-down workstation remain the real controls.
              </p>
              <Link to="/teacher/analytics">
                <Button variant="outline" size="sm" className="w-full justify-center">
                  View Security Event Log
                </Button>
              </Link>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
