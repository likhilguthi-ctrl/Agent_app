"use client";

import EmployeeNavbar from "@/components/EmployeeNavbar";
import AuthGuard from "@/components/AuthGuard";

export default function EmployeeLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard allowedRoles={["AGENT"]}>
      <div className="min-h-screen bg-sand-50 pb-20 md:pb-0">
        <EmployeeNavbar />
        <main className="w-full max-w-6xl mx-auto px-4 sm:px-6 md:px-8 py-5 sm:py-6 md:py-8">
          {children}
        </main>
      </div>
    </AuthGuard>
  );
}
