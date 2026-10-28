"use client";

import { useEffect, useState } from "react";
import type { SupportTicket } from "@/types";
import { Card, Badge, Button, EmptyState, Spinner } from "@/components/ui";
import { fetchBackendApi, getAuthenticatedIdentity } from "@/lib/backend-api";

export default function SupportTicketsList() {
  const [tickets, setTickets] = useState<SupportTicket[] | null>(null);
  const [resolving, setResolving] = useState<string | null>(null);

  async function load() {
    try {
      const res = await fetchBackendApi("api/v1/support-tickets");
      if (res.ok) {
        const data = await res.json();
        setTickets(data.tickets || []);
      } else {
        setTickets([]);
      }
    } catch {
      setTickets([]);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function resolve(id: string) {
    setResolving(id);
    try {
      const identity = await getAuthenticatedIdentity();
      await fetchBackendApi(`api/v1/support-tickets/${id}/resolve`, {
        method: "PATCH",
        headers: {
          "X-CSRF-Token": identity?.csrf_token || "",
          Origin: window.location.origin,
        },
      });
      await load();
    } catch {
      // ignore or log
    } finally {
      setResolving(null);
    }
  }

  if (tickets === null) {
    return (
      <div className="flex justify-center py-24 text-slate-400">
        <Spinner className="w-6 h-6" />
      </div>
    );
  }

  if (tickets.length === 0) {
    return (
      <Card>
        <EmptyState title="No support tickets" description="Issues raised by employees will show up here." />
      </Card>
    );
  }

  const open = tickets.filter((t) => t.status === "open");
  const resolved = tickets.filter((t) => t.status === "resolved");

  return (
    <div className="space-y-8">
      <div>
        <h2 className="font-display font-semibold text-navy-800 mb-3">Open ({open.length})</h2>
        {open.length === 0 ? (
          <p className="text-sm text-slate-400">Nothing waiting on you. Nice.</p>
        ) : (
          <div className="space-y-3">
            {open.map((t) => (
              <Card key={t.id} className="p-5 flex items-start justify-between gap-4">
                <div>
                  <div className="flex items-center gap-2">
                    <p className="font-medium text-slate-800">{t.employeeName}</p>
                    <Badge tone="amber">Open</Badge>
                  </div>
                  <p className="text-sm text-slate-600 mt-1.5">{t.message}</p>
                  <p className="text-xs text-slate-400 mt-2">
                    Raised {new Date(t.createdAt).toLocaleString()} · Resolution target: 2–3 working days
                  </p>
                </div>
                <Button size="sm" variant="secondary" onClick={() => resolve(t.id)} disabled={resolving === t.id}>
                  {resolving === t.id ? "Marking…" : "Mark resolved"}
                </Button>
              </Card>
            ))}
          </div>
        )}
      </div>

      {resolved.length > 0 && (
        <div>
          <h2 className="font-display font-semibold text-navy-800 mb-3">Resolved ({resolved.length})</h2>
          <div className="space-y-3">
            {resolved.map((t) => (
              <Card key={t.id} className="p-5 opacity-70">
                <div className="flex items-center gap-2">
                  <p className="font-medium text-slate-800">{t.employeeName}</p>
                  <Badge tone="emerald">Resolved</Badge>
                </div>
                <p className="text-sm text-slate-600 mt-1.5">{t.message}</p>
              </Card>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
