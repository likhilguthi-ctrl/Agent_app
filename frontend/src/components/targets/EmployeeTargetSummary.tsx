"use client";

import React, { useEffect, useState } from "react";
import { Card, Spinner } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";
import { TargetStatusBadge } from "./TargetStatusBadge";
import { TargetProgressBar } from "./TargetProgressBar";

interface TargetItem {
  id: string;
  target_type_id: string;
  target_type_code: string;
  target_type_name: string;
  target_type_unit: string;
  calculation_type: string;
  target_quantity: number;
  achieved_quantity: number;
  remaining_quantity: number;
  progress_percentage: number;
  start_date: string;
  deadline: string;
  period: string;
  status: string;
  notes: string | null;
}

interface EmployeeTargetSummaryProps {
  userId: string;
}

export function EmployeeTargetSummary({ userId }: EmployeeTargetSummaryProps) {
  const [targets, setTargets] = useState<TargetItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      setLoading(true);
      try {
        // Query targets for this agent
        const res = await fetchBackendApi(`api/v1/targets/agent?agent_user_id=${userId}`, {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (res.ok) {
          const data = await res.json();
          setTargets(data);
        }
      } catch {
        // ignore load error
      } finally {
        setLoading(false);
      }
    }
    if (userId) {
      load();
    }
  }, [userId]);

  if (loading) {
    return (
      <Card className="p-5 flex justify-center items-center">
        <Spinner />
      </Card>
    );
  }

  if (targets.length === 0) {
    return (
      <Card className="p-5 text-center text-sm text-slate-400">
        No active targets currently assigned to this member.
      </Card>
    );
  }

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="font-display font-bold text-navy-800 text-base">
            Assigned Targets & Performance
          </h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Verified KPI progress and milestone deliverables.
          </p>
        </div>
        <span className="text-xs font-semibold text-slate-500">
          {targets.length} {targets.length === 1 ? "Target" : "Targets"}
        </span>
      </div>

      <div className="divide-y divide-slate-100">
        {targets.map((t) => (
          <div key={t.id} className="py-3.5 first:pt-0 last:pb-0">
            <div className="flex items-center justify-between gap-3 mb-2">
              <div>
                <span className="font-semibold text-sm text-slate-800 block">
                  {t.target_type_name}
                </span>
                <span className="text-[11px] text-slate-400">
                  {t.period} · Deadline: {new Date(t.deadline).toLocaleDateString()}
                </span>
              </div>
              <TargetStatusBadge status={t.status} />
            </div>

            <TargetProgressBar
              progressPercentage={t.progress_percentage}
              status={t.status}
            />

            <div className="flex items-center justify-between text-xs text-slate-500 mt-2 font-mono">
              <span>Achieved: <strong className="text-slate-800">{t.achieved_quantity}</strong> / {t.target_quantity} {t.target_type_unit}</span>
              <span>Remaining: <strong className="text-slate-800">{t.remaining_quantity}</strong></span>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

