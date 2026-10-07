import React from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { DashboardLayout } from '../layout/DashboardLayout'
import { TeacherOverview } from './TeacherOverview'
import { TeacherQuestionsView } from './TeacherQuestionsView'
import { TeacherExamsView } from './TeacherExamsView'
import { TeacherStudentsView } from './TeacherStudentsView'
import { TeacherResultsView } from './TeacherResultsView'
import { TeacherAnalyticsView } from './TeacherAnalyticsView'

export const TeacherDashboard: React.FC = () => {
  return (
    <DashboardLayout showSidebar={true}>
      <Routes>
        <Route index element={<TeacherOverview />} />
        <Route path="dashboard" element={<TeacherOverview />} />
        <Route path="questions" element={<TeacherQuestionsView />} />
        <Route path="exams" element={<TeacherExamsView />} />
        <Route path="students" element={<TeacherStudentsView />} />
        <Route path="results" element={<TeacherResultsView />} />
        <Route path="analytics" element={<TeacherAnalyticsView />} />
        {/* Wildcard redirect to teacher overview */}
        <Route path="*" element={<Navigate to="/teacher" replace />} />
      </Routes>
    </DashboardLayout>
  )
}

