"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { fetchBackendApi, logoutSession } from "@/lib/backend-api";
import { RefChip } from "@/components/ui";

const navItems = [
  { href: "/employee/dashboard", label: "Home", icon: "⌂" },
  { href: "/employee/support", label: "Support", icon: "?" },
  { href: "/employee/profile", label: "Profile", icon: "👤" },
];

export default function EmployeeNavbar() {
  const pathname = usePathname();
  const router = useRouter();
  const [profile, setProfile] = useState<{ name: string; refId: string } | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [logoutError, setLogoutError] = useState("");

  useEffect(() => {
    fetchBackendApi("api/v1/agents/me")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.employee) {
          setProfile({ name: `${d.employee.firstName} ${d.employee.lastName}`, refId: d.employee.refId });
        } else if (d?.agent) {
          setProfile({ name: `${d.agent.first_name} ${d.agent.last_name}`, refId: d.agent.ref_id });
        }
      })
      .catch(() => {});
  }, []);

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
    <>
      <header className="bg-white/95 backdrop-blur border-b border-slate-200 sticky top-0 z-40 safe-top">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between gap-3">
          <Link href="/employee/dashboard" className="flex items-center gap-2.5 min-w-0">
            <div className="w-9 h-9 shrink-0 rounded-lg bg-navy-700 flex items-center justify-center shadow-sm">
              <span className="font-display text-emerald-400 font-bold text-xs">SP</span>
            </div>
            <span className="font-display font-semibold text-navy-800 truncate">Sahayak Partners</span>
          </Link>

          <nav className="hidden md:flex items-center gap-1">
            {navItems.map((item) => {
              const active = pathname === item.href;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`px-3.5 py-2 rounded-lg text-sm font-medium transition-colors ${
                    active ? "bg-navy-50 text-navy-700" : "text-slate-500 hover:text-navy-700"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <div className="flex items-center gap-2">
            {profile && (
              <div className="text-right hidden sm:block">
                <p className="text-sm font-medium text-slate-800 leading-tight truncate max-w-[180px]">{profile.name}</p>
                <div className="mt-0.5">
                  <RefChip refId={profile.refId} />
                </div>
              </div>
            )}

            <button
              onClick={() => setMenuOpen((open) => !open)}
              aria-label="Open account menu"
              aria-expanded={menuOpen}
              className="md:hidden w-10 h-10 rounded-lg border border-slate-200 text-slate-600 flex items-center justify-center text-xl"
            >
              <span aria-hidden>{menuOpen ? "×" : "☰"}</span>
            </button>

            <button onClick={handleLogout} className="hidden md:block text-sm text-slate-400 hover:text-red-600 font-medium px-2 py-2">
              Log out
            </button>
          </div>
        </div>

        {menuOpen && (
          <div className="md:hidden border-t border-slate-100 bg-white px-4 py-3 shadow-sm">
            {profile && (
              <div className="pb-3 mb-2 border-b border-slate-100">
                <p className="text-sm font-semibold text-slate-800">{profile.name}</p>
                <div className="mt-1"><RefChip refId={profile.refId} /></div>
              </div>
            )}
            <nav className="space-y-1">
              {navItems.map((item) => {
                const active = pathname === item.href;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`flex items-center gap-3 min-h-11 px-3 rounded-lg text-sm font-medium ${
                      active ? "bg-navy-50 text-navy-700" : "text-slate-600"
                    }`}
                  >
                    <span className="w-6 text-center font-semibold" aria-hidden>{item.icon}</span>
                    {item.label}
                  </Link>
                );
              })}
              <button
                onClick={handleLogout}
                className="w-full flex items-center gap-3 min-h-11 px-3 rounded-lg text-sm font-medium text-red-600"
              >
                <span className="w-6 text-center" aria-hidden>↪</span>
                Log out
              </button>
            </nav>
          </div>
        )}
      </header>
      {logoutError && (
        <p role="alert" className="px-4 py-2 text-sm text-red-700">
          {logoutError}
        </p>
      )}

      <nav className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-white/95 backdrop-blur border-t border-slate-200 safe-bottom">
        <div className="grid grid-cols-2 max-w-lg mx-auto">
          {navItems.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`min-h-16 flex flex-col items-center justify-center gap-1 text-[11px] font-medium ${
                  active ? "text-navy-700" : "text-slate-400"
                }`}
              >
                <span className={`text-lg leading-none ${active ? "font-bold" : ""}`} aria-hidden>{item.icon}</span>
                {item.label}
              </Link>
            );
          })}
        </div>
      </nav>
    </>
  );
}
