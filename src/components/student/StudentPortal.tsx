import React from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { DashboardLayout } from '../layout/DashboardLayout'
import { StudentExamsPage } from './StudentExamsPage'
import { StudentResultsPage } from './StudentResultsPage'
import { StudentResultReviewPage } from './StudentResultReviewPage'
import { StudentGuidelinesPage } from './StudentGuidelinesPage'
import { StudentProfilePage } from './StudentProfilePage'
import { StudentOnboarding } from './StudentOnboarding'
import { useAuth } from '../../context/AuthContext'
import { isOnboarded } from '../../lib/cohort'

export const StudentPortal: React.FC = () => {
  const { profile } = useAuth()

  // Cohort gate: a student with no authoritative class/batch sees ONLY the
  // onboarding screen. No exam, result, review, guideline or profile route
  // is reachable until the database confirms membership.
  if (profile && profile.role === 'student' && !isOnboarded(profile)) {
    return <StudentOnboarding />
  }

  return (
    <DashboardLayout showSidebar={true}>
      <Routes>
        <Route index element={<Navigate to="exams" replace />} />
        <Route path="exams" element={<StudentExamsPage />} />
        <Route path="results" element={<StudentResultsPage />} />
        <Route path="review/:attemptId" element={<StudentResultReviewPage />} />
        <Route path="guidelines" element={<StudentGuidelinesPage />} />
        <Route path="profile" element={<StudentProfilePage />} />
        <Route path="*" element={<Navigate to="exams" replace />} />
      </Routes>
    </DashboardLayout>
  )
}
