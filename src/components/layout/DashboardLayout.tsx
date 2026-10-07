import React from 'react'
import { Navbar } from './Navbar'
import { Sidebar } from './Sidebar'

export interface DashboardLayoutProps {
  children: React.ReactNode
  showSidebar?: boolean
}

export const DashboardLayout: React.FC<DashboardLayoutProps> = ({
  children,
  showSidebar = true,
}) => {
  return (
    <div className="h-screen w-screen overflow-hidden flex bg-slate-50 font-sans antialiased text-slate-900">
      {/* Fixed Sidebar */}
      {showSidebar && <Sidebar />}

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0 h-screen overflow-hidden">
        <Navbar />
        <main className="flex-1 overflow-y-auto p-6 sm:p-8 custom-scrollbar">
          <div className="mx-auto max-w-6xl">{children}</div>
        </main>
      </div>
    </div>
  )
}
