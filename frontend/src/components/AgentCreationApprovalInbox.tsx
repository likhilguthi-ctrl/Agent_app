"use client";

import React, { useEffect, useState, useCallback } from "react";
import { Card, Button, Badge, Spinner } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";

export interface CreationRequestItem {
  id: string;
  request_number: string;
  tenant_id: string;
  requesting_agent_user_id: string;
  requesting_agent_membership_id: string;
  requesting_agent_name: string;
  manager_user_id?: string | null;
  manager_name?: string | null;
  state_id: string;
  district_id: string;
  mandal_id: string;
  state_name: string;
  district_name: string;
  mandal_name: string;
  reason: string;
  status: string;
  manager_decision?: string | null;
  manager_decision_at?: string | null;
  manager_comment?: string | null;
  superadmin_decision?: string | null;
  superadmin_decision_at?: string | null;
  superadmin_comment?: string | null;
  fulfilled_agent_profile_id?: string | null;
  created_at: string;
  updated_at: string;
}

interface AgentCreationApprovalInboxProps {
  role: "admin" | "SUPER_ADMIN";
  title?: string;
}

export default function AgentCreationApprovalInbox({
  role,
  title = "Agent Creation Endorsement & Approval Inbox",
}: AgentCreationApprovalInboxProps) {
  const [requests, setRequests] = useState<CreationRequestItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedReq, setSelectedReq] = useState<CreationRequestItem | null>(null);
  const [decisionModal, setDecisionModal] = useState<"APPROVE" | "REJECT" | null>(null);
  const [comment, setComment] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);

  const fetchRequests = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const endpoint =
        role === "SUPER_ADMIN"
          ? "api/v1/platform/creation-requests"
          : "api/v1/manager/creation-requests";
      const res = await fetchBackendApi(endpoint, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!res.ok) throw new Error("Failed to load approval requests");
      const data = await res.json();
      setRequests(data.items || []);
    } catch (err: any) {
      setError(err?.message || "Failed to load requests");
      setRequests([]);
    } finally {
      setIsLoading(false);
    }
  }, [role]);

  useEffect(() => {
    fetchRequests();
  }, [fetchRequests]);

  const handleDecisionSubmit = async () => {
    if (!selectedReq || !decisionModal) return;
    setIsSubmitting(true);
    setError(null);
    try {
      const endpoint =
        role === "SUPER_ADMIN"
          ? `api/v1/platform/creation-requests/${selectedReq.id}/review`
          : `api/v1/manager/creation-requests/${selectedReq.id}/review`;

      const res = await fetchBackendApi(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision: decisionModal,
          comment: comment.trim() || undefined,
        }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData?.detail?.message || "Review decision failed");
      }

      setActionSuccess(
        `Request ${selectedReq.request_number} successfully ${
          decisionModal === "APPROVE" ? "approved" : "rejected"
        }.`
      );
      setDecisionModal(null);
      setSelectedReq(null);
      setComment("");
      await fetchRequests();
    } catch (err: any) {
      setError(err?.message || "Action failed");
    } finally {
      setIsSubmitting(false);
    }
  };

  const getStatusTone = (st: string): "amber" | "emerald" | "red" | "navy" | "slate" => {
    switch (st) {
      case "PENDING_MANAGER":
        return "amber";
      case "PENDING_SUPER_ADMIN":
        return "navy";
      case "APPROVED":
        return "emerald";
      case "REJECTED":
        return "red";
      case "FULFILLED":
        return "slate";
      default:
        return "slate";
    }
  };

  return (
    <Card className="p-6 border-slate-200 shadow-sm space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-slate-100">
        <div>
          <h3 className="text-base font-bold font-display text-navy-950 flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full bg-amber-500" />
            {title}
          </h3>
          <p className="text-xs text-slate-500 mt-0.5">
            {role === "admin"
              ? "Review subordinate agent creation requests requiring Manager endorsement (Stage 1)."
              : "Dual-custody platform review for manager-endorsed agent creation requests (Stage 2)."}
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={fetchRequests} disabled={isLoading}>
          ↻ Refresh
        </Button>
      </div>

      {actionSuccess && (
        <div className="rounded-lg bg-emerald-50 border border-emerald-200 p-3 text-xs text-emerald-800 font-medium">
          {actionSuccess}
        </div>
      )}

      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 p-3 text-xs text-red-700">
          {error}
        </div>
      )}

      {isLoading ? (
        <div className="py-8 flex flex-col items-center justify-center text-slate-400 gap-2">
          <Spinner />
          <p className="text-xs">Loading approval requests...</p>
        </div>
      ) : requests.length === 0 ? (
        <div className="text-center py-8 border border-dashed border-slate-200 rounded-lg text-slate-500 text-xs">
          No pending child agent creation requests currently in your review queue.
        </div>
      ) : (
        <div className="overflow-x-auto border border-slate-200 rounded-lg">
          <table className="w-full text-left text-xs">
            <thead className="bg-slate-50 text-slate-600 font-semibold uppercase border-b border-slate-200">
              <tr>
                <th className="px-4 py-2.5">Request #</th>
                <th className="px-4 py-2.5">Requesting Agent</th>
                <th className="px-4 py-2.5">Proposed Territory</th>
                <th className="px-4 py-2.5">Business Justification</th>
                <th className="px-4 py-2.5">Workflow Status</th>
                <th className="px-4 py-2.5 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {requests.map((req) => {
                const canAct =
                  (role === "admin" && req.status === "PENDING_MANAGER") ||
                  (role === "SUPER_ADMIN" && req.status === "PENDING_SUPER_ADMIN");

                return (
                  <tr key={req.id} className="hover:bg-slate-50 transition">
                    <td className="px-4 py-3 font-mono font-semibold text-navy-900">
                      {req.request_number}
                    </td>
                    <td className="px-4 py-3">
                      <p className="font-semibold text-slate-800">{req.requesting_agent_name}</p>
                      {req.manager_name && (
                        <p className="text-[11px] text-slate-400">Mgr: {req.manager_name}</p>
                      )}
                    </td>
                    <td className="px-4 py-3 text-[11px] text-slate-600">
                      <div><span className="font-semibold text-slate-800">State:</span> {req.state_name}</div>
                      <div><span className="font-semibold text-slate-800">District:</span> {req.district_name}</div>
                      <div><span className="font-semibold text-slate-800">Mandal:</span> {req.mandal_name}</div>
                    </td>
                    <td className="px-4 py-3 max-w-xs truncate text-slate-600" title={req.reason}>
                      {req.reason}
                    </td>
                    <td className="px-4 py-3">
                      <Badge tone={getStatusTone(req.status)}>
                        {req.status}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-right">
                      {canAct ? (
                        <div className="flex items-center justify-end gap-1.5">
                          <Button
                            size="sm"
                            className="bg-emerald-600 hover:bg-emerald-700 text-white"
                            onClick={() => {
                              setSelectedReq(req);
                              setDecisionModal("APPROVE");
                              setComment("");
                            }}
                          >
                            Approve
                          </Button>
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => {
                              setSelectedReq(req);
                              setDecisionModal("REJECT");
                              setComment("");
                            }}
                          >
                            Reject
                          </Button>
                        </div>
                      ) : (
                        <span className="text-[11px] text-slate-400">Processed</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Decision Review Modal */}
      {decisionModal && selectedReq && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
          <Card className="max-w-md w-full p-6 shadow-2xl space-y-4">
            <div className="flex items-center justify-between pb-2 border-b border-slate-100">
              <h4 className="text-base font-bold font-display text-navy-950">
                {decisionModal === "APPROVE" ? "Endorse / Approve Request" : "Reject Request"}
              </h4>
              <button
                type="button"
                onClick={() => setDecisionModal(null)}
                className="text-slate-400 hover:text-slate-600 text-sm font-bold"
              >
                ✕
              </button>
            </div>

            <div className="text-xs text-slate-600 space-y-1 bg-slate-50 p-3 rounded-lg border border-slate-100">
              <div><strong>Request:</strong> {selectedReq.request_number}</div>
              <div><strong>Agent:</strong> {selectedReq.requesting_agent_name}</div>
              <div><strong>Location:</strong> {selectedReq.mandal_name}, {selectedReq.district_name}, {selectedReq.state_name}</div>
              <div><strong>Reason:</strong> {selectedReq.reason}</div>
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Review Comments <span className="text-slate-400 font-normal">(optional)</span>
              </label>
              <textarea
                rows={3}
                placeholder="Enter review comments or reasoning..."
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                className="w-full rounded-lg border border-slate-300 p-2.5 text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-navy-400"
              />
            </div>

            <div className="flex items-center justify-end gap-2 pt-2 border-t border-slate-100">
              <Button size="sm" variant="outline" onClick={() => setDecisionModal(null)}>
                Cancel
              </Button>
              <Button
                size="sm"
                disabled={isSubmitting}
                className={decisionModal === "APPROVE" ? "bg-emerald-600 hover:bg-emerald-700 text-white" : "bg-red-600 hover:bg-red-700 text-white"}
                onClick={handleDecisionSubmit}
              >
                {isSubmitting ? "Submitting..." : decisionModal === "APPROVE" ? "Confirm Approval" : "Confirm Rejection"}
              </Button>
            </div>
          </Card>
        </div>
      )}
    </Card>
  );
}
