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

export function AgentTargetCards() {
  const [targets, setTargets] = useState<TargetItem[]>([]);
  const [loading, setLoading] = useState(true);

  async function loadTargets() {
    setLoading(true);
    try {
      const res = await fetchBackendApi("api/v1/targets/agent", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (res.ok) {
        const data = await res.json();
        setTargets(data);
      }
    } catch {
      // tolerate load error
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadTargets();
  }, []);

  if (loading) {
    return (
      <div className="flex justify-center p-8">
        <Spinner />
      </div>
    );
  }

  if (targets.length === 0) {
    return (
      <Card className="p-6 text-center text-sm text-slate-400">
        You have no active targets assigned at this time. Focus on subscriber recruitment and engagement!
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="font-display text-lg font-bold text-navy-800">
          My Operational Targets & Performance
        </h2>
        <p className="text-xs text-slate-500 mt-0.5">
          Real-time verification of your operational milestones and community deliverables.
        </p>
      </div>

      <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
        {targets.map((t) => {
          const daysLeft = Math.ceil(
            (new Date(t.deadline).getTime() - Date.now()) / (1000 * 60 * 60 * 24)
          );

          return (
            <Card key={t.id} className="p-5 flex flex-col justify-between">
              <div>
                <div className="flex items-start justify-between gap-3 mb-2">
                  <div>
                    <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 block">
                      {t.period} Milestone
                    </span>
                    <h3 className="font-display font-bold text-navy-800 text-base">
                      {t.target_type_name}
                    </h3>
                  </div>
                  <TargetStatusBadge status={t.status} />
                </div>

                {t.notes && (
                  <p className="text-xs text-slate-500 italic mt-1 line-clamp-2">
                    &ldquo;{t.notes}&rdquo;
                  </p>
                )}

                <div className="my-4">
                  <TargetProgressBar
                    progressPercentage={t.progress_percentage}
                    status={t.status}
                  />
                </div>

                <div className="grid grid-cols-3 gap-2 py-3 border-y border-slate-100 text-xs">
                  <div>
                    <span className="text-slate-400 block text-[11px]">Goal Quota</span>
                    <span className="font-bold text-slate-800 text-sm">
                      {t.target_quantity} {t.target_type_unit}
                    </span>
                  </div>
                  <div>
                    <span className="text-slate-400 block text-[11px]">Verified</span>
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

              <div className="pt-4 mt-2 flex items-center justify-between text-xs text-slate-500">
                <span>
                  Deadline: <span className="font-medium text-slate-700">{new Date(t.deadline).toLocaleDateString()}</span>
                </span>
                <span className={daysLeft < 0 ? "text-rose-600 font-semibold" : daysLeft <= 5 ? "text-amber-600 font-semibold" : "text-slate-500"}>
                  {daysLeft < 0 ? `${Math.abs(daysLeft)}d overdue` : `${daysLeft}d remaining`}
                </span>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

