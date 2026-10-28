"use client";

import DashboardShell from "@/components/DashboardShell";
import { Icon, icons } from "@/components/icons";
import AuthGuard, { useAuth } from "@/components/AuthGuard";

function AgentLayoutContent({ children }: { children: React.ReactNode }) {
  const { identity } = useAuth();
  const navItems = [
    { href: "/agent/dashboard", label: "Dashboard", icon: <Icon path={icons.grid} /> },
    { href: "/agent/customers", label: "Subscribers", icon: <Icon path={icons.users} /> },
    { href: "/agent/register", label: "Register subscriber", icon: <Icon path={icons.plus} /> },
    { href: "/agent/activity", label: "Activity log", icon: <Icon path={icons.activity} /> },
    { href: "/agent/profile", label: "My Profile", icon: <Icon path={icons.user} /> },
  ];

  const tenantSlug = identity.tenant?.slug || "Community Field Office";
  const displayName = identity.username || "Field Agent";

  return (
    <DashboardShell
      roleLabel="Field Agent"
      name={displayName}
      tenantLabel={tenantSlug}
      navItems={navItems}
    >
      {children}
    </DashboardShell>
  );
}

export default function AgentLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGuard allowedRoles={["AGENT"]}>
      <AgentLayoutContent>{children}</AgentLayoutContent>
    </AuthGuard>
  );
}

