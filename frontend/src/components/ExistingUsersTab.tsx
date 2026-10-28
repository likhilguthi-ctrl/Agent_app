"use client";

import { useEffect, useState } from "react";
import type { SubscribedUser, SubscriptionType } from "@/types";
import { Card, Badge, Spinner, EmptyState, Button } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";

const typeLabels: Record<SubscriptionType, string> = {
  existing: "Existing customer",
  payment_link: "Payment link",
  notify_confirm: "Notify to confirm",
};

export default function ExistingUsersTab({ refreshKey }: { refreshKey: number }) {
  const [users, setUsers] = useState<SubscribedUser[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  async function load() {
    try {
      const res = await fetchBackendApi("api/v1/agent/users");
      if (!res.ok) {
        setUsers([]);
        return;
      }
      const data = await res.json();
      const rawList = Array.isArray(data.items) ? data.items : (Array.isArray(data.users) ? data.users : []);
      const mapped: SubscribedUser[] = rawList.map((u: any) => ({
        id: String(u.id),
        name: u.full_name || u.name || "Subscriber",
        contact: u.mobile_e164 || u.contact || "",
        email: u.email || undefined,
        address: u.address || undefined,
        employeeRefId: String(u.registered_by_agent_membership_id || u.employeeRefId || ""),
        packageId: u.package_plan || u.packageId || "standard",
        billingCycle: (u.billing_cycle || u.billingCycle || "monthly"),
        amount: typeof u.amount === "number" ? u.amount : 0,
        paymentStatus: (u.paymentStatus || (u.status === "active" ? "paid" : "pending")),
        subscriptionType: (u.subscriptionType || "existing"),
        consentAccepted: Boolean(u.consentAccepted ?? true),
        payToken: u.payToken || String(u.id),
        createdAt: typeof u.created_at === "string" ? u.created_at : (u.createdAt || new Date().toISOString()),
      }));
      setUsers(mapped);
    } catch {
      setUsers([]);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  async function updateUser(id: string, body: Record<string, unknown>) {
    setBusyId(id);
    try {
      await fetch(`/api/users/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await load();
    } catch {
      // ignore
    } finally {
      setBusyId(null);
    }
  }

  function linkFor(user: SubscribedUser) {
    const origin = typeof window !== "undefined" ? window.location.origin : "";
    return `${origin}/pay/${user.payToken}`;
  }

  async function copyLink(user: SubscribedUser) {
    try {
      await navigator.clipboard.writeText(linkFor(user));
      setCopiedId(user.id);
      setTimeout(() => setCopiedId(null), 2000);
    } catch {
      // no-op
    }
  }

  if (users === null) {
    return (
      <div className="flex justify-center py-16 text-slate-400">
        <Spinner className="w-6 h-6" />
      </div>
    );
  }

  if (users.length === 0) {
    return (
      <Card>
        <EmptyState
          title="No users yet"
          description="Add your first user from the New User tab to see them here."
        />
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden">
      {/* Mobile cards */}
      <div className="md:hidden divide-y divide-slate-100">
        {users.map((u) => (
          <div key={u.id} className="p-4 space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="font-medium text-slate-800 truncate">{u.name}</p>
                <p className="text-xs text-slate-400 mt-0.5">{new Date(u.createdAt).toLocaleDateString()}</p>
              </div>
              <Badge tone={u.paymentStatus === "paid" ? "emerald" : u.paymentStatus === "failed" ? "red" : "amber"}>
                {u.paymentStatus}
              </Badge>
            </div>

            <div className="grid grid-cols-2 gap-3 text-sm">
              <div className="min-w-0">
                <p className="text-[11px] uppercase tracking-wide text-slate-400">Contact</p>
                <p className="text-slate-700 truncate mt-0.5">{u.contact}</p>
                {u.email && <p className="text-xs text-slate-400 truncate">{u.email}</p>}
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-slate-400">Package</p>
                <p className="text-slate-700 capitalize mt-0.5">{u.billingCycle}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-slate-400">Amount</p>
                <p className="text-slate-700 mt-0.5">₹{u.amount}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-slate-400">Type</p>
                <select
                  value={u.subscriptionType}
                  disabled={busyId === u.id}
                  onChange={(e) => updateUser(u.id, { subscriptionType: e.target.value })}
                  className="mt-1 w-full min-w-0 rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-xs text-slate-700 outline-none focus:ring-2 focus:ring-navy-100"
                >
                  {Object.entries(typeLabels)
                    .filter(([value]) => value !== "existing" || u.subscriptionType === "existing")
                    .map(([value, label]) => (
                    <option key={value} value={value} disabled={value === "existing"}>{label}</option>
                  ))}
                </select>
              </div>
            </div>

            {u.subscriptionType === "notify_confirm" && u.notifiedAt && u.paymentStatus !== "paid" && (
              <p className="text-[11px] text-slate-400">Notified {new Date(u.notifiedAt).toLocaleDateString()}</p>
            )}

            {u.paymentStatus !== "paid" && u.subscriptionType !== "existing" && (
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 pt-1">
                <Button size="sm" variant="outline" onClick={() => copyLink(u)} className="w-full">
                  {copiedId === u.id ? "Copied!" : "Copy link"}
                </Button>
                <Button size="sm" variant="ghost" disabled={busyId === u.id} onClick={() => updateUser(u.id, { action: "resend" })} className="w-full">
                  Resend
                </Button>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Desktop table */}
      <div className="hidden md:block overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-slate-400 text-xs uppercase tracking-wide border-b border-slate-100">
              <th className="px-5 py-3 font-medium">Name</th>
              <th className="px-5 py-3 font-medium">Contact</th>
              <th className="px-5 py-3 font-medium">Package</th>
              <th className="px-5 py-3 font-medium">Amount</th>
              <th className="px-5 py-3 font-medium">Type</th>
              <th className="px-5 py-3 font-medium">Status</th>
              <th className="px-5 py-3 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className="border-b border-slate-50 last:border-0 align-top">
                <td className="px-5 py-3.5">
                  <p className="font-medium text-slate-800">{u.name}</p>
                  <p className="text-xs text-slate-400">{new Date(u.createdAt).toLocaleDateString()}</p>
                </td>
                <td className="px-5 py-3.5 text-slate-600">
                  {u.contact}
                  {u.email && <p className="text-xs text-slate-400">{u.email}</p>}
                </td>
                <td className="px-5 py-3.5 text-slate-600 capitalize">{u.billingCycle}</td>
                <td className="px-5 py-3.5 text-slate-600">₹{u.amount}</td>
                <td className="px-5 py-3.5">
                  <select value={u.subscriptionType} disabled={busyId === u.id} onChange={(e) => updateUser(u.id, { subscriptionType: e.target.value })} className="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-700 outline-none focus:ring-2 focus:ring-navy-100">
                    {Object.entries(typeLabels)
                      .filter(([value]) => value !== "existing" || u.subscriptionType === "existing")
                      .map(([value, label]) => <option key={value} value={value} disabled={value === "existing"}>{label}</option>)}
                  </select>
                </td>
                <td className="px-5 py-3.5">
                  <Badge tone={u.paymentStatus === "paid" ? "emerald" : u.paymentStatus === "failed" ? "red" : "amber"}>{u.paymentStatus}</Badge>
                  {u.subscriptionType === "notify_confirm" && u.notifiedAt && u.paymentStatus !== "paid" && <p className="text-[11px] text-slate-400 mt-1">Notified {new Date(u.notifiedAt).toLocaleDateString()}</p>}
                </td>
                <td className="px-5 py-3.5">
                  <div className="flex items-center gap-2 flex-wrap justify-end">
                    {u.paymentStatus !== "paid" && u.subscriptionType !== "existing" && <>
                      <Button size="sm" variant="outline" onClick={() => copyLink(u)}>{copiedId === u.id ? "Copied!" : "Copy link"}</Button>
                      <Button size="sm" variant="ghost" disabled={busyId === u.id} onClick={() => updateUser(u.id, { action: "resend" })}>Resend</Button>
                    </>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
