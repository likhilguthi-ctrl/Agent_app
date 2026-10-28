"use client";

import { useEffect, useState } from "react";
import type { SupportTicket } from "@/types";
import { Card, Textarea, Button, Alert, Badge, EmptyState, Spinner } from "@/components/ui";
import { fetchBackendApi, getAuthenticatedIdentity } from "@/lib/backend-api";
import { getWhatsAppSupportUrl } from "@/lib/notifications";

const WHATSAPP_SUPPORT_NUMBER = "911234567890"; // demo number — replace with real support line

export default function EmployeeSupportPage() {
  const [message, setMessage] = useState("");
  const [tickets, setTickets] = useState<SupportTicket[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState("");

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

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setSuccess(false);
    if (!message.trim()) {
      setError("Please describe the issue before sending.");
      return;
    }
    setLoading(true);
    try {
      const identity = await getAuthenticatedIdentity();
      const res = await fetchBackendApi("api/v1/support-tickets", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity?.csrf_token || "",
          Origin: window.location.origin,
        },
        body: JSON.stringify({ message }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || data.detail?.message || "Something went wrong.");
        return;
      }
      setMessage("");
      setSuccess(true);
      load();
    } catch {
      setError("Couldn't reach the server. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="w-full max-w-2xl">
      <div className="mb-5 sm:mb-6">
        <h1 className="font-display text-xl sm:text-2xl font-semibold text-navy-800">Support &amp; help</h1>
        <p className="text-sm text-slate-500 mt-1 leading-5">
          Facing an issue? Send it to your admin, or reach out directly on WhatsApp.
        </p>
      </div>

      <Card className="p-4 sm:p-6">
        <h2 className="font-display font-semibold text-navy-800 mb-4">Raise a ticket</h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <Textarea
            rows={5}
            placeholder="Describe the issue or trouble you're facing…"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            className="min-h-[130px] resize-y"
          />
          {error && <Alert tone="red">{error}</Alert>}
          {success && (
            <Alert tone="emerald">
              Your message has been registered as a new issue and will be resolved in 2–3 working days.
            </Alert>
          )}
          <Button type="submit" disabled={loading} size="lg" className="w-full sm:w-auto">
            {loading ? "Sending…" : "Send to admin"}
          </Button>
        </form>

        <div className="mt-5 pt-5 border-t border-slate-100">
          <p className="text-sm text-slate-500 mb-3">Prefer to chat? Message us on WhatsApp.</p>
          <a
            href={getWhatsAppSupportUrl({ phone: WHATSAPP_SUPPORT_NUMBER, message: "Hi, I need help with my Sahayak Partners account." })}
            target="_blank"
            rel="noopener noreferrer"
            className="block sm:inline-block"
          >
            <Button variant="secondary" size="lg" className="w-full sm:w-auto">Chat on WhatsApp</Button>
          </a>
        </div>
      </Card>

      <div className="mt-7 sm:mt-8">
        <h2 className="font-display font-semibold text-navy-800 mb-3">Your tickets</h2>
        {tickets === null ? (
          <Spinner className="w-5 h-5 text-slate-300" />
        ) : tickets.length === 0 ? (
          <Card>
            <EmptyState title="No tickets yet" description="Anything you send to your admin will show up here." />
          </Card>
        ) : (
          <div className="space-y-3">
            {tickets.map((t) => (
              <Card key={t.id} className="p-4 sm:p-5">
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 sm:gap-3">
                  <Badge tone={t.status === "open" ? "amber" : "emerald"}>
                    {t.status === "open" ? "In progress" : "Resolved"}
                  </Badge>
                  <span className="text-xs text-slate-400">{new Date(t.createdAt).toLocaleString()}</span>
                </div>
                <p className="text-sm text-slate-700 mt-3 leading-5 break-words">{t.message}</p>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
