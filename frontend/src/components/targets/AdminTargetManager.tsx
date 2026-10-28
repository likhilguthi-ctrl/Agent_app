"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { Card, Button, Input, Spinner, Alert } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";
import { extractResponseError } from "@/lib/api-error";
import { TargetStatusBadge } from "./TargetStatusBadge";
import { TargetProgressBar } from "./TargetProgressBar";

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

interface AgentOption {
  id: string;
  user_id: string;
  first_name: string;
  last_name: string;
  ref_id: string;
}

export function AdminTargetManager() {
  const [adminTargets, setAdminTargets] = useState<TargetItem[]>([]);
  const [agentTargets, setAgentTargets] = useState<TargetItem[]>([]);
  const [agents, setAgents] = useState<AgentOption[]>([]);
  const [loading, setLoading] = useState(true);

  // Allocate Modal state
  const [showAllocateModal, setShowAllocateModal] = useState(false);
  const [selectedParentId, setSelectedParentId] = useState("");
  const [selectedAgentUserId, setSelectedAgentUserId] = useState("");
  const [allocatedQty, setAllocatedQty] = useState("5");
  const [allocStartDate, setAllocStartDate] = useState("");
  const [allocDeadline, setAllocDeadline] = useState("");
  const [allocNotes, setAllocNotes] = useState("");
  const [allocating, setAllocating] = useState(false);
  const [allocError, setAllocError] = useState("");

  async function loadData() {
    setLoading(true);
    try {
      const [adminRes, agentTargetsRes, agentsRes] = await Promise.all([
        fetchBackendApi("api/v1/targets/admin", { credentials: "same-origin", cache: "no-store" }),
        fetchBackendApi("api/v1/targets/agent", { credentials: "same-origin", cache: "no-store" }),
        fetchBackendApi("api/v1/agents", { credentials: "same-origin", cache: "no-store" }),
      ]);

      if (adminRes.ok) {
        const adminData = await adminRes.json();
        setAdminTargets(adminData);
        if (adminData.length > 0 && !selectedParentId) {
          setSelectedParentId(adminData[0].id);
          setAllocStartDate(adminData[0].start_date);
          setAllocDeadline(adminData[0].deadline);
        }
      }
      if (agentTargetsRes.ok) {
        const agentTargetsData = await agentTargetsRes.json();
        setAgentTargets(agentTargetsData);
      }
      if (agentsRes.ok) {
        const agentsData = await agentsRes.json();
        const list = (agentsData.items || []).map((ag: any) => ({
          id: ag.id,
          user_id: ag.user_id,
          first_name: ag.first_name,
          last_name: ag.last_name,
          ref_id: ag.ref_id,
        }));
        setAgents(list);
        if (list.length > 0 && !selectedAgentUserId) {
          setSelectedAgentUserId(list[0].user_id);
        }
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

  const selectedParent = adminTargets.find((t) => t.id === selectedParentId);

  function openAllocateModalFor(target: TargetItem) {
    setSelectedParentId(target.id);
    setAllocStartDate(target.start_date);
    setAllocDeadline(target.deadline);
    setAllocatedQty(Math.min(target.remaining_to_allocate, 5).toString());
    setAllocError("");
    setShowAllocateModal(true);
  }

  async function handleAllocate(e: React.FormEvent) {
    e.preventDefault();
    setAllocError("");

    const qty = parseInt(allocatedQty, 10);
    if (isNaN(qty) || qty <= 0) {
      setAllocError("Allocated quantity must be greater than zero.");
      return;
    }
    if (!selectedParent) {
      setAllocError("Please select an active parent target.");
      return;
    }
    if (qty > selectedParent.remaining_to_allocate) {
      setAllocError(
        `Allocation (${qty}) exceeds remaining quota available (${selectedParent.remaining_to_allocate}).`
      );
      return;
    }
    if (!selectedAgentUserId) {
      setAllocError("Please select a team member.");
      return;
    }

    setAllocating(true);
    try {
      const res = await fetchBackendApi("api/v1/targets/allocate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          parent_target_id: selectedParent.id,
          agent_user_id: selectedAgentUserId,
          target_quantity: qty,
          start_date: allocStartDate || selectedParent.start_date,
          deadline: allocDeadline || selectedParent.deadline,
          notes: allocNotes.trim() || null,
        }),
      });

      if (!res.ok) {
        const errMsg = await extractResponseError(res);
        setAllocError(errMsg || "Failed to allocate target.");
        setAllocating(false);
        return;
      }

      setShowAllocateModal(false);
      setAllocNotes("");
      setAllocating(false);
      loadData();
    } catch {
      setAllocError("An unexpected error occurred while allocating target.");
      setAllocating(false);
    }
  }

  return (
    <div className="space-y-8">
      {/* 1. Admin's Assigned Targets from Super Admin */}
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 className="font-display text-lg font-bold text-navy-800">
              Community KPI Targets
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              High-level targets assigned to your community administration by the Platform Operator.
            </p>
          </div>
          {adminTargets.some((t) => t.remaining_to_allocate > 0 && t.status === "ACTIVE") && (
            <Button
              onClick={() => {
                const available = adminTargets.find((t) => t.remaining_to_allocate > 0 && t.status === "ACTIVE");
                if (available) openAllocateModalFor(available);
              }}
            >
              + Allocate Target to Team
            </Button>
          )}
        </div>

        {loading ? (
          <div className="flex justify-center p-8">
            <Spinner />
          </div>
        ) : adminTargets.length === 0 ? (
          <Card className="p-6 text-center text-sm text-slate-400">
            No KPI targets have been assigned to your administration yet.
          </Card>
        ) : (
          <div className="grid md:grid-cols-2 gap-4">
            {adminTargets.map((t) => (
              <Card key={t.id} className="p-5 flex flex-col justify-between">
                <div>
                  <div className="flex items-start justify-between gap-3 mb-2">
                    <div>
                      <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 block">
                        {t.period} Target
                      </span>
                      <h3 className="font-display font-bold text-navy-800 text-base">
                        {t.target_type_name}
                      </h3>
                    </div>
                    <TargetStatusBadge status={t.status} />
                  </div>

                  <div className="my-4">
                    <TargetProgressBar
                      progressPercentage={t.progress_percentage}
                      status={t.status}
                    />
                  </div>

                  <div className="grid grid-cols-3 gap-2 py-3 border-y border-slate-100 text-xs">
                    <div>
                      <span className="text-slate-400 block text-[11px]">Target Quota</span>
                      <span className="font-bold text-slate-800 text-sm">
                        {t.target_quantity} {t.target_type_unit}
                      </span>
                    </div>
                    <div>
                      <span className="text-slate-400 block text-[11px]">Achieved</span>
                      <span className="font-bold text-emerald-600 text-sm">
                        {t.achieved_quantity} {t.target_type_unit}
                      </span>
                    </div>
                    <div>
                      <span className="text-slate-400 block text-[11px]">Remaining</span>
                      <span className="font-bold text-slate-800 text-sm">
                        {t.remaining_quantity} {t.target_type_unit}
                      </span>
                    </div>
                  </div>
                </div>

                <div className="pt-4 mt-2 flex items-center justify-between text-xs">
                  <div className="text-slate-500">
                    <span className="font-medium text-slate-700">{t.remaining_to_allocate}</span> {t.target_type_unit} available to cascade
                  </div>
                  {t.remaining_to_allocate > 0 && t.status === "ACTIVE" && (
                    <button
                      type="button"
                      onClick={() => openAllocateModalFor(t)}
                      className="inline-flex items-center gap-1 font-semibold text-navy-700 hover:text-navy-900 transition-colors"
                    >
                      Allocate to Agent →
                    </button>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>

      {/* 2. Cascaded Team Targets Table */}
      <div className="space-y-4">
        <div>
          <h2 className="font-display text-lg font-bold text-navy-800">
            Managed Team Performance & Cascaded Targets
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Individual target quotas allocated to field agents and verified execution metrics.
          </p>
        </div>

        <Card className="overflow-hidden p-0">
          {loading ? (
            <div className="flex justify-center p-8">
              <Spinner />
            </div>
          ) : agentTargets.length === 0 ? (
            <div className="p-8 text-center text-sm text-slate-400">
              No targets have been cascaded to team agents yet.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm text-slate-700">
                <thead className="border-b border-slate-100 bg-sand-50/40 text-xs font-semibold text-slate-500 uppercase tracking-wider">
                  <tr>
                    <th className="px-5 py-3.5">Field Agent</th>
                    <th className="px-5 py-3.5">KPI Goal</th>
                    <th className="px-5 py-3.5">Progress</th>
                    <th className="px-5 py-3.5">Quota / Achieved</th>
                    <th className="px-5 py-3.5">Remaining</th>
                    <th className="px-5 py-3.5">Status</th>
                    <th className="px-5 py-3.5">Deadline</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {agentTargets.map((t) => (
                    <tr key={t.id} className="hover:bg-slate-50/50 transition-colors">
                      <td className="px-5 py-4">
                        <span className="font-semibold text-navy-800 block">
                          {t.assigned_to_name}
                        </span>
                        <span className="text-[11px] text-slate-400 font-mono">
                          {t.period} Cadence
                        </span>
                      </td>
                      <td className="px-5 py-4">
                        <span className="font-medium text-slate-800 block">
                          {t.target_type_name}
                        </span>
                      </td>
                      <td className="px-5 py-4 min-w-[130px]">
                        <TargetProgressBar
                          progressPercentage={t.progress_percentage}
                          status={t.status}
                        />
                      </td>
                      <td className="px-5 py-4 font-mono text-xs">
                        <span className="font-bold text-slate-800">{t.achieved_quantity}</span>
                        <span className="text-slate-400"> / {t.target_quantity} {t.target_type_unit}</span>
                      </td>
                      <td className="px-5 py-4 font-mono text-xs font-medium text-slate-700">
                        {t.remaining_quantity} {t.target_type_unit}
                      </td>
                      <td className="px-5 py-4">
                        <TargetStatusBadge status={t.status} />
                      </td>
                      <td className="px-5 py-4 text-xs font-medium text-slate-600">
                        {new Date(t.deadline).toLocaleDateString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {/* Allocate Target to Agent Modal */}
      {showAllocateModal && selectedParent && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-navy-950/60 p-4 backdrop-blur-xs">
          <div className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl">
            <h3 className="font-display text-lg font-bold text-navy-800">
              Allocate Target to Team Member
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              Cascade a target quota from parent target:{" "}
              <span className="font-semibold text-slate-700">{selectedParent.target_type_name}</span>.
            </p>

            <div className="my-3 rounded-lg border border-sky-100 bg-sky-50/70 p-3 text-xs text-sky-800">
              Parent Quota: <span className="font-bold">{selectedParent.target_quantity}</span> · 
              Already Cascaded: <span className="font-bold">{selectedParent.allocated_to_children}</span> · 
              Remaining Available: <span className="font-bold">{selectedParent.remaining_to_allocate} {selectedParent.target_type_unit}</span>
            </div>

            {allocError && (
              <div className="my-3">
                <Alert tone="red">{allocError}</Alert>
              </div>
            )}

            <form onSubmit={handleAllocate} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                  Select Team Agent
                </label>
                <select
                  value={selectedAgentUserId}
                  onChange={(e) => setSelectedAgentUserId(e.target.value)}
                  className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 shadow-xs focus:border-navy-600 focus:outline-none"
                  required
                >
                  {agents.map((ag) => (
                    <option key={ag.user_id} value={ag.user_id}>
                      {ag.first_name} {ag.last_name} ({ag.ref_id})
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                  Target Quota to Allocate ({selectedParent.target_type_unit})
                </label>
                <Input
                  type="number"
                  min="1"
                  max={selectedParent.remaining_to_allocate}
                  value={allocatedQty}
                  onChange={(e) => setAllocatedQty(e.target.value)}
                  required
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                    Start Date
                  </label>
                  <Input
                    type="date"
                    min={selectedParent.start_date}
                    max={selectedParent.deadline}
                    value={allocStartDate}
                    onChange={(e) => setAllocStartDate(e.target.value)}
                    required
                  />
                </div>

                <div>
                  <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                    Deadline
                  </label>
                  <Input
                    type="date"
                    min={allocStartDate || selectedParent.start_date}
                    max={selectedParent.deadline}
                    value={allocDeadline}
                    onChange={(e) => setAllocDeadline(e.target.value)}
                    required
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 uppercase tracking-wider mb-1">
                  Instructions / Notes (Optional)
                </label>
                <textarea
                  value={allocNotes}
                  onChange={(e) => setAllocNotes(e.target.value)}
                  rows={2}
                  className="w-full rounded-lg border border-slate-200 bg-white p-2.5 text-sm text-slate-800 shadow-xs focus:border-navy-600 focus:outline-none"
                  placeholder="e.g. Focus on Ward 4 residential cluster"
                />
              </div>

              <div className="mt-6 flex justify-end gap-3 pt-3 border-t border-slate-100">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setShowAllocateModal(false)}
                  disabled={allocating}
                >
                  Cancel
                </Button>
                <Button type="submit" disabled={allocating}>
                  {allocating ? "Allocating..." : "Confirm Allocation"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
