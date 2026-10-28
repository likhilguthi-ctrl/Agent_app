"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { Employee, SubscribedUser, ActivityLogEntry } from "@/types";
import { Card, StatCard, Badge, RefChip, Spinner, EmptyState } from "@/components/ui";

export default function Overview({
  basePath,
  showActivity = false,
  greeting,
}: {
  basePath: string;
  showActivity?: boolean;
  greeting: string;
}) {
  const [employees, setEmployees] = useState<Employee[] | null>(null);
  const [users, setUsers] = useState<SubscribedUser[] | null>(null);
  const [activity, setActivity] = useState<ActivityLogEntry[] | null>(null);

  useEffect(() => {
    fetch("/api/employees")
      .then((r) => (r.ok ? r.json() : { employees: [] }))
      .then((d) => setEmployees(d.employees || []))
      .catch(() => setEmployees([]));
    fetch("/api/users")
      .then((r) => (r.ok ? r.json() : { users: [] }))
      .then((d) => setUsers(d.users || []))
      .catch(() => setUsers([]));
    if (showActivity) {
      fetch("/api/activity")
        .then((r) => (r.ok ? r.json() : { activity: [] }))
        .then((d) => setActivity(d.activity || []))
        .catch(() => setActivity([]));
    }
  }, [showActivity]);

  if (employees === null || users === null) {
    return (
      <div className="flex justify-center py-24 text-slate-400">
        <Spinner className="w-6 h-6" />
      </div>
    );
  }

  const activeEmployees = employees.filter((e) => e.status === "active");
  const paidUsers = users.filter((u) => u.paymentStatus === "paid");
  const totalRevenue = paidUsers.reduce((s, u) => s + u.amount, 0);
  const totalCommission = paidUsers.reduce((sum, u) => {
    const emp = employees.find((e) => e.refId === u.employeeRefId);
    const rate = emp?.commissionRate ?? 10;
    return sum + (u.amount * rate) / 100;
  }, 0);

  const topEmployees = [...employees]
    .map((e) => ({ e, count: paidUsers.filter((u) => u.employeeRefId === e.refId).length }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 5);

  return (
    <div>
      <h1 className="font-display text-2xl font-semibold text-navy-800">{greeting}</h1>
      <p className="text-sm text-slate-500 mt-1">Here&apos;s how things are looking today.</p>

      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4 mt-6">
        <StatCard label="Total employees" value={employees.length} sub={`${activeEmployees.length} active`} />
        <StatCard label="Users referred" value={paidUsers.length} />
        <StatCard label="Subscription revenue" value={`₹${totalRevenue.toLocaleString("en-IN")}`} />
        <StatCard label="Commission payable" value={`₹${Math.round(totalCommission).toLocaleString("en-IN")}`} />
      </div>

      <div className={`grid gap-6 mt-6 ${showActivity ? "lg:grid-cols-2" : ""}`}>
        <Card className="p-6">
          <h2 className="font-display font-semibold text-navy-800 mb-4">Top performing employees</h2>
          {topEmployees.length === 0 || topEmployees[0].count === 0 ? (
            <EmptyState title="No referrals yet" description="Rankings will appear once employees start referring users." />
          ) : (
            <div className="space-y-3">
              {topEmployees.map(({ e, count }, i) => (
                <Link
                  key={e.id}
                  href={`${basePath}/employees/${e.id}`}
                  className="flex items-center justify-between gap-3 py-2 hover:bg-sand-50 rounded-lg px-2 -mx-2 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <span className="w-6 h-6 rounded-full bg-navy-50 text-navy-700 text-xs font-semibold flex items-center justify-center">
                      {i + 1}
                    </span>
                    <div>
                      <p className="text-sm font-medium text-slate-800">
                        {e.firstName} {e.lastName}
                      </p>
                      <RefChip refId={e.refId} />
                    </div>
                  </div>
                  <Badge tone="navy">{count} users</Badge>
                </Link>
              ))}
            </div>
          )}
        </Card>

        {showActivity && (
          <Card className="p-6">
            <h2 className="font-display font-semibold text-navy-800 mb-4">Recent activity</h2>
            {activity === null ? (
              <Spinner className="w-5 h-5 text-slate-300" />
            ) : activity.length === 0 ? (
              <EmptyState title="No activity yet" description="Admin and employee actions will be logged here." />
            ) : (
              <ul className="space-y-3 max-h-80 overflow-y-auto pr-1">
                {activity.slice(0, 12).map((a) => (
                  <li key={a.id} className="text-sm border-l-2 border-emerald-200 pl-3">
                    <p className="text-slate-700">
                      <span className="font-medium">{a.actor}</span> — {a.action}
                    </p>
                    <p className="text-xs text-slate-400">{a.details}</p>
                    <p className="text-xs text-slate-300">{new Date(a.timestamp).toLocaleString()}</p>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        )}
      </div>
    </div>
  );
}
