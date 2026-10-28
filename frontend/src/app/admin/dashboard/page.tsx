"use client";

import Link from "next/link";
import { useAuth } from "@/components/AuthGuard";
import { Card, StatCard } from "@/components/ui";
import { AdminTargetManager } from "@/components/targets/AdminTargetManager";
import AgentCreationApprovalInbox from "@/components/AgentCreationApprovalInbox";

export default function AdminDashboard() {
  const { identity } = useAuth();
  const tenantSlug = identity.tenant?.slug || "demo-community";

  return (
    <div className="space-y-8">
      {/* Welcome Banner */}
      <div className="rounded-2xl border border-slate-200 bg-white p-6 sm:p-8 shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-semibold text-emerald-700">
                ● Community Manager Session Active
              </span>
              <span className="inline-flex items-center rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700 font-mono">
                {tenantSlug}
              </span>
            </div>
            <h1 className="mt-2 font-display text-2xl font-bold text-navy-800 sm:text-3xl">
              Community Manager Operations
            </h1>
            <p className="mt-1 text-sm text-slate-500">
              Welcome back, <span className="font-semibold text-slate-700">{identity.username || "Community Manager"}</span>. You are managing the <span className="font-semibold text-navy-800">{tenantSlug}</span> territory and agent hierarchy.
            </p>
          </div>
          <Link
            href="/admin/employees/add"
            className="inline-flex items-center justify-center rounded-lg bg-navy-700 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-navy-800 transition-colors"
          >
            Add Field Agent
          </Link>
        </div>
      </div>

      {/* Child Agent Creation Endorsement Inbox */}
      <AgentCreationApprovalInbox role="admin" title="Child Agent Creation Requests (Manager Review)" />

      {/* Summary Cards */}
      <div>
        <h2 className="font-display text-base font-semibold text-navy-800 mb-4">
          Community Overview
        </h2>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="Assigned Community"
            value={tenantSlug}
            sub="Tenant-isolated scope"
          />
          <StatCard
            label="Authorized Role"
            value="Community Manager"
            sub="Territory & agent oversight"
          />
          <StatCard
            label="Operational Status"
            value="Active"
            sub="Active community membership"
          />
          <StatCard
            label="Session Security"
            value="Protected"
            sub="PostgreSQL authenticated"
          />
        </div>
      </div>

      {/* Target Management & Team Cascade */}
      <AdminTargetManager />

      {/* Authenticated Identity & Context */}
      <div className="grid lg:grid-cols-3 gap-6">
        <Card className="p-6 lg:col-span-2">
          <h2 className="font-display font-semibold text-navy-800 mb-4">
            Authenticated Tenant Context
          </h2>
          <p className="text-sm text-slate-500 mb-6">
            Cryptographically verified identity claims resolved by the FastAPI backend (<code className="text-xs bg-slate-100 px-1 py-0.5 rounded font-mono">/api/v1/auth/me</code>).
          </p>

          <div className="grid sm:grid-cols-2 gap-4 text-sm">
            <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
              <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Username</span>
              <span className="font-semibold text-navy-800 mt-1 block">{identity.username}</span>
            </div>

            <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
              <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Authoritative Role</span>
              <span className="font-semibold text-emerald-700 mt-1 block">{identity.role}</span>
            </div>

            <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
              <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Tenant Slug</span>
              <span className="font-semibold text-navy-800 mt-1 block font-mono text-xs">{tenantSlug}</span>
            </div>

            <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
              <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Tenant ID</span>
              <span className="font-mono text-xs text-slate-700 mt-1 block truncate" title={identity.tenant_id || identity.tenant?.id || ""}>
                {identity.tenant_id || identity.tenant?.id || "—"}
              </span>
            </div>

            <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
              <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Membership ID</span>
              <span className="font-mono text-xs text-slate-700 mt-1 block truncate" title={identity.membership_id || ""}>
                {identity.membership_id || "—"}
              </span>
            </div>

            <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
              <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Session Expiration</span>
              <span className="font-medium text-slate-700 mt-1 block">
                {new Date(identity.expires_at).toLocaleString()}
              </span>
            </div>
          </div>
        </Card>

        {/* Quick Actions */}
        <Card className="p-6">
          <h2 className="font-display font-semibold text-navy-800 mb-4">
            Management Sections
          </h2>
          <div className="space-y-3">
            <Link
              href="/admin/employees"
              className="flex items-center justify-between p-3 rounded-lg border border-slate-200 hover:border-slate-300 hover:bg-slate-50 transition-colors"
            >
              <div>
                <p className="text-sm font-semibold text-navy-800">Team & Agents</p>
                <p className="text-xs text-slate-500">View roster of community agents</p>
              </div>
              <span className="text-slate-400">→</span>
            </Link>

            <Link
              href="/admin/employees/add"
              className="flex items-center justify-between p-3 rounded-lg border border-slate-200 hover:border-slate-300 hover:bg-slate-50 transition-colors"
            >
              <div>
                <p className="text-sm font-semibold text-navy-800">Add Team Member</p>
                <p className="text-xs text-slate-500">Register new field agent</p>
              </div>
              <span className="text-slate-400">→</span>
            </Link>

            <Link
              href="/admin/support"
              className="flex items-center justify-between p-3 rounded-lg border border-slate-200 hover:border-slate-300 hover:bg-slate-50 transition-colors"
            >
              <div>
                <p className="text-sm font-semibold text-navy-800">Support Tickets</p>
                <p className="text-xs text-slate-500">Resolve community subscriber queries</p>
              </div>
              <span className="text-slate-400">→</span>
            </Link>
          </div>
        </Card>
      </div>
    </div>
  );
}
