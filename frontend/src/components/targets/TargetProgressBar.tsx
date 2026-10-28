"use client";

import React from "react";

interface TargetProgressBarProps {
  progressPercentage: number;
  status: string;
  showLabel?: boolean;
}

export function TargetProgressBar({
  progressPercentage,
  status,
  showLabel = true,
}: TargetProgressBarProps) {
  const isCompleted = status.toUpperCase() === "COMPLETED" || progressPercentage >= 100;
  const isOverdue = status.toUpperCase() === "OVERDUE";

  let barColor = "bg-sky-500";
  let textColor = "text-sky-700";

  if (isCompleted) {
    barColor = "bg-emerald-500";
    textColor = "text-emerald-700";
  } else if (isOverdue) {
    barColor = "bg-rose-500";
    textColor = "text-rose-700";
  }

  const fillWidth = Math.min(Math.max(progressPercentage, 0), 100);

  return (
    <div className="w-full">
      {showLabel && (
        <div className="flex justify-between items-center text-xs mb-1">
          <span className="font-medium text-slate-500">Progress</span>
          <span className={`font-semibold ${textColor}`}>{progressPercentage}%</span>
        </div>
      )}
      <div className="h-2 w-full bg-slate-100 rounded-full overflow-hidden">
        <div
          className={`h-full ${barColor} rounded-full transition-all duration-300`}
          style={{ width: `${fillWidth}%` }}
        />
      </div>
    </div>
  );
}

