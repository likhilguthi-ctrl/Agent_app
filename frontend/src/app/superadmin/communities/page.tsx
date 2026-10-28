"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Card, Button, Badge } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";
import PlatformCommunityManagement from "@/components/PlatformCommunityManagement";

interface CommunitySummary {
  id: string;
  slug: string;
  status: "active" | "inactive";
  created_at: string;
  domains_count: number;
  administrators_count: number;
}

interface CommunityListResponse {
  items: CommunitySummary[];
  total: number;
  limit: number;
  offset: number;
}

export const dynamic = "force-dynamic";

export default function SuperAdminCommunitiesPage() {
  const [activeTab, setActiveTab] = useState<"communities" | "onboarding">("communities");

  // Communities state
  const [communities, setCommunities] = useState<CommunitySummary[]>([]);
  const [total, setTotal] = useState(0);
  const [limit] = useState(10);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchCommunities = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.set("limit", String(limit));
      params.set("offset", String(offset));
      if (search.trim()) {
        params.set("search", search.trim());
      }
      if (statusFilter !== "all") {
        params.set("status", statusFilter);
      }

      const res = await fetchBackendApi(`api/v1/platform/communities?${params.toString()}`, {
        credentials: "same-origin",
        cache: "no-store",
      });

      if (!res.ok) {
        setError(`Failed to load communities (${res.status}).`);
        setIsLoading(false);
        return;
      }

      const data: CommunityListResponse = await res.json();
      setCommunities(data.items || []);
      setTotal(data.total || 0);
    } catch {
      setError("Network error while connecting to platform service.");
    } finally {
      setIsLoading(false);
    }
  }, [limit, offset, search, statusFilter]);

  useEffect(() => {
    if (activeTab === "communities") {
      fetchCommunities();
    }
  }, [activeTab, fetchCommunities]);

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setOffset(0);
    fetchCommunities();
  };

  const handleStatusChange = (val: string) => {
    setStatusFilter(val);
    setOffset(0);
  };

  const totalPages = Math.ceil(total / limit) || 1;
  const currentPage = Math.floor(offset / limit) + 1;

  return (
    <div className="space-y-6">
      {/* Top Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold font-display text-navy-900 tracking-tight">
            Community & Tenant Management
          </h1>
          <p className="text-sm text-slate-600 mt-1">
            Provision, monitor, and manage isolated communities and their administrators.
          </p>
        </div>

        {activeTab === "communities" && (
          <Link href="/superadmin/communities/create">
            <Button variant="primary">
              + Create Community
            </Button>
          </Link>
        )}
      </div>

      {/* Navigation Tabs */}
      <div className="flex items-center gap-2 border-b border-slate-200">
        <button
          type="button"
          onClick={() => setActiveTab("communities")}
          className={`px-4 py-2 text-sm font-semibold border-b-2 transition -mb-px ${
            activeTab === "communities"
              ? "border-navy-700 text-navy-900"
              : "border-transparent text-slate-500 hover:text-slate-700"
          }`}
        >
          Active Communities
        </button>
        <button
          type="button"
          onClick={() => setActiveTab("onboarding")}
          className={`px-4 py-2 text-sm font-semibold border-b-2 transition -mb-px ${
            activeTab === "onboarding"
              ? "border-navy-700 text-navy-900"
              : "border-transparent text-slate-500 hover:text-slate-700"
          }`}
        >
          Onboarding Requests
        </button>
      </div>

      {/* Tab: Onboarding Requests */}
      {activeTab === "onboarding" && (
        <PlatformCommunityManagement />
      )}

      {/* Tab: Active Communities */}
      {activeTab === "communities" && (
        <div className="space-y-4">
          {/* Filters Bar */}
          <Card className="p-4">
            <form onSubmit={handleSearchSubmit} className="flex flex-col sm:flex-row gap-3">
              <div className="flex-1">
                <input
                  type="text"
                  placeholder="Search by community slug or domain..."
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:ring-2 focus:ring-navy-100 focus:border-navy-400 transition"
                />
              </div>

              <div className="w-full sm:w-48">
                <select
                  value={statusFilter}
                  onChange={(e) => handleStatusChange(e.target.value)}
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none focus:ring-2 focus:ring-navy-100 focus:border-navy-400 transition"
                >
                  <option value="all">All Statuses</option>
                  <option value="active">Active Only</option>
                  <option value="inactive">Inactive Only</option>
                </select>
              </div>

              <Button type="submit" variant="outline" size="sm">
                Filter
              </Button>
            </form>
          </Card>

          {/* Communities Table Card */}
          <Card className="overflow-hidden">
            {error && (
              <div className="p-4 bg-red-50 border-b border-red-200 text-sm text-red-700">
                {error}
              </div>
            )}

            {isLoading ? (
              <div className="p-8 space-y-4">
                <div className="h-6 w-1/3 bg-slate-200 animate-pulse rounded" />
                <div className="h-4 w-full bg-slate-100 animate-pulse rounded" />
                <div className="h-4 w-full bg-slate-100 animate-pulse rounded" />
                <div className="h-4 w-full bg-slate-100 animate-pulse rounded" />
              </div>
            ) : communities.length === 0 ? (
              <div className="p-12 text-center">
                <p className="text-base font-semibold text-slate-700">No communities found.</p>
                <p className="text-xs text-slate-500 mt-1">
                  {search || statusFilter !== "all"
                    ? "Try adjusting your search query or status filter."
                    : "Get started by provisioning the first community."}
                </p>
                <div className="mt-4">
                  <Link href="/superadmin/communities/create">
                    <Button variant="primary" size="sm">
                      + Create First Community
                    </Button>
                  </Link>
                </div>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="bg-slate-50 text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
                    <tr>
                      <th className="py-3 px-4 font-semibold">Community Slug</th>
                      <th className="py-3 px-4 font-semibold">Status</th>
                      <th className="py-3 px-4 font-semibold">Domains</th>
                      <th className="py-3 px-4 font-semibold">Administrators</th>
                      <th className="py-3 px-4 font-semibold">Created Date</th>
                      <th className="py-3 px-4 font-semibold text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {communities.map((comm) => (
                      <tr key={comm.id} className="hover:bg-slate-50/70 transition">
                        <td className="py-3.5 px-4 font-medium text-navy-900">
                          <Link
                            href={`/superadmin/communities/${comm.id}`}
                            className="text-navy-800 hover:text-emerald-700 font-semibold"
                          >
                            {comm.slug}
                          </Link>
                          <div className="text-[10px] font-mono text-slate-400 mt-0.5">
                            {comm.id}
                          </div>
                        </td>
                        <td className="py-3.5 px-4">
                          <Badge tone={comm.status === "active" ? "emerald" : "amber"}>
                            {comm.status.toUpperCase()}
                          </Badge>
                        </td>
                        <td className="py-3.5 px-4 text-slate-700 font-mono text-xs">
                          {comm.domains_count} domain{comm.domains_count === 1 ? "" : "s"}
                        </td>
                        <td className="py-3.5 px-4 text-slate-700 font-mono text-xs">
                          {comm.administrators_count} admin{comm.administrators_count === 1 ? "" : "s"}
                        </td>
                        <td className="py-3.5 px-4 text-slate-500 text-xs">
                          {new Date(comm.created_at).toLocaleDateString()}
                        </td>
                        <td className="py-3.5 px-4 text-right">
                          <Link href={`/superadmin/communities/${comm.id}`}>
                            <Button variant="outline" size="sm">
                              Manage
                            </Button>
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Pagination Controls */}
            {!isLoading && total > 0 && (
              <div className="flex items-center justify-between p-4 border-t border-slate-100 text-xs text-slate-600 bg-slate-50/50">
                <span>
                  Showing {offset + 1} - {Math.min(offset + limit, total)} of {total} communities
                </span>
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - limit))}
                  >
                    Previous
                  </Button>
                  <span className="font-medium text-slate-700">
                    Page {currentPage} of {totalPages}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset + limit >= total}
                    onClick={() => setOffset(offset + limit)}
                  >
                    Next
                  </Button>
                </div>
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
