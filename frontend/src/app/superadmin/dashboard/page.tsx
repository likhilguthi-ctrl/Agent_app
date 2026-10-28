"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/components/AuthGuard";
import { Card, StatCard } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";
import { SuperAdminTargetManager } from "@/components/targets/SuperAdminTargetManager";
import AgentCreationApprovalInbox from "@/components/AgentCreationApprovalInbox";

interface PendingApproval {
  id: string;
  agent_profile_id?: string | null;
  appointment_id?: string | null;
  title: string;
  message: string;
  resolution_status: string;
  created_at: string;
}

export default function SuperAdminDashboard() {
  const { identity } = useAuth();
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([]);

  useEffect(() => {
    async function loadPending() {
      try {
        const res = await fetchBackendApi(
          "api/v1/platform/notifications?resolution_status=PENDING&limit=10",
          {
            credentials: "same-origin",
            cache: "no-store",
          }
        );
        if (res.ok) {
          const data = await res.json();
          setPendingApprovals(data.items || []);
        }
      } catch {
        // tolerate background loading failures
      }
    }
    loadPending();
  }, []);

  return (
    <div className="space-y-8">
      {/* Welcome Banner */}
      <div className="rounded-2xl border border-slate-200 bg-white p-6 sm:p-8 shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-semibold text-emerald-700">
                ● Platform Operator Session Active
              </span>
            </div>
            <h1 className="mt-2 font-display text-2xl font-bold text-navy-800 sm:text-3xl">
              Platform Administration
            </h1>
            <p className="mt-1 text-sm text-slate-500">
              Welcome back, <span className="font-semibold text-slate-700">{identity.username || "Super Admin"}</span>. You have unrestricted platform-level administrative authority.
            </p>
          </div>
          <Link
            href="/superadmin/communities"
            className="inline-flex items-center justify-center rounded-lg bg-navy-700 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-navy-800 transition-colors"
          >
            Manage Communities
          </Link>
        </div>
      </div>

      {/* Action Required: Pending Approvals Widget */}
      {pendingApprovals.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/70 p-5 shadow-sm">
          <div className="flex items-center justify-between gap-4 mb-3">
            <div className="flex items-center gap-2.5">
              <span className="flex h-3 w-3 relative">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-3 w-3 bg-amber-500"></span>
              </span>
              <h2 className="font-display text-base font-bold text-amber-900">
                Action Required: Pending Agent Approvals ({pendingApprovals.length})
              </h2>
            </div>
            <span className="text-xs font-medium text-amber-800">
              SEC-03 Dual Custody Review
            </span>
          </div>
          <div className="divide-y divide-amber-200/60 border-t border-amber-200/60">
            {pendingApprovals.map((item) => (
              <div key={item.id} className="py-3 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-slate-800">{item.title}</p>
                  <p className="text-xs text-slate-600 mt-0.5">{item.message}</p>
                  <p className="text-[11px] text-slate-400 mt-1">{new Date(item.created_at).toLocaleString()}</p>
                </div>
                {item.agent_profile_id && (
                  <Link
                    href={`/superadmin/employees/${item.agent_profile_id}`}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-navy-800 bg-white border border-amber-300 rounded-lg shadow-xs hover:bg-amber-100/50 transition-colors"
                  >
                    Review Profile & Approve →
                  </Link>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Dual-Custody Child Agent Approvals Queue */}
      <AgentCreationApprovalInbox role="SUPER_ADMIN" title="Dual-Custody Child Agent Approvals" />

      {/* Summary Cards */}
      <div>
        <h2 className="font-display text-base font-semibold text-navy-800 mb-4">
          Platform Overview
        </h2>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="Pending Approvals"
            value={pendingApprovals.length}
            sub={pendingApprovals.length > 0 ? "Awaiting Super Admin review" : "All approvals up to date"}
          />
          <StatCard
            label="Platform Scope"
            value="Global"
            sub="Platform-wide administrative access"
          />
          <StatCard
            label="Session Security"
            value="Protected"
            sub="HttpOnly · SameSite=Lax"
          />
          <StatCard
            label="Community Governance"
            value="Multi-Tenant"
            sub="Domain-proof onboarding engine"
          />
        </div>
      </div>

      {/* Target Management & Administrator Performance */}
      <SuperAdminTargetManager />

      {/* Authenticated Identity & Context */}
      <div className="grid lg:grid-cols-3 gap-6">
        <Card className="p-6 lg:col-span-2">
          <h2 className="font-display font-semibold text-navy-800 mb-4">
            Authenticated Server Context
          </h2>
          <p className="text-sm text-slate-500 mb-6">
            Identity claims confirmed by the FastAPI backend (<code className="text-xs bg-slate-100 px-1 py-0.5 rounded font-mono">/api/v1/auth/me</code>).
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
              <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Operator Principal ID</span>
              <span className="font-mono text-xs text-slate-700 mt-1 block truncate" title={identity.user_id}>
                {identity.user_id}
              </span>
            </div>

            <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
              <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Account Status</span>
              <span className="font-semibold text-slate-800 mt-1 block capitalize">{identity.status}</span>
            </div>

            <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5 sm:col-span-2">
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
            Platform Tools
          </h2>
          <div className="space-y-3">
            <Link
              href="/superadmin/communities"
              className="flex items-center justify-between p-3 rounded-lg border border-slate-200 hover:border-slate-300 hover:bg-slate-50 transition-colors"
            >
              <div>
                <p className="text-sm font-semibold text-navy-800">Community Onboarding</p>
                <p className="text-xs text-slate-500">Review pending domain challenges & activations</p>
              </div>
              <span className="text-slate-400">→</span>
            </Link>

            <Link
              href="/superadmin/activity"
              className="flex items-center justify-between p-3 rounded-lg border border-slate-200 hover:border-slate-300 hover:bg-slate-50 transition-colors"
            >
              <div>
                <p className="text-sm font-semibold text-navy-800">System Activity</p>
                <p className="text-xs text-slate-500">Platform operational audit event trail</p>
              </div>
              <span className="text-slate-400">→</span>
            </Link>
          </div>
        </Card>
      </div>
    </div>
  );
}
