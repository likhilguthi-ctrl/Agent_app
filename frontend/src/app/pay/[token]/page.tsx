"use client";

import { useEffect, useState } from "react";
import { Card, Spinner, EmptyState } from "@/components/ui";

interface LookupData {
  name: string;
  amount: number;
  billingCycle: string;
  paymentStatus: string;
  employeeName: string;
  package: { id: string; name: string; tier: string };
}

const tierStyles: Record<string, string> = {
  bronze: "bg-bronze",
  silver: "bg-silver",
  gold: "bg-gold",
};

export default function PayPage({ params }: { params: { token: string } }) {
  const [data, setData] = useState<LookupData | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`/api/pay-lookup?token=${params.token}`)
      .then(async (r) => {
        if (!r.ok) {
          if (r.status === 404) setNotFound(true);
          else setUnavailable(true);
          return;
        }
        setData(await r.json());
      })
      .catch(() => setUnavailable(true))
      .finally(() => setLoading(false));
  }, [params.token]);

  if (loading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-sand-50">
        <Spinner className="w-6 h-6 text-slate-400" />
      </main>
    );
  }

  if (unavailable || notFound || !data) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-sand-50 px-6">
        <Card className="max-w-md w-full">
          <EmptyState
            title={unavailable ? "Payment lookup unavailable" : "Link not valid"}
            description={
              unavailable
                ? "Payment links are unavailable until an approved, tenant-scoped payment contract is implemented. No payment was taken."
                : "This payment link may have expired or already been used. Ask for a fresh one."
            }
          />
        </Card>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-sand-50 px-6 py-10">
      <div className="max-w-md mx-auto">
        <div className="text-center mb-6">
          <div className="w-9 h-9 rounded-lg bg-navy-700 flex items-center justify-center mx-auto">
            <span className="font-display text-emerald-400 font-bold text-sm">SP</span>
          </div>
          {data.employeeName && (
            <p className="text-xs text-slate-400 mt-2">
              Referred by <span className="font-medium text-slate-600">{data.employeeName}</span>
            </p>
          )}
        </div>

        <Card className="overflow-hidden mb-5">
          <div className={`h-1.5 ${tierStyles[data.package.tier]}`} />
          <div className="p-5 flex items-center justify-between">
            <div>
              <p className="font-display text-lg font-semibold text-navy-800">{data.package.name}</p>
              <p className="text-xs text-slate-400 capitalize">
                For {data.name} · {data.billingCycle} billing
              </p>
            </div>
            <p className="font-display text-2xl font-semibold text-navy-800">₹{data.amount}</p>
          </div>
        </Card>

        {data.paymentStatus === "paid" ? (
          <Card className="p-8 text-center">
            <div className="w-12 h-12 rounded-full bg-emerald-50 text-emerald-600 text-2xl flex items-center justify-center mx-auto">
              !
            </div>
            <h1 className="font-display text-xl font-semibold text-navy-800 mt-4">
              Payment status needs verification
            </h1>
            <p className="text-sm text-slate-500 mt-1.5">
              This legacy record is marked paid, but no payment provider has verified the settlement. Contact your community administrator.
            </p>
          </Card>
        ) : (
          <Card className="p-6">
            <h1 className="font-display text-xl font-semibold text-navy-800">Payment unavailable</h1>
            <p className="mt-2 text-sm leading-6 text-slate-500">
              This link is not connected to a verified payment provider. No payment was taken. Contact your community administrator for next steps.
            </p>
          </Card>
        )}
      </div>
    </main>
  );
}
