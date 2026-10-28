"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Card, Badge, RefChip, EmptyState, Button, Spinner, Input } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";

interface AgentListItem {
  id: string;
  firstName: string;
  lastName: string;
  username: string;
  mobile: string;
  email: string;
  refId: string;
  status: "active" | "inactive" | "pending";
  commissionRate: number;
  communitySlug: string;
  aadhaarMasked: string;
  panMasked: string;
}

export default function EmployeeList({ basePath }: { basePath: string }) {
  const [employees, setEmployees] = useState<AgentListItem[] | null>(null);
  const [query, setQuery] = useState("");
  const isSuperAdmin = basePath === "/superadmin";

  useEffect(() => {
    const endpoint = isSuperAdmin ? "api/v1/platform/agents" : "api/v1/agents";
    fetchBackendApi(endpoint, {
      credentials: "same-origin",
      cache: "no-store",
    })
      .then((r) => (r.ok ? r.json() : { items: [] }))
      .then((d) => {
        const mapped: AgentListItem[] = (d.items || []).map((ag: any) => ({
          id: ag.id,
          firstName: ag.first_name,
          lastName: ag.last_name,
          username: ag.username,
          mobile: ag.mobile_e164,
          email: ag.email || "",
          refId: ag.ref_id,
          status: ag.status === "active" ? "active" : ag.status === "pending" ? "pending" : "inactive",
          commissionRate: Number(ag.effective_commission_rate),
          communitySlug: ag.community_slug,
          aadhaarMasked: ag.aadhaar_number_masked,
          panMasked: ag.pan_number_masked,
        }));
        setEmployees(mapped);
      })
      .catch(() => {
        setEmployees([]);
      });
  }, [isSuperAdmin]);

  if (employees === null) {
    return (
      <div className="flex justify-center py-24 text-slate-400">
        <Spinner className="w-6 h-6" />
      </div>
    );
  }

  const filtered = employees.filter((e) => {
    const q = query.toLowerCase();
    return (
      `${e.firstName} ${e.lastName}`.toLowerCase().includes(q) ||
      e.refId.toLowerCase().includes(q) ||
      e.username.toLowerCase().includes(q) ||
      e.mobile.includes(q)
    );
  });

  return (
    <div>
      <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-5 gap-4">
        <div>
          <p className="text-sm text-slate-500">
            {employees.length} active agent{employees.length === 1 ? "" : "s"} registered in platform
          </p>
        </div>
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 sm:gap-3 w-full sm:w-auto">
          <Input
            placeholder="Search by name, mobile, or reference ID"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="w-full sm:w-72"
          />
          <Link href={`${basePath}/employees/add`}>
            <Button>+ Register Agent</Button>
          </Link>
        </div>
      </div>

      {filtered.length === 0 ? (
        <Card>
          <EmptyState
            title={employees.length === 0 ? "No agents registered yet" : "No matches"}
            description={
              employees.length === 0
                ? "Register your first agent to generate their permanent reference ID and establish private document storage."
                : "Try a different name, username, mobile number, or reference ID."
            }
            action={
              employees.length === 0 && (
                <Link href={`${basePath}/employees/add`}>
                  <Button>+ Register Agent</Button>
                </Link>
              )
            }
          />
        </Card>
      ) : (
        <>
          <Card className="md:hidden overflow-hidden">
            <div className="divide-y divide-slate-100">
              {filtered.map((emp) => (
                <Link key={emp.id} href={`${basePath}/employees/${emp.id}`} className="block p-4 active:bg-sand-50">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-medium text-slate-800 truncate">{emp.firstName} {emp.lastName}</p>
                      <p className="text-xs text-slate-400 truncate mt-0.5">@{emp.username} • {emp.communitySlug}</p>
                    </div>
                    <Badge tone={emp.status === "active" ? "emerald" : emp.status === "pending" ? "amber" : "slate"}>
                      {emp.status === "active" ? "Active" : emp.status === "pending" ? "Pending Approval" : "Inactive"}
                    </Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-3 mt-3 text-sm">
                    <div>
                      <p className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">Reference ID</p>
                      <RefChip refId={emp.refId} />
                    </div>
                    <div>
                      <p className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">Commission</p>
                      <p className="text-slate-700 font-bold text-emerald-600">{emp.commissionRate}%</p>
                    </div>
                    <div className="col-span-2">
                      <p className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">Identity</p>
                      <p className="text-xs text-slate-600 font-mono">Aadhaar: {emp.aadhaarMasked} | PAN: {emp.panMasked}</p>
                    </div>
                  </div>
                  <p className="text-xs font-medium text-navy-700 mt-4">View profile →</p>
                </Link>
              ))}
            </div>
          </Card>

          <Card className="hidden md:block overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-slate-400 text-xs uppercase tracking-wide border-b border-slate-100">
                  <th className="px-5 py-3 font-medium">Agent</th>
                  <th className="px-5 py-3 font-medium">Reference ID</th>
                  <th className="px-5 py-3 font-medium">Community</th>
                  <th className="px-5 py-3 font-medium">Mobile</th>
                  <th className="px-5 py-3 font-medium">Aadhaar / PAN</th>
                  <th className="px-5 py-3 font-medium">Commission</th>
                  <th className="px-5 py-3 font-medium">Status</th>
                  <th className="px-5 py-3 font-medium"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {filtered.map((emp) => (
                  <tr key={emp.id} className="hover:bg-sand-50/50 transition-colors">
                    <td className="px-5 py-3.5">
                      <p className="font-medium text-slate-800">{emp.firstName} {emp.lastName}</p>
                      <p className="text-xs text-slate-400">@{emp.username}</p>
                    </td>
                    <td className="px-5 py-3.5">
                      <RefChip refId={emp.refId} />
                    </td>
                    <td className="px-5 py-3.5 font-mono text-xs text-slate-700">
                      {emp.communitySlug}
                    </td>
                    <td className="px-5 py-3.5 text-slate-600 font-mono text-xs">
                      {emp.mobile}
                    </td>
                    <td className="px-5 py-3.5 text-xs font-mono text-slate-500">
                      <div>Aadhaar: {emp.aadhaarMasked}</div>
                      <div>PAN: {emp.panMasked}</div>
                    </td>
                    <td className="px-5 py-3.5 font-bold text-emerald-600">
                      {emp.commissionRate}%
                    </td>
                    <td className="px-5 py-3.5">
                      <Badge tone={emp.status === "active" ? "emerald" : emp.status === "pending" ? "amber" : "slate"}>
                        {emp.status === "active" ? "Active" : emp.status === "pending" ? "Pending Approval" : "Inactive"}
                      </Badge>
                    </td>
                    <td className="px-5 py-3.5 text-right">
                      <Link
                        href={`${basePath}/employees/${emp.id}`}
                        className="text-xs font-medium text-navy-700 hover:text-navy-900"
                      >
                        View profile →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </>
      )}
    </div>
  );
}
