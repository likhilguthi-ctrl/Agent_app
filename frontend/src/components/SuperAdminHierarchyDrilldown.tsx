"use client";

import React, { useEffect, useState, useCallback } from "react";
import { Card, Button, Input, Badge, RefChip, Spinner } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";

interface ManagerItem {
  user_id: string;
  membership_id: string;
  first_name?: string | null;
  last_name?: string | null;
  username: string;
  mobile_e164: string;
  status: string;
  states: string[];
  direct_agents_count: number;
  total_subscribers_count: number;
  created_at: string;
}

interface AgentItem {
  agent_profile_id: string;
  user_id: string;
  membership_id: string;
  ref_id: string;
  first_name: string;
  last_name: string;
  username: string;
  mobile_e164: string;
  status: string;
  state?: string | null;
  district?: string | null;
  mandal?: string | null;
  subscribers_count: number;
  created_at: string;
}

interface SubscriberItem {
  subscriber_profile_id: string;
  full_name: string;
  mobile_e164: string;
  package_plan?: string | null;
  package_type?: string | null;
  status: string;
  payment_status?: string | null;
  subscription_status?: string | null;
  created_at: string;
}

interface SuperAdminHierarchyDrilldownProps {
  communityId: string;
  communitySlug: string;
}

export default function SuperAdminHierarchyDrilldown({
  communityId,
  communitySlug,
}: SuperAdminHierarchyDrilldownProps) {
  // Navigation level: 1 = Managers, 2 = Agents under Manager, 3 = Subscribers under Agent
  const [level, setLevel] = useState<1 | 2 | 3>(1);

  // Selected entities for drill-down context
  const [selectedManager, setSelectedManager] = useState<ManagerItem | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<AgentItem | null>(null);

  // Pagination & Filtering state
  const [page, setPage] = useState(1);
  const [pageSize] = useState(10);
  const [search, setSearch] = useState("");
  const [searchInput, setSearchInput] = useState("");

  // Data lists
  const [managers, setManagers] = useState<ManagerItem[]>([]);
  const [agents, setAgents] = useState<AgentItem[]>([]);
  const [subscribers, setSubscribers] = useState<SubscriberItem[]>([]);

  const [totalCount, setTotalCount] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 1. Fetch Level 1: Managers
  const fetchManagers = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const q = new URLSearchParams({
        page: String(page),
        page_size: String(pageSize),
      });
      if (search.trim()) q.set("search", search.trim());

      const res = await fetchBackendApi(
        `api/v1/platform/communities/${communityId}/managers?${q.toString()}`,
        { credentials: "same-origin", cache: "no-store" }
      );
      if (!res.ok) throw new Error("Failed to load community managers");
      const data = await res.json();
      setManagers(data.items || []);
      setTotalCount(data.total || 0);
      setTotalPages(data.total_pages || 1);
    } catch (err: any) {
      setError(err?.message || "Failed to load managers");
      setManagers([]);
    } finally {
      setIsLoading(false);
    }
  }, [communityId, page, pageSize, search]);

  // 2. Fetch Level 2: Agents under Manager
  const fetchAgents = useCallback(async () => {
    if (!selectedManager) return;
    setIsLoading(true);
    setError(null);
    try {
      const q = new URLSearchParams({
        page: String(page),
        page_size: String(pageSize),
      });
      if (search.trim()) q.set("search", search.trim());

      const res = await fetchBackendApi(
        `api/v1/platform/communities/${communityId}/managers/${selectedManager.user_id}/agents?${q.toString()}`,
        { credentials: "same-origin", cache: "no-store" }
      );
      if (!res.ok) throw new Error("Failed to load agents under manager");
      const data = await res.json();
      setAgents(data.items || []);
      setTotalCount(data.total || 0);
      setTotalPages(data.total_pages || 1);
    } catch (err: any) {
      setError(err?.message || "Failed to load agents");
      setAgents([]);
    } finally {
      setIsLoading(false);
    }
  }, [communityId, selectedManager, page, pageSize, search]);

  // 3. Fetch Level 3: Subscribers under Agent
  const fetchSubscribers = useCallback(async () => {
    if (!selectedAgent) return;
    setIsLoading(true);
    setError(null);
    try {
      const q = new URLSearchParams({
        page: String(page),
        page_size: String(pageSize),
      });
      if (search.trim()) q.set("search", search.trim());

      const res = await fetchBackendApi(
        `api/v1/platform/communities/${communityId}/agents/${selectedAgent.agent_profile_id}/subscribers?${q.toString()}`,
        { credentials: "same-origin", cache: "no-store" }
      );
      if (!res.ok) throw new Error("Failed to load subscribers");
      const data = await res.json();
      setSubscribers(data.items || []);
      setTotalCount(data.total || 0);
      setTotalPages(data.total_pages || 1);
    } catch (err: any) {
      setError(err?.message || "Failed to load subscribers");
      setSubscribers([]);
    } finally {
      setIsLoading(false);
    }
  }, [communityId, selectedAgent, page, pageSize, search]);

  // Trigger appropriate level fetch
  useEffect(() => {
    if (level === 1) {
      fetchManagers();
    } else if (level === 2) {
      fetchAgents();
    } else if (level === 3) {
      fetchSubscribers();
    }
  }, [level, fetchManagers, fetchAgents, fetchSubscribers]);

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setPage(1);
    setSearch(searchInput);
  };

  const navigateToLevel1 = () => {
    setLevel(1);
    setSelectedManager(null);
    setSelectedAgent(null);
    setPage(1);
    setSearch("");
    setSearchInput("");
  };

  const navigateToLevel2 = (mgr: ManagerItem) => {
    setSelectedManager(mgr);
    setSelectedAgent(null);
    setLevel(2);
    setPage(1);
    setSearch("");
    setSearchInput("");
  };

  const navigateToLevel3 = (ag: AgentItem) => {
    setSelectedAgent(ag);
    setLevel(3);
    setPage(1);
    setSearch("");
    setSearchInput("");
  };

  return (
    <Card className="p-6 border-navy-100 shadow-sm space-y-5">
      {/* Header & Breadcrumb Path */}
      <div>
        <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-slate-100">
          <div>
            <h3 className="text-base font-bold font-display text-navy-950 flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse" />
              Community Organizational Hierarchy (3-Level Drill-Down)
            </h3>
            <p className="text-xs text-slate-500 mt-0.5">
              Explore Level 1 (Managers) → Level 2 (Agents) → Level 3 (Subscribers) for {communitySlug}.
            </p>
          </div>
          <Badge tone="navy">SEC-04 Tenant Isolation</Badge>
        </div>

        {/* Dynamic Breadcrumbs */}
        <nav aria-label="Hierarchy Breadcrumbs" className="flex items-center gap-2 text-xs font-semibold mt-3">
          <button
            type="button"
            onClick={navigateToLevel1}
            className={`transition ${
              level === 1 ? "text-navy-900 font-bold" : "text-emerald-700 hover:underline"
            }`}
          >
            Level 1: Community Managers ({communitySlug})
          </button>

          {level >= 2 && selectedManager && (
            <>
              <span className="text-slate-400">/</span>
              <button
                type="button"
                onClick={() => {
                  setLevel(2);
                  setSelectedAgent(null);
                  setPage(1);
                }}
                className={`transition ${
                  level === 2 ? "text-navy-900 font-bold" : "text-emerald-700 hover:underline"
                }`}
              >
                Level 2: {selectedManager.first_name || "Manager"} {selectedManager.last_name || ""} (Agents)
              </button>
            </>
          )}

          {level === 3 && selectedAgent && (
            <>
              <span className="text-slate-400">/</span>
              <span className="text-navy-900 font-bold">
                Level 3: {selectedAgent.first_name} {selectedAgent.last_name} ({selectedAgent.ref_id}) Subscribers
              </span>
            </>
          )}
        </nav>
      </div>

      {/* Search Bar & Pagination Stats */}
      <div className="flex flex-col sm:flex-row items-center justify-between gap-3">
        <form onSubmit={handleSearchSubmit} className="flex items-center gap-2 w-full sm:w-auto">
          <Input
            placeholder={`Search ${level === 1 ? "managers" : level === 2 ? "agents" : "subscribers"}...`}
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            className="w-full sm:w-64 text-xs"
          />
          <Button type="submit" size="sm" variant="secondary">
            Search
          </Button>
          {search && (
            <button
              type="button"
              onClick={() => {
                setSearch("");
                setSearchInput("");
                setPage(1);
              }}
              className="text-xs text-slate-400 hover:text-slate-600 underline"
            >
              Clear
            </button>
          )}
        </form>

        <div className="text-xs text-slate-500 flex items-center gap-2">
          <span>Total records: <strong className="text-navy-900">{totalCount}</strong></span>
          <span>•</span>
          <span>Page {page} of {totalPages}</span>
        </div>
      </div>

      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 p-3 text-xs text-red-700">
          {error}
        </div>
      )}

      {/* TABLE VIEWS */}
      {isLoading ? (
        <div className="py-12 flex flex-col items-center justify-center text-slate-400 gap-2">
          <Spinner />
          <p className="text-xs">Loading hierarchy records...</p>
        </div>
      ) : (
        <>
          {/* LEVEL 1: MANAGERS */}
          {level === 1 && (
            managers.length === 0 ? (
              <div className="text-center py-8 border border-dashed border-slate-200 rounded-lg text-slate-500 text-xs">
                No managers found in this community.
              </div>
            ) : (
              <div className="overflow-x-auto border border-slate-200 rounded-lg">
                <table className="w-full text-left text-xs">
                  <thead className="bg-slate-50 text-slate-600 font-semibold uppercase border-b border-slate-200">
                    <tr>
                      <th className="px-4 py-2.5">Manager</th>
                      <th className="px-4 py-2.5">Contact</th>
                      <th className="px-4 py-2.5">Assigned States</th>
                      <th className="px-4 py-2.5 text-center">Managed Agents</th>
                      <th className="px-4 py-2.5 text-center">Total Subscribers</th>
                      <th className="px-4 py-2.5">Status</th>
                      <th className="px-4 py-2.5 text-right">Drill Down</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {managers.map((m) => (
                      <tr key={m.user_id} className="hover:bg-slate-50 transition">
                        <td className="px-4 py-3 font-medium">
                          <p className="text-navy-900 font-semibold">{m.first_name || "Manager"} {m.last_name || ""}</p>
                          <p className="text-[11px] font-mono text-slate-400">@{m.username}</p>
                        </td>
                        <td className="px-4 py-3 text-slate-600">
                          {m.mobile_e164}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex flex-wrap gap-1">
                            {m.states && m.states.length > 0 ? (
                              m.states.map((st) => (
                                <span key={st} className="px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 text-[10px] font-medium">
                                  {st}
                                </span>
                              ))
                            ) : (
                              <span className="text-slate-400 text-[10px]">Unassigned</span>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-center">
                          <Badge tone={m.direct_agents_count > 0 ? "emerald" : "slate"}>
                            {m.direct_agents_count} Agents
                          </Badge>
                        </td>
                        <td className="px-4 py-3 text-center font-bold text-navy-800">
                          {m.total_subscribers_count}
                        </td>
                        <td className="px-4 py-3">
                          <Badge tone={m.status === "active" ? "emerald" : "slate"}>
                            {m.status.toUpperCase()}
                          </Badge>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <Button size="sm" onClick={() => navigateToLevel2(m)}>
                            View Agents →
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}

          {/* LEVEL 2: AGENTS UNDER MANAGER */}
          {level === 2 && (
            agents.length === 0 ? (
              <div className="text-center py-8 border border-dashed border-slate-200 rounded-lg text-slate-500 text-xs">
                No field agents currently managed by this manager.
              </div>
            ) : (
              <div className="overflow-x-auto border border-slate-200 rounded-lg">
                <table className="w-full text-left text-xs">
                  <thead className="bg-slate-50 text-slate-600 font-semibold uppercase border-b border-slate-200">
                    <tr>
                      <th className="px-4 py-2.5">Field Agent</th>
                      <th className="px-4 py-2.5">Ref ID</th>
                      <th className="px-4 py-2.5">Location Hierarchy</th>
                      <th className="px-4 py-2.5">Contact</th>
                      <th className="px-4 py-2.5 text-center">Subscribers</th>
                      <th className="px-4 py-2.5">Status</th>
                      <th className="px-4 py-2.5 text-right">Drill Down</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {agents.map((ag) => (
                      <tr key={ag.agent_profile_id} className="hover:bg-slate-50 transition">
                        <td className="px-4 py-3 font-medium">
                          <p className="text-navy-900 font-semibold">{ag.first_name} {ag.last_name}</p>
                          <p className="text-[11px] font-mono text-slate-400">@{ag.username}</p>
                        </td>
                        <td className="px-4 py-3">
                          <RefChip refId={ag.ref_id} />
                        </td>
                        <td className="px-4 py-3 text-[11px] text-slate-600">
                          <div><span className="font-semibold text-slate-800">State:</span> {ag.state || "N/A"}</div>
                          <div><span className="font-semibold text-slate-800">District:</span> {ag.district || "N/A"}</div>
                          {ag.mandal && <div><span className="font-semibold text-slate-800">Mandal:</span> {ag.mandal}</div>}
                        </td>
                        <td className="px-4 py-3 text-slate-600">
                          {ag.mobile_e164}
                        </td>
                        <td className="px-4 py-3 text-center">
                          <Badge tone={ag.subscribers_count > 0 ? "emerald" : "slate"}>
                            {ag.subscribers_count} Subscribers
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          <Badge tone={ag.status === "active" ? "emerald" : ag.status === "pending" ? "amber" : "slate"}>
                            {ag.status.toUpperCase()}
                          </Badge>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <Button size="sm" onClick={() => navigateToLevel3(ag)}>
                            View Subscribers →
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}

          {/* LEVEL 3: SUBSCRIBERS UNDER AGENT */}
          {level === 3 && (
            subscribers.length === 0 ? (
              <div className="text-center py-8 border border-dashed border-slate-200 rounded-lg text-slate-500 text-xs">
                No subscribers onboarded by this agent yet.
              </div>
            ) : (
              <div className="overflow-x-auto border border-slate-200 rounded-lg">
                <table className="w-full text-left text-xs">
                  <thead className="bg-slate-50 text-slate-600 font-semibold uppercase border-b border-slate-200">
                    <tr>
                      <th className="px-4 py-2.5">Subscriber Full Name</th>
                      <th className="px-4 py-2.5">Mobile</th>
                      <th className="px-4 py-2.5">Selected OTT Plan</th>
                      <th className="px-4 py-2.5">Payment Status</th>
                      <th className="px-4 py-2.5">Subscription Status</th>
                      <th className="px-4 py-2.5">Enrolled Date</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {subscribers.map((sub) => (
                      <tr key={sub.subscriber_profile_id} className="hover:bg-slate-50 transition">
                        <td className="px-4 py-3 font-semibold text-navy-900">
                          {sub.full_name}
                        </td>
                        <td className="px-4 py-3 text-slate-600 font-mono">
                          {sub.mobile_e164}
                        </td>
                        <td className="px-4 py-3">
                          <span className="font-semibold text-navy-800">{sub.package_plan || "Custom Package"}</span>
                          {sub.package_type && <span className="text-slate-400 text-[10px] block">{sub.package_type}</span>}
                        </td>
                        <td className="px-4 py-3">
                          <Badge tone={sub.payment_status === "SUCCESS" || sub.payment_status === "paid" ? "emerald" : "amber"}>
                            {(sub.payment_status || "PENDING").toUpperCase()}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          <Badge tone={sub.subscription_status === "active" ? "emerald" : "slate"}>
                            {(sub.subscription_status || "inactive").toUpperCase()}
                          </Badge>
                        </td>
                        <td className="px-4 py-3 text-slate-500">
                          {new Date(sub.created_at).toLocaleDateString()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}
        </>
      )}

      {/* Pagination Controls */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between pt-3 border-t border-slate-100">
          <Button
            size="sm"
            variant="outline"
            disabled={page <= 1 || isLoading}
            onClick={() => setPage((p) => Math.max(p - 1, 1))}
          >
            ← Previous
          </Button>
          <span className="text-xs text-slate-500 font-medium">
            Page {page} of {totalPages}
          </span>
          <Button
            size="sm"
            variant="outline"
            disabled={page >= totalPages || isLoading}
            onClick={() => setPage((p) => Math.min(p + 1, totalPages))}
          >
            Next →
          </Button>
        </div>
      )}
    </Card>
  );
}
