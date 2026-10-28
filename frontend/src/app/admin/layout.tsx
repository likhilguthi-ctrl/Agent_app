"use client";

import DashboardShell from "@/components/DashboardShell";
import { Icon, icons } from "@/components/icons";
import AuthGuard, { useAuth } from "@/components/AuthGuard";

function AdminLayoutContent({ children }: { children: React.ReactNode }) {
  const { identity } = useAuth();
  const navItems = [
    { href: "/admin/dashboard", label: "Dashboard", icon: <Icon path={icons.grid} /> },
    { href: "/admin/employees", label: "Employees", icon: <Icon path={icons.people} /> },
    { href: "/admin/employees/add", label: "Add employee", icon: <Icon path={icons.plus} /> },
    { href: "/admin/support", label: "Support tickets", icon: <Icon path={icons.ticket} /> },
    { href: "/admin/profile", label: "My Profile", icon: <Icon path={icons.user} /> },
  ];

  const tenantSlug = identity.tenant?.slug || "Community";
  const displayName = identity.username || "Community Admin";

  return (
    <DashboardShell
      roleLabel="Admin"
      name={displayName}
      tenantLabel={tenantSlug}
      navItems={navItems}
    >
      {children}
    </DashboardShell>
  );
}

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard allowedRoles={["ADMIN"]}>
      <AdminLayoutContent>{children}</AdminLayoutContent>
    </AuthGuard>
  );
}
