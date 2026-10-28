"use client";

import React, { useState } from "react";
import { Card, Button } from "@/components/ui";
import CascadingLocationSelector, { LocationSelection } from "./CascadingLocationSelector";
import { fetchBackendApi } from "@/lib/backend-api";

interface AgentCreationRequestModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

export default function AgentCreationRequestModal({
  isOpen,
  onClose,
  onSuccess,
}: AgentCreationRequestModalProps) {
  const [location, setLocation] = useState<LocationSelection>({
    state: "",
    district: "",
    mandal: "",
  });
  const [reason, setReason] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!location.state_id || !location.district_id || !location.mandal_id) {
      setError("Please select State, District, and Mandal.");
      return;
    }
    if (!reason.trim()) {
      setError("Please provide a business reason/justification.");
      return;
    }

    setIsSubmitting(true);
    try {
      const res = await fetchBackendApi("api/v1/agent/creation-requests", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          state_id: location.state_id,
          district_id: location.district_id,
          mandal_id: location.mandal_id,
          reason: reason.trim(),
        }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData?.detail?.message || "Failed to submit creation request");
      }

      onSuccess();
      onClose();
    } catch (err: any) {
      setError(err?.message || "Submission failed");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
      <Card className="max-w-lg w-full p-6 shadow-2xl space-y-4">
        <div className="flex items-center justify-between pb-3 border-b border-slate-100">
          <div>
            <h3 className="text-base font-bold font-display text-navy-950">
              Request Permission to Create Child Agent
            </h3>
            <p className="text-xs text-slate-500 mt-0.5">
              Submit territory details for two-stage managerial and platform approval.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 text-sm font-bold"
          >
            ✕
          </button>
        </div>

        {error && (
          <div className="rounded-lg bg-red-50 border border-red-200 p-3 text-xs text-red-700">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-2">
              Proposed Operating Territory <span className="text-red-500">*</span>
            </label>
            <CascadingLocationSelector
              value={location}
              onChange={(loc) => setLocation(loc)}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">
              Business Need & Expansion Justification <span className="text-red-500">*</span>
            </label>
            <textarea
              rows={3}
              required
              placeholder="Explain subscriber demand, mandal coverage, or field operational reasons..."
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="w-full rounded-lg border border-slate-300 p-2.5 text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-navy-400"
            />
          </div>

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-slate-100">
            <Button size="sm" variant="outline" type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button size="sm" type="submit" disabled={isSubmitting}>
              {isSubmitting ? "Submitting..." : "Submit for Approval"}
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
