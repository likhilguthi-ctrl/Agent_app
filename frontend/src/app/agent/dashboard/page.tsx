"use client";

import React, { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useAuth } from "@/components/AuthGuard";
import { Card, StatCard, Badge, RefChip, Button } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";
import { AgentTargetCards } from "@/components/targets/AgentTargetCards";
import AgentCreationRequestModal from "@/components/AgentCreationRequestModal";
import CompleteChildAgentRegistrationModal, {
  ApprovedCreationRequest,
} from "@/components/CompleteChildAgentRegistrationModal";

interface PolicyInfo {
  tenant_id: string;
  community_slug: string;
  photo_requirement: "OPTIONAL" | "REQUIRED";
  default_commission_rate: number;
  policy_version: number;
}

interface CollectionSummary {
  total_subscribers: number;
  active_subscriptions: number;
  total_collected_amount: number | string;
  currency: string;
  target_amount?: number | string | null;
  target_achievement_percentage?: number | null;
}

interface SubscriberSummary {
  id: string;
  tenant_id: string;
  community_slug: string;
  membership_id: string;
  user_id: string;
  full_name: string;
  mobile_e164: string;
  masked_mobile?: string | null;
  whatsapp_number?: string | null;
  whatsapp_verified: boolean;
  whatsapp_consent: boolean;
  email?: string | null;
  email_verified: boolean;
  address?: string | null;
  photo_key?: string | null;
  package_plan?: string | null;
  package_type?: string | null;
  status: string;
  payment_status?: string | null;
  subscription_status?: string | null;
  payment_amount?: number | string | null;
  created_at: string;
}

export default function AgentDashboard() {
  const { identity } = useAuth();
  const tenantSlug = identity.tenant?.slug || "Community Field Office";

  const [policy, setPolicy] = useState<PolicyInfo | null>(null);
  const [subscribers, setSubscribers] = useState<SubscriberSummary[]>([]);
  const [collectionStats, setCollectionStats] = useState<CollectionSummary | null>(null);
  const [networkStats, setNetworkStats] = useState<{ total_descendants: number; direct_children_count: number } | null>(null);
  const [creationRequests, setCreationRequests] = useState<any[]>([]);
  const [isRequestModalOpen, setIsRequestModalOpen] = useState(false);
  const [selectedApprovedRequest, setSelectedApprovedRequest] = useState<ApprovedCreationRequest | null>(null);
  const [loading, setLoading] = useState(true);

  const loadCreationRequests = useCallback(async () => {
    try {
      const res = await fetchBackendApi("api/v1/agent/creation-requests", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (res.ok) {
        const data = await res.json();
        setCreationRequests(data.items || []);
      }
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    loadCreationRequests();
    Promise.all([
      fetchBackendApi("api/v1/agent/policy", { credentials: "same-origin", cache: "no-store" }),
      fetchBackendApi("api/v1/agent/users", { credentials: "same-origin", cache: "no-store" }),
      fetchBackendApi("api/v1/agent/collections/summary", { credentials: "same-origin", cache: "no-store" }),
      fetchBackendApi("api/v1/agent/descendants/stats", { credentials: "same-origin", cache: "no-store" }),
    ])
      .then(async ([polRes, subRes, colRes, statsRes]) => {
        if (polRes.ok) {
          const polData = await polRes.json();
          setPolicy(polData);
        }
        if (subRes.ok) {
          const subData = await subRes.json();
          setSubscribers(subData.items || []);
        }
        if (colRes.ok) {
          const colData = await colRes.json();
          setCollectionStats(colData);
        }
        if (statsRes.ok) {
          const statsData = await statsRes.json();
          setNetworkStats(statsData);
        }
      })
      .catch(() => {
        // ignore
      })
      .finally(() => setLoading(false));
  }, [loadCreationRequests]);

  const totalCollected = parseFloat(String(collectionStats?.total_collected_amount || "0"));
  const targetGoal = parseFloat(String(collectionStats?.target_amount || "50000"));
  const achievementPct = collectionStats?.target_achievement_percentage ?? (targetGoal > 0 ? Math.min((totalCollected / targetGoal) * 100, 100) : 0);

  return (
    <div className="space-y-8">
      {/* Welcome Banner */}
      <div className="rounded-2xl border border-slate-200 bg-white p-6 sm:p-8 shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-semibold text-emerald-700">
                ● Field Agent Session Active
              </span>
              <span className="inline-flex items-center rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700 font-mono">
                {tenantSlug}
              </span>
              {policy && (
                <span className="inline-flex items-center rounded-full bg-indigo-50 px-2.5 py-0.5 text-xs font-medium text-indigo-700">
                  Photo Policy: {policy.photo_requirement}
                </span>
              )}
            </div>
            <h1 className="mt-2 font-display text-2xl font-bold text-navy-800 sm:text-3xl">
              Field Operations Dashboard
            </h1>
            <p className="mt-1 text-sm text-slate-500">
              Welcome back, <span className="font-semibold text-slate-700">{identity.username || "Field Agent"}</span>. Operating in <span className="font-semibold text-navy-800">{tenantSlug}</span>.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="secondary"
              onClick={() => setIsRequestModalOpen(true)}
              className="text-xs font-semibold"
            >
              + Request Child Agent Creation
            </Button>
            <Link
              href="/agent/register"
              className="inline-flex items-center justify-center rounded-lg bg-emerald-600 px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-emerald-700 transition-colors"
            >
              + Register Subscriber
            </Link>
          </div>
        </div>
      </div>

      {/* Authoritative PostgreSQL Collection Progress Bar */}
      <Card className="p-6 border-slate-200 bg-gradient-to-br from-white to-slate-50 space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <div>
            <span className="text-xs font-bold uppercase tracking-wider text-emerald-700">
              Authoritative PostgreSQL Collection Performance
            </span>
            <h2 className="text-lg font-bold text-navy-900 mt-0.5">
              Monthly Subscription Revenue Progress
            </h2>
          </div>
          <div className="text-right">
            <span className="text-2xl font-extrabold text-navy-950 font-display">
              ₹{totalCollected.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
            </span>
            <span className="text-xs text-slate-500 block">
              of ₹{targetGoal.toLocaleString("en-IN")} Target
            </span>
          </div>
        </div>

        {/* Progress Bar Container */}
        <div>
          <div className="flex justify-between items-center text-xs font-semibold text-slate-600 mb-1.5">
            <span>Progress: {achievementPct.toFixed(1)}% Achieved</span>
            <span>{collectionStats?.active_subscriptions || 0} Active Subscriptions</span>
          </div>
          <div className="w-full h-3.5 bg-slate-200 rounded-full overflow-hidden p-0.5">
            <div
              className="h-full bg-gradient-to-r from-emerald-500 to-teal-600 rounded-full transition-all duration-500"
              style={{ width: `${Math.min(achievementPct, 100)}%` }}
            />
          </div>
        </div>
      </Card>

      {/* Summary Cards */}
      <div>
        <h2 className="font-display text-base font-semibold text-navy-800 mb-4">
          Field Status Overview
        </h2>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="Assigned Territory"
            value={tenantSlug}
            sub="Community-scoped operations"
          />
          <StatCard
            label="Direct Subscribers"
            value={loading ? "…" : String(subscribers.length)}
            sub="Direct registered households"
          />
          <StatCard
            label="Total Collected"
            value={loading ? "…" : `₹${totalCollected.toLocaleString("en-IN")}`}
            sub="Settled payment orders"
          />
          <StatCard
            label="Commission Benchmark"
            value={policy ? `${policy.default_commission_rate}%` : "10.00%"}
            sub="Community default rate"
          />
        </div>
      </div>

      {/* Target Goals & Achievements */}
      <AgentTargetCards />

      {/* Recent Subscribers Registered By Agent */}
      <Card className="p-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-5">
          <div>
            <h2 className="text-base font-bold font-display text-navy-900">
              Your Enrolled Subscribers ({subscribers.length})
            </h2>
            <p className="text-xs text-slate-600 mt-0.5">
              Subscribers registered in {tenantSlug} with payment and subscription status.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Link href="/agent/customers">
              <Button size="sm" variant="outline">View Network Hierarchy →</Button>
            </Link>
            <Link href="/agent/register">
              <Button size="sm">+ Register Subscriber</Button>
            </Link>
          </div>
        </div>

        {subscribers.length === 0 ? (
          <div className="text-center py-10 border border-dashed border-slate-200 rounded-xl">
            <p className="text-sm font-medium text-slate-600">No subscribers registered yet.</p>
            <p className="text-xs text-slate-400 mt-1">
              Start registering community members to activate their OTT access.
            </p>
            <div className="mt-4">
              <Link href="/agent/register">
                <Button size="sm">+ Register First Subscriber</Button>
              </Link>
            </div>
          </div>
        ) : (
          <div className="overflow-x-auto border border-slate-200 rounded-lg">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 border-b border-slate-200 text-slate-600 font-semibold text-xs uppercase">
                <tr>
                  <th className="px-4 py-3">Subscriber Name</th>
                  <th className="px-4 py-3">Masked Mobile</th>
                  <th className="px-4 py-3">Package / Type</th>
                  <th className="px-4 py-3">Amount</th>
                  <th className="px-4 py-3">Payment Status</th>
                  <th className="px-4 py-3">Subscription</th>
                  <th className="px-4 py-3">WhatsApp Consent</th>
                  <th className="px-4 py-3">Enrolled Date</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {subscribers.map((sub) => (
                  <tr key={sub.id} className="hover:bg-slate-50 transition">
                    <td className="px-4 py-3 font-semibold text-navy-900">
                      {sub.full_name}
                      {sub.email && <span className="block text-xs font-normal text-slate-400">{sub.email}</span>}
                    </td>
                    <td className="px-4 py-3 font-mono text-slate-700 text-xs">
                      {sub.masked_mobile || sub.mobile_e164}
                    </td>
                    <td className="px-4 py-3">
                      <span className="font-medium text-slate-800 text-xs block">{sub.package_plan || "Standard"}</span>
                      {sub.package_type && (
                        <span className="text-[10px] text-slate-500 uppercase font-semibold">
                          {sub.package_type}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 font-semibold text-slate-900 text-xs">
                      ₹{sub.payment_amount ? parseFloat(String(sub.payment_amount)).toFixed(2) : "0.00"}
                    </td>
                    <td className="px-4 py-3">
                      <Badge tone={sub.payment_status === "paid" ? "emerald" : sub.payment_status === "failed" ? "red" : "amber"}>
                        {(sub.payment_status || "pending").toUpperCase()}
                      </Badge>
                    </td>
                    <td className="px-4 py-3">
                      <Badge tone={sub.subscription_status === "ACTIVE" ? "emerald" : "slate"}>
                        {sub.subscription_status || "PENDING"}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-xs">
                      {sub.whatsapp_consent ? (
                        <span className="text-emerald-700 font-semibold flex items-center gap-1">
                          ✓ Consented
                        </span>
                      ) : (
                        <span className="text-slate-400">Opted Out</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-500">
                      {new Date(sub.created_at).toLocaleDateString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Child Agent Creation Requests Card */}
      <Card className="p-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-base font-bold font-display text-navy-900">
              Child Agent Creation Requests ({creationRequests.length})
            </h2>
            <p className="text-xs text-slate-600 mt-0.5">
              Dual-custody creation requests submitted to your Community Manager and Super Admin.
            </p>
          </div>
          <Button size="sm" onClick={() => setIsRequestModalOpen(true)}>
            + New Child Agent Request
          </Button>
        </div>

        {creationRequests.length === 0 ? (
          <div className="text-center py-8 border border-dashed border-slate-200 rounded-xl text-slate-500 text-xs">
            <p className="font-medium text-slate-600">No child agent requests submitted yet.</p>
            <p className="mt-0.5 text-slate-400">You can expand your regional operations by submitting a child agent creation request.</p>
          </div>
        ) : (
          <div className="overflow-x-auto border border-slate-200 rounded-lg">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 border-b border-slate-200 text-slate-600 font-semibold text-xs uppercase">
                <tr>
                  <th className="px-4 py-3">Req Number</th>
                  <th className="px-4 py-3">Candidate Location</th>
                  <th className="px-4 py-3">Business Justification</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Created</th>
                  <th className="px-4 py-3">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {creationRequests.map((req) => (
                  <tr key={req.id} className="hover:bg-slate-50 transition">
                    <td className="px-4 py-3 font-mono font-bold text-xs text-navy-900">
                      {req.request_number}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-800">
                      <span className="font-medium">{req.mandal_name}</span>
                      <span className="text-slate-400 block">{req.district_name}, {req.state_name}</span>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-600 max-w-xs truncate" title={req.reason}>
                      {req.reason}
                    </td>
                    <td className="px-4 py-3">
                      <Badge
                        tone={
                          req.status === "APPROVED"
                            ? "emerald"
                            : req.status === "FULFILLED"
                            ? "navy"
                            : req.status === "REJECTED"
                            ? "red"
                            : "amber"
                        }
                      >
                        {req.status}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-500">
                      {new Date(req.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3 text-xs">
                      {req.status === "APPROVED" ? (
                        <button
                          type="button"
                          onClick={() => setSelectedApprovedRequest(req)}
                          className="inline-flex items-center px-2.5 py-1 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-md hover:bg-emerald-100 transition cursor-pointer"
                        >
                          + Complete Onboarding
                        </button>
                      ) : req.status === "FULFILLED" ? (
                        <span className="text-indigo-600 font-medium">✓ Fulfilled</span>
                      ) : req.status === "REJECTED" ? (
                        <span className="text-red-500 font-medium">Rejected</span>
                      ) : (
                        <span className="text-amber-600 font-medium">In Review</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Authenticated Identity & Permissions */}
      <Card className="p-6">
        <h2 className="font-display font-semibold text-navy-800 mb-4">
          Field Agent Identity Claims
        </h2>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4 text-sm">
          <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
            <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Username</span>
            <span className="font-semibold text-navy-800 mt-1 block">{identity.username}</span>
          </div>

          <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
            <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Role</span>
            <span className="font-semibold text-emerald-700 mt-1 block">{identity.role}</span>
          </div>

          <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
            <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Territory</span>
            <span className="font-semibold text-navy-800 mt-1 block font-mono text-xs">{tenantSlug}</span>
          </div>

          <div className="rounded-lg border border-slate-100 bg-sand-50/50 p-3.5">
            <span className="text-xs text-slate-500 block uppercase tracking-wider font-medium">Session Expiration</span>
            <span className="font-medium text-slate-700 mt-1 block text-xs">
              {new Date(identity.expires_at).toLocaleString()}
            </span>
          </div>
        </div>
      </Card>

      {/* Modals */}
      <AgentCreationRequestModal
        isOpen={isRequestModalOpen}
        onClose={() => setIsRequestModalOpen(false)}
        onSuccess={() => {
          loadCreationRequests();
          setIsRequestModalOpen(false);
        }}
      />

      <CompleteChildAgentRegistrationModal
        request={selectedApprovedRequest}
        isOpen={!!selectedApprovedRequest}
        onClose={() => setSelectedApprovedRequest(null)}
        onSuccess={() => {
          setSelectedApprovedRequest(null);
          loadCreationRequests();
        }}
      />
    </div>
  );
}
