"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/components/AuthGuard";
import { Card, Badge, Button } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";

interface DescendantSummary {
  member_id: string;
  user_id: string;
  depth: number;
  parent_member_id: string;
  relationship_type: string;
  status: string;
  created_at: string;
  mobile_masked?: string | null;
  full_name?: string | null;
  profile_type: string;
}

interface DescendantStats {
  total_descendants: number;
  direct_children_count: number;
  counts_by_depth: Record<string, number>;
  counts_by_status: Record<string, number>;
  counts_by_type: Record<string, number>;
}

export default function AgentCustomersPage() {
  const { identity } = useAuth();
  const tenantSlug = identity.tenant?.slug || "Community Field Office";

  const [descendants, setDescendants] = useState<DescendantSummary[]>([]);
  const [stats, setStats] = useState<DescendantStats | null>(null);
  const [selectedDepth, setSelectedDepth] = useState<number | null>(null); // null = all
  const [selectedType, setSelectedType] = useState<string>("REPORTS_TO");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const pageSize = 25;

  const loadData = useCallback(() => {
    setLoading(true);
    setError(null);

    const query = new URLSearchParams({
      relationship_type: selectedType,
      max_depth: "3",
      limit: String(pageSize),
      offset: String(page * pageSize),
    });

    Promise.all([
      fetchBackendApi(`api/v1/agent/descendants?${query.toString()}`, {
        credentials: "same-origin",
        cache: "no-store",
      }),
      fetchBackendApi(`api/v1/agent/descendants/stats?relationship_type=${selectedType}&max_depth=3`, {
        credentials: "same-origin",
        cache: "no-store",
      }),
    ])
      .then(async ([rosterRes, statsRes]) => {
        if (!rosterRes.ok) {
          const err = await rosterRes.json().catch(() => ({}));
          throw new Error(err.message || "Failed to load customer roster.");
        }
        const rosterData = await rosterRes.json();
        setDescendants(rosterData.descendants || []);

        if (statsRes.ok) {
          const statsData = await statsRes.json();
          setStats(statsData);
        }
      })
      .catch((err) => {
        setError(err.message || "An error occurred while loading customers.");
      })
      .finally(() => setLoading(false));
  }, [page, selectedType]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const filteredDescendants = selectedDepth !== null
    ? descendants.filter((d) => d.depth === selectedDepth)
    : descendants;

  return (
    <div className="space-y-6">
      {/* Header Banner */}
      <div className="rounded-2xl border border-slate-200 bg-white p-6 sm:p-8 shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-semibold text-emerald-700">
                ● Community OTT Network
              </span>
              <span className="inline-flex items-center rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700 font-mono">
                {tenantSlug}
              </span>
            </div>
            <h1 className="mt-2 font-display text-2xl font-bold text-navy-800 sm:text-3xl">
              Subscriber & Member Hierarchy
            </h1>
            <p className="mt-1 text-sm text-slate-500">
              Manage your direct subscribers and multi-tier organizational tree.
            </p>
          </div>
          <Link
            href="/agent/register"
            className="inline-flex items-center justify-center rounded-lg bg-emerald-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-emerald-700 transition-colors"
          >
            + Register Subscriber
          </Link>
        </div>
      </div>

      {/* Network Stats Bar */}
      {stats && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
            <span className="text-xs font-medium text-slate-500 uppercase tracking-wider">Total Network Size</span>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-display text-2xl font-bold text-navy-800">{stats.total_descendants}</span>
              <span className="text-xs text-slate-400">members</span>
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
            <span className="text-xs font-medium text-slate-500 uppercase tracking-wider">Direct (Tier 1)</span>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-display text-2xl font-bold text-emerald-600">{stats.direct_children_count}</span>
              <span className="text-xs text-slate-400">direct registrations</span>
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
            <span className="text-xs font-medium text-slate-500 uppercase tracking-wider">Tier 2 Sub-Network</span>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-display text-2xl font-bold text-blue-600">{stats.counts_by_depth?.["2"] || 0}</span>
              <span className="text-xs text-slate-400">members</span>
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card">
            <span className="text-xs font-medium text-slate-500 uppercase tracking-wider">Tier 3 Sub-Network</span>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="font-display text-2xl font-bold text-indigo-600">{stats.counts_by_depth?.["3"] || 0}</span>
              <span className="text-xs text-slate-400">members</span>
            </div>
          </div>
        </div>
      )}

      {/* Filter and Depth Tabs */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 pb-4">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setSelectedDepth(null)}
            className={`rounded-lg px-3.5 py-1.5 text-xs font-semibold transition-colors ${
              selectedDepth === null
                ? "bg-navy-800 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            All Tiers
          </button>
          <button
            onClick={() => setSelectedDepth(1)}
            className={`rounded-lg px-3.5 py-1.5 text-xs font-semibold transition-colors ${
              selectedDepth === 1
                ? "bg-emerald-600 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            Direct (L1)
          </button>
          <button
            onClick={() => setSelectedDepth(2)}
            className={`rounded-lg px-3.5 py-1.5 text-xs font-semibold transition-colors ${
              selectedDepth === 2
                ? "bg-blue-600 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            Tier 2
          </button>
          <button
            onClick={() => setSelectedDepth(3)}
            className={`rounded-lg px-3.5 py-1.5 text-xs font-semibold transition-colors ${
              selectedDepth === 3
                ? "bg-indigo-600 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            Tier 3
          </button>
        </div>

        <div className="flex items-center gap-2">
          <label htmlFor="relationship-type-select" className="text-xs text-slate-500 font-medium">Hierarchy Type:</label>
          <select
            id="relationship-type-select"
            aria-label="Hierarchy Type"
            value={selectedType}
            onChange={(e) => {
              setSelectedType(e.target.value);
              setPage(0);
            }}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm focus:border-emerald-500 focus:outline-none"
          >
            <option value="REPORTS_TO">Operational (REPORTS_TO)</option>
            <option value="REFERRED_BY">Referral Attributions (REFERRED_BY)</option>
            <option value="MANAGES">Management (MANAGES)</option>
          </select>
        </div>
      </div>

      {/* Error Alert */}
      {error && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
          <strong>Error:</strong> {error}
        </div>
      )}

      {/* Data Table */}
      <Card>
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-slate-100 text-xs font-semibold uppercase text-slate-400">
                <th className="py-3 px-4">Member Name</th>
                <th className="py-3 px-4">Tier Depth</th>
                <th className="py-3 px-4">Relationship</th>
                <th className="py-3 px-4">Masked Mobile</th>
                <th className="py-3 px-4">Status</th>
                <th className="py-3 px-4">Joined Date</th>
                <th className="py-3 px-4 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-sm">
              {loading ? (
                <tr>
                  <td colSpan={7} className="py-8 text-center text-slate-400">
                    Loading hierarchy members...
                  </td>
                </tr>
              ) : filteredDescendants.length === 0 ? (
                <tr>
                  <td colSpan={7} className="py-12 text-center text-slate-400">
                    No members found in this network tier.
                  </td>
                </tr>
              ) : (
                filteredDescendants.map((member) => (
                  <tr key={member.member_id} className="hover:bg-slate-50/60 transition-colors">
                    <td className="py-3.5 px-4">
                      <div className="font-medium text-navy-800">
                        {member.full_name || "Community Member"}
                      </div>
                      <div className="text-xs text-slate-400 font-mono">
                        {member.member_id.slice(0, 8)}...
                      </div>
                    </td>
                    <td className="py-3.5 px-4">
                      <span
                        className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${
                          member.depth === 1
                            ? "bg-emerald-50 text-emerald-700"
                            : member.depth === 2
                            ? "bg-blue-50 text-blue-700"
                            : "bg-indigo-50 text-indigo-700"
                        }`}
                      >
                        Tier {member.depth} {member.depth === 1 ? "(Direct)" : ""}
                      </span>
                    </td>
                    <td className="py-3.5 px-4 text-xs font-mono text-slate-600">
                      {member.relationship_type}
                    </td>
                    <td className="py-3.5 px-4 font-mono text-xs text-slate-600">
                      {member.mobile_masked || "—"}
                    </td>
                    <td className="py-3.5 px-4">
                      <Badge tone={member.status === "active" ? "emerald" : "slate"}>
                        {member.status}
                      </Badge>
                    </td>
                    <td className="py-3.5 px-4 text-xs text-slate-500">
                      {new Date(member.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-3.5 px-4 text-right">
                      <Link href={`/agent/customers/${encodeURIComponent(member.member_id)}`}>
                        <Button variant="secondary" size="sm">
                          Profile
                        </Button>
                      </Link>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination controls */}
        <div className="flex items-center justify-between border-t border-slate-100 px-4 py-3 text-xs text-slate-500">
          <span>
            Showing {filteredDescendants.length} results
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page === 0 || loading}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              Previous
            </Button>
            <span className="font-mono">Page {page + 1}</span>
            <Button
              variant="outline"
              size="sm"
              disabled={filteredDescendants.length < pageSize || loading}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
