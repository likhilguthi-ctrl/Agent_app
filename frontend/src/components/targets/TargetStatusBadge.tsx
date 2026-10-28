"use client";

import React from "react";
import { Badge } from "@/components/ui";

interface TargetStatusBadgeProps {
  status: string;
}

export function TargetStatusBadge({ status }: TargetStatusBadgeProps) {
  switch (status.toUpperCase()) {
    case "COMPLETED":
      return <Badge tone="emerald">Completed</Badge>;
    case "OVERDUE":
      return <Badge tone="red">Overdue</Badge>;
    case "ACTIVE":
      return <Badge tone="navy">Active</Badge>;
    case "CANCELLED":
      return <Badge tone="slate">Cancelled</Badge>;
    default:
      return <Badge tone="slate">{status}</Badge>;
  }
}
