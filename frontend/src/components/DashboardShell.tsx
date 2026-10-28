"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Icon, icons } from "@/components/icons";
import { logoutSession } from "@/lib/backend-api";

interface NavItem {
  href: string;
  label: string;
  icon: React.ReactNode;
}

export default function DashboardShell({
  roleLabel,
  name,
  tenantLabel,
  navItems,
  children,
}: {
  roleLabel: string;
  name: string;
  tenantLabel?: string;
  navItems: NavItem[];
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);
  const [logoutError, setLogoutError] = useState("");

  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  async function handleLogout() {
    setLogoutError("");
    try {
      await logoutSession();
      router.push("/login");
    } catch {
      setLogoutError("Logout could not be completed. Please try again.");
    }
  }

  return (
    <div className="min-h-screen flex bg-sand-50 pb-16 md:pb-0">
      <aside className="hidden md:flex w-60 shrink-0 bg-navy-800 text-white flex-col sticky top-0 h-screen">
        <div className="px-5 py-5 flex items-center gap-2.5 border-b border-white/10">
          <div className="w-8 h-8 rounded-lg bg-emerald-500 flex items-center justify-center">
            <span className="font-display text-white font-bold text-xs">SP</span>
          </div>
          <div className="min-w-0">
            <p className="font-display text-sm font-semibold leading-tight truncate">
              {tenantLabel ? `Community: ${tenantLabel}` : "Sahayak Partners"}
            </p>
            <p className="text-[11px] text-navy-100/70 leading-tight">{roleLabel}</p>
          </div>
        </div>

        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {navItems.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                  active ? "bg-emerald-500 text-white" : "text-navy-100 hover:bg-white/10"
                }`}
              >
                {item.icon}
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="px-3 py-4 border-t border-white/10">
          <div className="px-3 py-2 mb-2 min-w-0">
            <p className="text-sm font-medium truncate">{name}</p>
            <p className="text-[11px] text-navy-100/60">{roleLabel}</p>
          </div>
          <button
            onClick={handleLogout}
            className="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-navy-100 hover:bg-white/10 transition-colors"
          >
            <Icon path={icons.logout} />
            Log out
          </button>
        </div>
      </aside>

      <div className="md:hidden fixed top-0 inset-x-0 z-40 bg-navy-800 text-white safe-top">
        <div className="h-14 px-4 flex items-center justify-between gap-3">
          <button
            onClick={() => setMenuOpen((open) => !open)}
            aria-label="Open navigation"
            aria-expanded={menuOpen}
            className="w-10 h-10 rounded-lg flex items-center justify-center hover:bg-white/10"
          >
            <span className="text-xl" aria-hidden>{menuOpen ? "×" : "☰"}</span>
          </button>
          <div className="min-w-0 text-center">
            <p className="font-display text-sm font-semibold truncate">
              {tenantLabel ? `Community: ${tenantLabel}` : "Sahayak Partners"}
            </p>
            <p className="text-[10px] text-navy-100/70 truncate">{roleLabel}</p>
          </div>
          <div className="w-10 h-10 rounded-lg bg-emerald-500 flex items-center justify-center shrink-0">
            <span className="font-display text-white font-bold text-xs">SP</span>
          </div>
        </div>

        {menuOpen && (
          <div className="border-t border-white/10 bg-navy-800 px-3 py-3 shadow-lg">
            <div className="px-3 py-2 mb-2 border-b border-white/10">
              <p className="text-sm font-medium truncate">{name}</p>
              <p className="text-[11px] text-navy-100/60">{roleLabel}</p>
            </div>
            <nav className="space-y-1">
              {navItems.map((item) => {
                const active = pathname === item.href;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`flex items-center gap-3 min-h-11 px-3 rounded-lg text-sm font-medium ${
                      active ? "bg-emerald-500 text-white" : "text-navy-100"
                    }`}
                  >
                    {item.icon}
                    {item.label}
                  </Link>
                );
              })}
              <button
                onClick={handleLogout}
                className="w-full flex items-center gap-3 min-h-11 px-3 rounded-lg text-sm font-medium text-navy-100"
              >
                <Icon path={icons.logout} />
                Log out
              </button>
            </nav>
          </div>
        )}
      </div>

      <main className="flex-1 min-w-0 pt-14 md:pt-0">
        {logoutError && (
          <p role="alert" className="px-4 pt-4 text-sm text-red-700">
            {logoutError}
          </p>
        )}
        <div className="w-full max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-5 sm:py-6 md:py-8">{children}</div>
      </main>

      <nav className="md:hidden fixed bottom-0 inset-x-0 z-30 bg-white/95 backdrop-blur border-t border-slate-200 safe-bottom">
        <div className="grid grid-cols-3 max-w-lg mx-auto">
          {navItems.slice(0, 3).map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`min-h-16 flex flex-col items-center justify-center gap-1 text-[10px] font-medium ${
                  active ? "text-navy-700" : "text-slate-400"
                }`}
              >
                <span className={active ? "text-navy-700" : "text-slate-400"}>{item.icon}</span>
                <span className="truncate max-w-[90px]">{item.label}</span>
              </Link>
            );
          })}
        </div>
      </nav>
    </div>
  );
}
