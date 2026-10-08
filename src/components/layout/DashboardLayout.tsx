import React, { useState } from 'react'
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
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  return (
    <div className="h-screen w-screen overflow-hidden flex bg-slate-50 font-sans antialiased text-slate-900">
      {/* Fixed Sidebar (desktop) + slide-over drawer (mobile) */}
      {showSidebar && (
        <Sidebar mobileOpen={mobileNavOpen} onMobileClose={() => setMobileNavOpen(false)} />
      )}

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0 h-screen overflow-hidden">
        <Navbar onMenuClick={showSidebar ? () => setMobileNavOpen(true) : undefined} />
        <main className="flex-1 overflow-y-auto p-4 sm:p-6 lg:p-8 custom-scrollbar">
          <div className="mx-auto max-w-6xl">{children}</div>
        </main>
      </div>
    </div>
  )
}
