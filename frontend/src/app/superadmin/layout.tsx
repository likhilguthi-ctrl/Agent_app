"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import AuthGuard, { useAuth } from "@/components/AuthGuard";
import { Icon, icons } from "@/components/icons";
import { SuperAdminNotifications } from "@/components/SuperAdminNotifications";

function SuperAdminHeader() {
  const { identity, logout } = useAuth();
  const pathname = usePathname();

  const navItems = [
    { href: "/superadmin/dashboard", label: "Dashboard" },
    { href: "/superadmin/communities", label: "Communities" },
    { href: "/superadmin/plans", label: "Subscription Plans" },
    { href: "/superadmin/activity", label: "Activity" },
    { href: "/superadmin/profile", label: "Profile" },
  ];

  return (
    <header className="border-b border-slate-200 bg-white px-5 py-3.5 sm:px-8 shadow-sm">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4">
        <div className="flex items-center gap-6">
          <Link href="/superadmin/dashboard" className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-navy-700 flex items-center justify-center">
              <span className="font-display text-emerald-400 font-bold text-xs">SP</span>
            </div>
            <div>
              <p className="font-display text-sm font-semibold text-navy-800 leading-tight">
                Community OTT
              </p>
              <p className="text-[10px] font-semibold uppercase tracking-wider text-emerald-700">
                Super Admin
              </p>
            </div>
          </Link>

          <nav className="hidden sm:flex items-center gap-1">
            {navItems.map((item) => {
              const active = pathname === item.href;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                    active
                      ? "bg-navy-50 text-navy-800 font-semibold"
                      : "text-slate-600 hover:text-navy-800 hover:bg-slate-50"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>

        <div className="flex items-center gap-3">
          <SuperAdminNotifications />
          <div className="hidden sm:block text-right">
            <p className="text-sm font-medium text-slate-800 leading-tight">
              {identity.username || "Super Admin"}
            </p>
            <p className="text-[11px] text-emerald-700 font-semibold">
              Platform Operator
            </p>
          </div>
          <button
            onClick={() => void logout()}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-slate-600 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors border border-slate-200 hover:border-red-200"
          >
            <Icon path={icons.logout} />
            <span>Log out</span>
          </button>
        </div>
      </div>
    </header>
  );
}

export default function SuperAdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard allowedRoles={["SUPER_ADMIN"]}>
      <main className="min-h-screen bg-sand-50">
        <SuperAdminHeader />
        <div className="mx-auto grid max-w-7xl gap-6 px-5 py-8 sm:px-8">{children}</div>
      </main>
    </AuthGuard>
  );
}
