import React from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { DashboardLayout } from '../layout/DashboardLayout'
import { StudentExamsPage } from './StudentExamsPage'
import { StudentResultsPage } from './StudentResultsPage'
import { StudentResultReviewPage } from './StudentResultReviewPage'
import { StudentGuidelinesPage } from './StudentGuidelinesPage'
import { StudentProfilePage } from './StudentProfilePage'

export const StudentPortal: React.FC = () => {
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
