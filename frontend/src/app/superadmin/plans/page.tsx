"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Card, Button, Badge, Input, Spinner } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";
import { useAuth } from "@/components/AuthGuard";

interface Community {
  id: string;
  slug: string;
  status: string;
}

interface SubscriptionPlan {
  id: string;
  community_id: string;
  community_name?: string | null;
  code: string;
  name: string;
  description: string | null;
  benefits: string[] | Record<string, unknown> | null;
  duration_days: number;
  status: "active" | "inactive";
  nominal_price: string | number | null;
  currency: string;
  created_at: string;
  updated_at: string;
}

interface PlanListResponse {
  items: SubscriptionPlan[];
  total: number;
}

export const dynamic = "force-dynamic";

export default function SuperAdminPlansPage() {
  const { identity } = useAuth();
  const [plans, setPlans] = useState<SubscriptionPlan[]>([]);
  const [communities, setCommunities] = useState<Community[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [communityFilter, setCommunityFilter] = useState<string>("all");

  const getCsrfToken = useCallback(async (): Promise<string> => {
    if (identity?.csrf_token) return identity.csrf_token;
    try {
      const res = await fetchBackendApi("api/v1/auth/me", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (res.ok) {
        const data = (await res.json()) as { csrf_token?: string };
        return data.csrf_token || "";
      }
    } catch {
      // Fallback silent failure
    }
    return "";
  }, [identity?.csrf_token]);

  // Create Modal State
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createCommunityId, setCreateCommunityId] = useState("");
  const [createCode, setCreateCode] = useState("");
  const [createName, setCreateName] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [createDuration, setCreateDuration] = useState("30");
  const [createNominalPrice, setCreateNominalPrice] = useState("");
  const [createBenefits, setCreateBenefits] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Edit Modal State
  const [editingPlan, setEditingPlan] = useState<SubscriptionPlan | null>(null);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editDuration, setEditDuration] = useState("");
  const [editNominalPrice, setEditNominalPrice] = useState("");
  const [editBenefits, setEditBenefits] = useState("");

  // Load active communities
  useEffect(() => {
    fetchBackendApi("api/v1/platform/communities?limit=100&status=active", {
      credentials: "same-origin",
      cache: "no-store",
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error("Failed to load communities"))))
      .then((data: { items: Community[] }) => {
        const list = data.items || [];
        setCommunities(list);
        if (list.length > 0) {
          setCreateCommunityId((prev) => prev || list[0].id);
        }
      })
      .catch(() => setCommunities([]));
  }, []);

  const fetchPlans = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      let url = "api/v1/platform/subscription-plans";
      const params = new URLSearchParams();
      if (statusFilter !== "all") params.set("status", statusFilter);
      if (communityFilter !== "all") params.set("community_id", communityFilter);
      const queryStr = params.toString();
      if (queryStr) url += `?${queryStr}`;

      const res = await fetchBackendApi(url, {
        credentials: "same-origin",
        cache: "no-store",
      });

      if (!res.ok) {
        setError(`Failed to load subscription plans (${res.status}).`);
        setPlans([]);
        return;
      }

      const data: PlanListResponse = await res.json();
      setPlans(data.items);
    } catch {
      setError("An unexpected error occurred while loading subscription plans.");
      setPlans([]);
    } finally {
      setIsLoading(false);
    }
  }, [statusFilter, communityFilter]);

  useEffect(() => {
    fetchPlans();
  }, [fetchPlans]);

  const handleCreatePlan = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);

    if (!createCommunityId) {
      setFormError("Please select a community for this plan.");
      return;
    }

    setIsSubmitting(true);

    try {
      const benefitsArray = createBenefits
        .split("\n")
        .map((b) => b.trim())
        .filter(Boolean);

      const payload: Record<string, unknown> = {
        community_id: createCommunityId,
        code: createCode.trim(),
        name: createName.trim(),
        description: createDescription.trim() || null,
        duration_days: parseInt(createDuration, 10),
        benefits: benefitsArray.length > 0 ? benefitsArray : null,
        nominal_price: createNominalPrice.trim() ? parseFloat(createNominalPrice.trim()) : null,
        currency: "INR",
        status: "active",
      };

      const csrfToken = await getCsrfToken();
      const res = await fetchBackendApi("api/v1/platform/subscription-plans", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
        },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setFormError(body.message || body.detail?.message || `Failed to create plan (${res.status})`);
        return;
      }

      setShowCreateModal(false);
      setCreateCode("");
      setCreateName("");
      setCreateDescription("");
      setCreateDuration("30");
      setCreateNominalPrice("");
      setCreateBenefits("");
      await fetchPlans();
    } catch {
      setFormError("Network error occurred while submitting plan.");
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleToggleStatus = async (plan: SubscriptionPlan) => {
    const nextStatus = plan.status === "active" ? "inactive" : "active";
    try {
      const csrfToken = await getCsrfToken();
      const res = await fetchBackendApi(`api/v1/platform/subscription-plans/${plan.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
        },
        body: JSON.stringify({ status: nextStatus }),
      });

      if (res.ok) {
        await fetchPlans();
      } else {
        alert("Failed to update plan status.");
      }
    } catch {
      alert("Network error updating plan status.");
    }
  };

  const openEditModal = (plan: SubscriptionPlan) => {
    setEditingPlan(plan);
    setEditName(plan.name);
    setEditDescription(plan.description || "");
    setEditDuration(String(plan.duration_days));
    setEditNominalPrice(plan.nominal_price ? String(plan.nominal_price) : "");
    if (Array.isArray(plan.benefits)) {
      setEditBenefits(plan.benefits.join("\n"));
    } else {
      setEditBenefits("");
    }
  };

  const handleUpdatePlan = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingPlan) return;
    setFormError(null);
    setIsSubmitting(true);

    try {
      const benefitsArray = editBenefits
        .split("\n")
        .map((b) => b.trim())
        .filter(Boolean);

      const payload: Record<string, unknown> = {
        name: editName.trim(),
        description: editDescription.trim() || null,
        duration_days: parseInt(editDuration, 10),
        benefits: benefitsArray.length > 0 ? benefitsArray : null,
        nominal_price: editNominalPrice.trim() ? parseFloat(editNominalPrice.trim()) : null,
      };

      const csrfToken = await getCsrfToken();
      const res = await fetchBackendApi(`api/v1/platform/subscription-plans/${editingPlan.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
        },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setFormError(body.message || body.detail?.message || `Failed to update plan (${res.status})`);
        return;
      }

      setEditingPlan(null);
      await fetchPlans();
    } catch {
      setFormError("Network error occurred while updating plan.");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="space-y-6 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      {/* Page Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="font-display text-2xl font-bold text-navy-900">
            Subscription Plans Management
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            Configure community-specific subscription packages, duration terms, and catalog prices.
          </p>
        </div>
        <Button
          variant="primary"
          onClick={() => {
            setFormError(null);
            setShowCreateModal(true);
          }}
          className="bg-navy-700 hover:bg-navy-800 text-white self-start sm:self-auto"
        >
          + Create New Plan
        </Button>
      </div>

      {/* Filter Bar: Status Tabs & Community Selector */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-slate-200 pb-3">
        <div className="flex gap-2">
          {(["all", "active", "inactive"] as const).map((filter) => (
            <button
              key={filter}
              onClick={() => setStatusFilter(filter)}
              className={`px-3 py-1.5 text-xs font-semibold rounded-lg capitalize transition-colors ${
                statusFilter === filter
                  ? "bg-navy-100 text-navy-800"
                  : "text-slate-600 hover:bg-slate-100 hover:text-slate-800"
              }`}
            >
              {filter} Plans
            </button>
          ))}
        </div>

        {/* Community Dropdown Filter */}
        <div className="flex items-center gap-2">
          <label htmlFor="community-filter" className="text-xs font-medium text-slate-600 whitespace-nowrap">
            Filter Community:
          </label>
          <select
            id="community-filter"
            value={communityFilter}
            onChange={(e) => setCommunityFilter(e.target.value)}
            className="text-xs rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-slate-800 focus:border-navy-600 focus:outline-none focus:ring-1 focus:ring-navy-600"
          >
            <option value="all">All Communities</option>
            {communities.map((c) => (
              <option key={c.id} value={c.id}>
                {c.slug}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Plans List Table */}
      <Card className="overflow-hidden border border-slate-200 shadow-sm">
        {isLoading ? (
          <div className="p-8 text-center">
            <Spinner className="w-8 h-8 text-navy-600 mx-auto" />
            <p className="text-sm text-slate-500 mt-2">Loading subscription plans...</p>
          </div>
        ) : error ? (
          <div className="p-8 text-center text-red-600">
            <p>{error}</p>
            <Button variant="secondary" onClick={fetchPlans} className="mt-3">
              Retry
            </Button>
          </div>
        ) : plans.length === 0 ? (
          <div className="p-8 text-center text-slate-500">
            <p className="font-medium text-slate-700">No subscription plans found.</p>
            <p className="text-xs text-slate-400 mt-1">
              Create your first plan to allow community agents and operators to register subscriptions.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 text-xs font-semibold text-slate-600 uppercase tracking-wider border-b border-slate-200">
                <tr>
                  <th className="px-6 py-3.5">Community</th>
                  <th className="px-6 py-3.5">Plan Name & Code</th>
                  <th className="px-6 py-3.5">Duration</th>
                  <th className="px-6 py-3.5">Catalog Price</th>
                  <th className="px-6 py-3.5">Benefits</th>
                  <th className="px-6 py-3.5">Status</th>
                  <th className="px-6 py-3.5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {plans.map((plan) => (
                  <tr key={plan.id} className="hover:bg-slate-50/50 transition-colors">
                    <td className="px-6 py-4">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-emerald-50 text-emerald-800 border border-emerald-200">
                        {plan.community_name || "Community"}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      <div className="font-semibold text-navy-900">{plan.name}</div>
                      <div className="text-xs font-mono text-slate-400">{plan.code}</div>
                      {plan.description && (
                        <div className="text-xs text-slate-500 mt-0.5 line-clamp-1">
                          {plan.description}
                        </div>
                      )}
                    </td>
                    <td className="px-6 py-4 text-slate-700 font-medium">
                      {plan.duration_days} days
                    </td>
                    <td className="px-6 py-4 text-slate-700">
                      {plan.nominal_price ? (
                        <span className="font-semibold text-slate-900">
                          ₹{parseFloat(String(plan.nominal_price)).toFixed(2)}
                        </span>
                      ) : (
                        <span className="text-slate-400 italic">Manual only</span>
                      )}
                    </td>
                    <td className="px-6 py-4 text-xs text-slate-600 max-w-xs">
                      {Array.isArray(plan.benefits) && plan.benefits.length > 0 ? (
                        <div className="flex flex-wrap gap-1">
                          {plan.benefits.slice(0, 3).map((b, idx) => (
                            <span
                              key={idx}
                              className="px-2 py-0.5 bg-slate-100 text-slate-700 rounded text-[11px]"
                            >
                              {String(b)}
                            </span>
                          ))}
                          {plan.benefits.length > 3 && (
                            <span className="text-slate-400 text-[11px] self-center">
                              +{plan.benefits.length - 3} more
                            </span>
                          )}
                        </div>
                      ) : (
                        <span className="text-slate-400">-</span>
                      )}
                    </td>
                    <td className="px-6 py-4">
                      <Badge
                        tone={plan.status === "active" ? "emerald" : "slate"}
                      >
                        {plan.status}
                      </Badge>
                    </td>
                    <td className="px-6 py-4 text-right space-x-2">
                      <button
                        onClick={() => openEditModal(plan)}
                        className="text-xs font-semibold text-navy-600 hover:text-navy-900 hover:underline"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleToggleStatus(plan)}
                        className={`text-xs font-semibold ${
                          plan.status === "active"
                            ? "text-amber-600 hover:text-amber-800"
                            : "text-emerald-600 hover:text-emerald-800"
                        } hover:underline`}
                      >
                        {plan.status === "active" ? "Deactivate" : "Activate"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Create Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
          <div className="bg-white rounded-xl shadow-xl max-w-lg w-full p-6 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <h2 className="font-display text-lg font-bold text-navy-900">Create Subscription Plan</h2>
              <button
                onClick={() => setShowCreateModal(false)}
                className="text-slate-400 hover:text-slate-600 text-lg font-bold"
              >
                &times;
              </button>
            </div>

            {formError && (
              <div className="p-3 bg-red-50 text-red-700 text-xs rounded-lg border border-red-200">
                {formError}
              </div>
            )}

            <form onSubmit={handleCreatePlan} className="space-y-4">
              {/* Community Selection */}
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Community *
                </label>
                <select
                  required
                  value={createCommunityId}
                  onChange={(e) => setCreateCommunityId(e.target.value)}
                  className="w-full text-sm px-3.5 py-2 border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-navy-600 bg-white text-slate-900"
                >
                  <option value="">Select a community...</option>
                  {communities.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.slug}
                    </option>
                  ))}
                </select>
                <p className="text-[11px] text-slate-400 mt-1">
                  This subscription plan will only be available to agents and subscribers in this community.
                </p>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700 mb-1">
                    Plan Code *
                  </label>
                  <Input
                    required
                    placeholder="e.g. BASIC_MONTHLY"
                    value={createCode}
                    onChange={(e) => setCreateCode(e.target.value.toUpperCase())}
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700 mb-1">
                    Plan Name *
                  </label>
                  <Input
                    required
                    placeholder="e.g. Basic Monthly Plan"
                    value={createName}
                    onChange={(e) => setCreateName(e.target.value)}
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700 mb-1">
                    Duration (Days) *
                  </label>
                  <Input
                    required
                    type="number"
                    min="1"
                    max="3650"
                    value={createDuration}
                    onChange={(e) => setCreateDuration(e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700 mb-1">
                    Catalog Price (Optional ₹)
                  </label>
                  <Input
                    type="number"
                    step="0.01"
                    placeholder="e.g. 199.00"
                    value={createNominalPrice}
                    onChange={(e) => setCreateNominalPrice(e.target.value)}
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Description
                </label>
                <textarea
                  rows={2}
                  className="w-full text-xs p-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-navy-600"
                  placeholder="Plan description and features..."
                  value={createDescription}
                  onChange={(e) => setCreateDescription(e.target.value)}
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Benefits (one per line)
                </label>
                <textarea
                  rows={3}
                  className="w-full text-xs p-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-navy-600 font-mono"
                  placeholder="HD Streaming&#10;Multi-device login&#10;Priority support"
                  value={createBenefits}
                  onChange={(e) => setCreateBenefits(e.target.value)}
                />
              </div>

              <div className="flex justify-end gap-2 pt-2 border-t border-slate-100">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => setShowCreateModal(false)}
                  disabled={isSubmitting}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  variant="primary"
                  className="bg-navy-700 hover:bg-navy-800 text-white"
                  disabled={isSubmitting}
                >
                  {isSubmitting ? "Creating..." : "Save Plan"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit Modal */}
      {editingPlan && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
          <div className="bg-white rounded-xl shadow-xl max-w-lg w-full p-6 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div>
                <h2 className="font-display text-lg font-bold text-navy-900">Edit Plan</h2>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-xs text-slate-400 font-mono">{editingPlan.code}</span>
                  <span className="text-[11px] px-2 py-0.5 rounded bg-emerald-50 text-emerald-800 border border-emerald-200">
                    {editingPlan.community_name || "Assigned Community"}
                  </span>
                </div>
              </div>
              <button
                onClick={() => setEditingPlan(null)}
                className="text-slate-400 hover:text-slate-600 text-lg font-bold"
              >
                &times;
              </button>
            </div>

            {formError && (
              <div className="p-3 bg-red-50 text-red-700 text-xs rounded-lg border border-red-200">
                {formError}
              </div>
            )}

            <form onSubmit={handleUpdatePlan} className="space-y-4">
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Plan Name *
                </label>
                <Input
                  required
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-slate-700 mb-1">
                    Duration (Days) *
                  </label>
                  <Input
                    required
                    type="number"
                    min="1"
                    max="3650"
                    value={editDuration}
                    onChange={(e) => setEditDuration(e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-700 mb-1">
                    Catalog Price (₹)
                  </label>
                  <Input
                    type="number"
                    step="0.01"
                    value={editNominalPrice}
                    onChange={(e) => setEditNominalPrice(e.target.value)}
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Description
                </label>
                <textarea
                  rows={2}
                  className="w-full text-xs p-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-navy-600"
                  value={editDescription}
                  onChange={(e) => setEditDescription(e.target.value)}
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Benefits (one per line)
                </label>
                <textarea
                  rows={3}
                  className="w-full text-xs p-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-navy-600 font-mono"
                  value={editBenefits}
                  onChange={(e) => setEditBenefits(e.target.value)}
                />
              </div>

              <div className="flex justify-end gap-2 pt-2 border-t border-slate-100">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => setEditingPlan(null)}
                  disabled={isSubmitting}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  variant="primary"
                  className="bg-navy-700 hover:bg-navy-800 text-white"
                  disabled={isSubmitting}
                >
                  {isSubmitting ? "Saving..." : "Update Plan"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
