"use client";

import React, { useEffect, useState, useCallback, Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useAuth } from "@/components/AuthGuard";
import { Card, Button, Badge, Spinner, Alert } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";

function PaymentCallbackContent() {
  const searchParams = useSearchParams();
  const { identity } = useAuth();

  const orderId = searchParams.get("order_id");
  const paymentId = searchParams.get("payment_id");
  const queryStatus = searchParams.get("status");

  const [loading, setLoading] = useState(true);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmationDetails, setConfirmationDetails] = useState<{
    status: string;
    subscription_status: string;
    message: string;
  } | null>(null);

  const confirmPayment = useCallback(async () => {
    if (!orderId && !paymentId) {
      setError("No valid order identifier found in return URL.");
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const csrfToken = identity?.csrf_token || "";
      const res = await fetchBackendApi("api/v1/agent/payments/confirm", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
        },
        body: JSON.stringify({
          payment_order_id: orderId || null,
          external_payment_id: paymentId || null,
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.message || body.detail?.message || "Failed to confirm payment status.");
        setLoading(false);
        return;
      }

      const data = await res.json();
      setConfirmationDetails(data);
      setConfirmed(true);
    } catch {
      setError("Network error while verifying payment status with backend.");
    } finally {
      setLoading(false);
    }
  }, [orderId, paymentId, identity?.csrf_token]);

  useEffect(() => {
    confirmPayment();
  }, [confirmPayment]);

  return (
    <div className="max-w-xl mx-auto py-8 space-y-6">
      <Card className="p-8 border-slate-200 text-center space-y-6">
        {loading ? (
          <div className="py-12 space-y-4">
            <Spinner className="w-10 h-10 mx-auto text-emerald-600" />
            <h2 className="text-lg font-bold text-slate-800">Verifying Payment with Microservice…</h2>
            <p className="text-xs text-slate-500 max-w-sm mx-auto">
              Please wait while we confirm the settlement and trigger automated subscriber welcome notifications.
            </p>
          </div>
        ) : confirmed ? (
          <div className="space-y-6">
            <div className="w-14 h-14 rounded-full bg-emerald-100 flex items-center justify-center text-emerald-600 text-2xl font-bold mx-auto">
              ✓
            </div>
            <div>
              <h1 className="text-2xl font-display font-bold text-navy-900">
                Subscription Activated!
              </h1>
              <p className="text-sm text-slate-500 mt-1">
                Payment verified successfully via Payment Microservice.
              </p>
            </div>

            <div className="bg-slate-50 rounded-xl p-4 border border-slate-100 text-left text-sm space-y-2">
              <div className="flex justify-between items-center">
                <span className="text-xs text-slate-500 uppercase font-semibold">Payment Status</span>
                <Badge tone="emerald">PAID / SETTLED</Badge>
              </div>
              <div className="flex justify-between items-center">
                <span className="text-xs text-slate-500 uppercase font-semibold">Subscription</span>
                <Badge tone="emerald">ACTIVE</Badge>
              </div>
              {orderId && (
                <div className="flex justify-between items-center">
                  <span className="text-xs text-slate-500 uppercase font-semibold">Order ID</span>
                  <span className="font-mono text-xs text-slate-700">{orderId.slice(0, 18)}…</span>
                </div>
              )}
              <div className="pt-2 border-t border-slate-200">
                <p className="text-xs text-emerald-700 font-medium">
                  ✓ Welcome SMS and conditional WhatsApp notifications dispatched to subscriber.
                </p>
              </div>
            </div>

            <div className="flex flex-col sm:flex-row gap-3 justify-center pt-2">
              <Link href="/agent/dashboard">
                <Button className="w-full sm:w-auto">Return to Dashboard</Button>
              </Link>
              <Link href="/agent/register">
                <Button variant="secondary" className="w-full sm:w-auto">Register Another Subscriber</Button>
              </Link>
            </div>
          </div>
        ) : (
          <div className="space-y-6">
            <div className="w-14 h-14 rounded-full bg-amber-100 flex items-center justify-center text-amber-600 text-2xl font-bold mx-auto">
              !
            </div>
            <div>
              <h1 className="text-2xl font-display font-bold text-navy-900">
                Payment Verification Pending
              </h1>
              <p className="text-sm text-slate-500 mt-1">
                {error || "Payment was not yet marked complete by the gateway."}
              </p>
            </div>

            {error && <Alert tone="amber">{error}</Alert>}

            <div className="flex flex-col sm:flex-row gap-3 justify-center pt-2">
              <Button onClick={confirmPayment} className="w-full sm:w-auto">
                Retry Verification
              </Button>
              <Link href="/agent/dashboard">
                <Button variant="secondary" className="w-full sm:w-auto">Dashboard</Button>
              </Link>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

export default function PaymentCallbackPage() {
  return (
    <Suspense fallback={<div className="p-12 text-center"><Spinner className="w-8 h-8 mx-auto" /></div>}>
      <PaymentCallbackContent />
    </Suspense>
  );
}

