"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { Card, StatCard, Button, Input, Spinner, Alert } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";
import { extractResponseError } from "@/lib/api-error";
import { TargetStatusBadge } from "./TargetStatusBadge";
import { TargetProgressBar } from "./TargetProgressBar";

interface TargetType {
  id: string;
  code: string;
  name: string;
  unit: string;
  calculation_type: string;
  applicable_role: string;
}

interface AdministratorOption {
  user_id: string;
  tenant_id: string;
  community_slug: string;
  name: string;
  email: string;
}

interface TargetItem {
  id: string;
  tenant_id: string | null;
  parent_target_id: string | null;
  target_type_id: string;
  target_type_code: string;
  target_type_name: string;
  target_type_unit: string;
  calculation_type: string;
  assigned_to_user_id: string;
  assigned_to_name: string;
  assigned_to_role: string;
  target_quantity: number;
  achieved_quantity: number;
  remaining_quantity: number;
  progress_percentage: number;
  start_date: string;
  deadline: string;
  period: string;
  status: string;
  notes: string | null;
  allocated_to_children: number;
  remaining_to_allocate: number;
  adjustments_count: number;
}

interface TargetSummary {
  total_targets: number;
  active_targets: number;
  completed_targets: number;
  overdue_targets: number;
  total_target_quantity: number;
  total_achieved_quantity: number;
  overall_progress_percentage: number;
}

export function SuperAdminTargetManager() {
  const [targets, setTargets] = useState<TargetItem[]>([]);
  const [types, setTypes] = useState<TargetType[]>([]);
  const [admins, setAdmins] = useState<AdministratorOption[]>([]);
  const [summary, setSummary] = useState<TargetSummary | null>(null);
  const [loading, setLoading] = useState(true);

  // Filters
  const [kpiFilter, setKpiFilter] = useState("ALL");
  const [statusFilter, setStatusFilter] = useState("ALL");

  // Create Modal state
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedAdminId, setSelectedAdminId] = useState("");
  const [selectedTypeCode, setSelectedTypeCode] = useState("");
  const [targetQuantity, setTargetQuantity] = useState("10");
  const [period, setPeriod] = useState("MONTHLY");
  const [startDate, setStartDate] = useState(new Date().toISOString().split("T")[0]);
  const [deadline, setDeadline] = useState(
    new Date(Date.now() + 30 * 86400000).toISOString().split("T")[0]
  );
  const [notes, setNotes] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  // Adjustment Modal state
  const [adjustingTarget, setAdjustingTarget] = useState<TargetItem | null>(null);
  const [adjustQuantity, setAdjustQuantity] = useState("1");
  const [adjustReason, setAdjustReason] = useState("");
  const [adjusting, setAdjusting] = useState(false);
  const [adjustError, setAdjustError] = useState("");

  async function loadData() {
    setLoading(true);
    try {
      const [typesRes, adminsRes, targetsRes, summaryRes] = await Promise.all([
        fetchBackendApi("api/v1/targets/types", { credentials: "same-origin", cache: "no-store" }),
        fetchBackendApi("api/v1/targets/administrators", { credentials: "same-origin", cache: "no-store" }),
        fetchBackendApi("api/v1/targets/admin", { credentials: "same-origin", cache: "no-store" }),
        fetchBackendApi("api/v1/targets/summary", { credentials: "same-origin", cache: "no-store" }),
      ]);

      if (typesRes.ok) {
        const typesData = await typesRes.json();
        setTypes(typesData);
        if (typesData.length > 0 && !selectedTypeCode) {
          setSelectedTypeCode(typesData[0].code);
        }
      }
      if (adminsRes.ok) {
        const adminsData = await adminsRes.json();
        setAdmins(adminsData);
        if (adminsData.length > 0 && !selectedAdminId) {
          setSelectedAdminId(adminsData[0].user_id);
        }
      }
      if (targetsRes.ok) {
        const targetsData = await targetsRes.json();
        setTargets(targetsData);
      }
      if (summaryRes.ok) {
        const sumData = await summaryRes.json();
        setSummary(sumData);
      }
    } catch {
      // tolerate background load failure
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreateTarget(e: React.FormEvent) {
    e.preventDefault();
    setCreateError("");

    const qty = parseInt(targetQuantity, 10);
    if (isNaN(qty) || qty <= 0) {
      setCreateError("Target quantity must be greater than zero.");
      return;
    }
    if (!selectedAdminId) {
      setCreateError("Please select an administrator.");
      return;
    }
    if (!selectedTypeCode) {
      setCreateError("Please select a KPI target type.");
      return;
    }
    if (startDate > deadline) {
      setCreateError("Start date cannot be after deadline.");
      return;
    }

    setCreating(true);
    try {
      const res = await fetchBackendApi("api/v1/targets/admin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          admin_user_id: selectedAdminId,
          target_type_code: selectedTypeCode,
          target_quantity: qty,
          period,
          start_date: startDate,
          deadline,
          notes: notes.trim() || null,
        }),
      });

      if (!res.ok) {
        const errMsg = await extractResponseError(res);
        setCreateError(errMsg || "Failed to assign target.");
        setCreating(false);
        return;
      }

      setShowCreateModal(false);
      setNotes("");
      setCreating(false);
      loadData();
    } catch {
      setCreateError("An unexpected error occurred while assigning target.");
      setCreating(false);
    }
  }

  async function handleRecordAdjustment(e: React.FormEvent) {
    e.preventDefault();
    if (!adjustingTarget) return;
    setAdjustError("");

    const qty = parseInt(adjustQuantity, 10);
    if (isNaN(qty) || qty === 0) {
      setAdjustError("Adjustment quantity cannot be zero.");
      return;
    }
    if (!adjustReason.trim() || adjustReason.trim().length < 3) {
      setAdjustError("Please provide an auditable justification (minimum 3 characters).");
      return;
    }

    setAdjusting(true);
    try {
      const res = await fetchBackendApi(`api/v1/targets/${adjustingTarget.id}/adjust`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          adjustment_quantity: qty,
          reason: adjustReason.trim(),
        }),
      });

      if (!res.ok) {
        const errMsg = await extractResponseError(res);
        setAdjustError(errMsg || "Failed to record adjustment.");
        setAdjusting(false);
        return;
      }

      setAdjustingTarget(null);
      setAdjustReason("");
      setAdjusting(false);
      loadData();
    } catch {
      setAdjustError("An unexpected error occurred.");
      setAdjusting(false);
    }
  }

  const filteredTargets = targets.filter((t) => {
    if (kpiFilter !== "ALL" && t.target_type_code !== kpiFilter) return false;
    if (statusFilter !== "ALL" && t.status.toUpperCase() !== statusFilter) return false;
    return true;
  });

  return (
    <div className="space-y-6">
      {/* Header and Quick Assign */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="font-display text-xl font-bold text-navy-800">
            Target Management & Administrator Performance
          </h2>
          <p className="text-sm text-slate-500 mt-0.5">
            Hierarchical KPI target assignment and authoritative progress tracking.
          </p>
        </div>
        <Button onClick={() => setShowCreateModal(true)}>
          + Assign Admin Target
        </Button>
      </div>

      {/* KPI Overview Cards */}
      {summary && (
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="Total Targets"
            value={summary.total_targets}
            sub={`${summary.active_targets} Active · ${summary.completed_targets} Completed`}
          />
          <StatCard
            label="Total Quota"
            value={summary.total_target_quantity.toLocaleString()}
            sub="Combined targets assigned"
          />
          <StatCard
            label="Total Achieved"
            value={summary.total_achieved_quantity.toLocaleString()}
            sub="Verified business units"
          />
          <StatCard
            label="Overall Progress"
            value={`${summary.overall_progress_percentage}%`}
            sub={summary.overdue_targets > 0 ? `${summary.overdue_targets} Overdue` : "All on schedule"}
          />
        </div>
      )}

      {/* Targets Table Card */}
      <Card className="overflow-hidden p-0">
        <div className="border-b border-slate-100 bg-slate-50/50 p-4 sm:flex sm:items-center sm:justify-between sm:gap-4">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">
              Filter:
            </span>
            <select
              value={kpiFilter}
              onChange={(e) => setKpiFilter(e.target.value)}
              className="text-xs rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 font-medium text-slate-700 shadow-xs focus:border-navy-600 focus:outline-none"
            >
              <option value="ALL">All KPIs</option>
              {types.map((t) => (
                <option key={t.code} value={t.code}>
                  {t.name}
                </option>
              ))}
            </select>

            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="text-xs rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 font-medium text-slate-700 shadow-xs focus:border-navy-600 focus:outline-none"
            >
              <option value="ALL">All Statuses</option>
              <option value="ACTIVE">Active</option>
              <option value="COMPLETED">Completed</option>
              <option value="OVERDUE">Overdue</option>
            </select>
          </div>

          <div className="mt-3 sm:mt-0 text-xs text-slate-400">
            Showing {filteredTargets.length} of {targets.length} targets
          </div>
        </div>

        {loading ? (
          <div className="flex justify-center p-8">
            <Spinner />
          </div>
        ) : filteredTargets.length === 0 ? (
          <div className="p-8 text-center text-sm text-slate-400">
            No administrator targets match the selected criteria.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm text-slate-700">
              <thead className="border-b border-slate-100 bg-sand-50/40 text-xs font-semibold text-slate-500 uppercase tracking-wider">
                <tr>
                  <th className="px-5 py-3.5">Administrator</th>
                  <th className="px-5 py-3.5">KPI Target</th>
                  <th className="px-5 py-3.5">Progress</th>
                  <th className="px-5 py-3.5">Quota / Achieved</th>
                  <th className="px-5 py-3.5">Cascaded</th>
                  <th className="px-5 py-3.5">Status</th>
                  <th className="px-5 py-3.5">Deadline</th>
                  <th className="px-5 py-3.5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {filteredTargets.map((t) => (
                  <tr key={t.id} className="hover:bg-slate-50/50 transition-colors">
                    <td className="px-5 py-4">
                      <Link
                        href={`/superadmin/employees/${t.assigned_to_user_id}`}
                        className="font-semibold text-navy-800 hover:text-navy-900 hover:underline block"
                      >
                        {t.assigned_to_name}
                      </Link>
                      <span className="text-[11px] text-slate-400">{t.period} Target</span>
                    </td>
                    <td className="px-5 py-4">
                      <span className="font-medium text-slate-800 block">{t.target_type_name}</span>
                      <span className="text-[11px] text-slate-400 capitalize">
                        {t.calculation_type.toLowerCase()} calculation
                      </span>
                    </td>
                    <td className="px-5 py-4 min-w-[140px]">
                      <TargetProgressBar
                        progressPercentage={t.progress_percentage}
                        status={t.status}
                      />
                    </td>
                    <td className="px-5 py-4 font-mono text-xs">
                      <span className="font-bold text-slate-800">{t.achieved_quantity}</span>
                      <span className="text-slate-400"> / {t.target_quantity} {t.target_type_unit}</span>
                      <span className="block text-[11px] text-slate-400 mt-0.5">
                        {t.remaining_quantity} remaining
                      </span>
                    </td>
                    <td className="px-5 py-4 font-mono text-xs">
                      <span className="text-slate-700">{t.allocated_to_children}</span>
                      <span className="text-slate-400"> / {t.target_quantity}</span>
                      <span className="block text-[11px] text-slate-400">
                        {t.remaining_to_allocate} avail
                      </span>
                    </td>
                    <td className="px-5 py-4">
                      <TargetStatusBadge status={t.status} />
                    </td>
                    <td className="px-5 py-4 text-xs font-medium text-slate-600">
                      {new Date(t.deadline).toLocaleDateString()}
                    </td>
                    <td className="px-5 py-4 text-right">
                      <button
                        type="button"
                        onClick={() => setAdjustingTarget(t)}
                        className="inline-flex items-center rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs font-semibold text-slate-700 shadow-2xs hover:bg-slate-50 transition-colors"
                      >
                        Adjust ±
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Assign Admin Target Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-navy-950/60 p-4 backdrop-blur-xs">
          <div className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl">
            <h3 className="font-display text-lg font-bold text-navy-800">
              Assign Target to Administrator
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              Super Admin assigns high-level business goals to community administrators.
            </p>

            {createError && (
              <div className="mt-4">
                <Alert tone="red">{createError}</Alert>
              </div>
            )}

            <form onSubmit={handleCreateTarget} className="mt-4 space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                  Select Administrator
                </label>
                <select
                  value={selectedAdminId}
                  onChange={(e) => setSelectedAdminId(e.target.value)}
                  className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 shadow-xs focus:border-navy-600 focus:outline-none"
                  required
                >
                  {admins.map((a) => (
                    <option key={a.user_id} value={a.user_id}>
                      {a.name} ({a.community_slug})
                    </option>
                  ))}
                </select>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                    KPI Target Type
                  </label>
                  <select
                    value={selectedTypeCode}
                    onChange={(e) => setSelectedTypeCode(e.target.value)}
                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 shadow-xs focus:border-navy-600 focus:outline-none"
                    required
                  >
                    {types.map((t) => (
                      <option key={t.code} value={t.code}>
                        {t.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                    Target Quota
                  </label>
                  <Input
                    type="number"
                    min="1"
                    value={targetQuantity}
                    onChange={(e) => setTargetQuantity(e.target.value)}
                    required
                  />
                </div>
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                    Cadence / Period
                  </label>
                  <select
                    value={period}
                    onChange={(e) => setPeriod(e.target.value)}
                    className="w-full rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-sm text-slate-800 shadow-xs focus:border-navy-600 focus:outline-none"
                  >
                    <option value="MONTHLY">Monthly</option>
                    <option value="QUARTERLY">Quarterly</option>
                    <option value="WEEKLY">Weekly</option>
                    <option value="ANNUAL">Annual</option>
                    <option value="CUSTOM">Custom</option>
                  </select>
                </div>

                <div>
                  <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                    Start Date
                  </label>
                  <Input
                    type="date"
                    value={startDate}
                    onChange={(e) => setStartDate(e.target.value)}
                    required
                  />
                </div>

                <div>
                  <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                    Deadline
                  </label>
                  <Input
                    type="date"
                    value={deadline}
                    onChange={(e) => setDeadline(e.target.value)}
                    required
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                  Notes (Optional)
                </label>
                <textarea
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  rows={2}
                  className="w-full rounded-lg border border-slate-200 bg-white p-2.5 text-sm text-slate-800 shadow-xs focus:border-navy-600 focus:outline-none"
                  placeholder="e.g. Special community expansion campaign"
                />
              </div>

              <div className="mt-6 flex justify-end gap-3 pt-3 border-t border-slate-100">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setShowCreateModal(false)}
                  disabled={creating}
                >
                  Cancel
                </Button>
                <Button type="submit" disabled={creating}>
                  {creating ? "Assigning..." : "Assign Target"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Manual Achievement Adjustment Modal */}
      {adjustingTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-navy-950/60 p-4 backdrop-blur-xs">
          <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl">
            <h3 className="font-display text-lg font-bold text-navy-800">
              Adjust Target Achievement
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              Target: <span className="font-semibold text-slate-700">{adjustingTarget.target_type_name}</span> ({adjustingTarget.assigned_to_name})
            </p>

            {adjustError && (
              <div className="mt-4">
                <Alert tone="red">{adjustError}</Alert>
              </div>
            )}

            <form onSubmit={handleRecordAdjustment} className="mt-4 space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                  Adjustment Delta (± {adjustingTarget.target_type_unit})
                </label>
                <Input
                  type="number"
                  value={adjustQuantity}
                  onChange={(e) => setAdjustQuantity(e.target.value)}
                  placeholder="e.g. +5 or -2"
                  required
                />
                <span className="text-[11px] text-slate-400 mt-1 block">
                  Current achieved: {adjustingTarget.achieved_quantity} · Target quota: {adjustingTarget.target_quantity}
                </span>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                  Auditable Reason / Justification
                </label>
                <textarea
                  value={adjustReason}
                  onChange={(e) => setAdjustReason(e.target.value)}
                  rows={3}
                  className="w-full rounded-lg border border-slate-200 bg-white p-2.5 text-sm text-slate-800 shadow-xs focus:border-navy-600 focus:outline-none"
                  placeholder="e.g. Verified manual content upload logs from studio drive"
                  required
                />
              </div>

              <div className="mt-6 flex justify-end gap-3 pt-3 border-t border-slate-100">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setAdjustingTarget(null)}
                  disabled={adjusting}
                >
                  Cancel
                </Button>
                <Button type="submit" disabled={adjusting}>
                  {adjusting ? "Saving..." : "Record Adjustment"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
