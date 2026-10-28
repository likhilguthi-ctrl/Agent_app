"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { fetchBackendApi } from "@/lib/backend-api";

type OnboardingSummary = {
  request_id: string;
  tenant_slug: string;
  domain: string;
  state: string;
  created_at: string;
  expires_at: string;
  domain_verified: boolean;
};

type OnboardingList = {
  items: OnboardingSummary[];
  limit: number;
  offset: number;
};

type OnboardingDetail = OnboardingSummary & {
  history: Array<{
    event_type: string;
    action: string;
    outcome: string;
    occurred_at: string;
    actor_category: string;
    reason_code: string | null;
  }>;
};

type ActivationEligibility = {
  request_id: string;
  eligible: boolean;
  blockers: string[];
};

type ActivationIntentResult = {
  request_id: string;
  activation_attempt_id: string;
  state: string;
  created_at: string;
  replayed: boolean;
};

type ApiFailure = Error & { status?: number };
type ViewState = "checking" | "signed-out" | "ready" | "denied" | "unavailable";

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed unexpectedly.";
}

async function requestJson<T>(
  path: string,
  init?: RequestInit
): Promise<{ response: Response; data: T | null }> {
  const response = await fetchBackendApi(path, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
  });
  let data: T | null = null;
  if (response.status !== 204) {
    try {
      data = (await response.json()) as T;
    } catch {
      throw new Error("FastAPI returned an invalid JSON response.");
    }
  }
  if (!response.ok) {
    const payload = data as
      | { error?: { code?: string; message?: string } }
      | null;
    const error = new Error(
      payload?.error?.message ??
        payload?.error?.code ??
        `FastAPI request failed with status ${response.status}.`
    ) as ApiFailure;
    error.status = response.status;
    throw error;
  }
  return { response, data };
}

function idempotencyKey(): string {
  if (!globalThis.crypto?.randomUUID) {
    throw new Error("A secure browser context is required for this operation.");
  }
  return globalThis.crypto.randomUUID();
}

function requestPath(requestId: string, suffix = ""): string {
  return `api/v1/platform/onboarding-requests/${requestId}${suffix}`;
}

export default function PlatformCommunityManagement() {
  const [view, setView] = useState<ViewState>("checking");
  const [csrfToken, setCsrfToken] = useState("");
  const [operatorId, setOperatorId] = useState("");
  const [items, setItems] = useState<OnboardingSummary[]>([]);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<OnboardingDetail | null>(null);
  const [activationEligibility, setActivationEligibility] =
    useState<ActivationEligibility | null>(null);
  const [activationIntentPending, setActivationIntentPending] = useState(false);
  const [challengeName, setChallengeName] = useState("");
  const [challengeValue, setChallengeValue] = useState("");
  const [tenantSlug, setTenantSlug] = useState("");
  const [domain, setDomain] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingList, setLoadingList] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function reportFailure(caught: unknown) {
    const status = (caught as ApiFailure).status;
    setError(messageFromError(caught));
    if (status === 401) {
      setView("signed-out");
      setCsrfToken("");
      setOperatorId("");
      setSelected(null);
      setItems([]);
    }
  }

  const loadRequests = useCallback(async (nextOffset: number) => {
    setLoadingList(true);
    try {
      const { data } = await requestJson<OnboardingList>(
        `api/v1/platform/onboarding-requests?limit=25&offset=${nextOffset}`
      );
      if (!data || !Array.isArray(data.items)) {
        throw new Error("FastAPI returned an invalid onboarding list.");
      }
      setItems(data.items);
      setOffset(data.offset);
    } finally {
      setLoadingList(false);
    }
  }, []);

  const loadDetail = useCallback(async (requestId: string) => {
    const { data } = await requestJson<OnboardingDetail>(
      requestPath(requestId)
    );
    if (!data || !Array.isArray(data.history)) {
      throw new Error("FastAPI returned invalid onboarding details.");
    }
    setSelected(data);
  }, []);

  const loadActivationEligibility = useCallback(async (requestId: string) => {
    const { data } = await requestJson<ActivationEligibility>(
      requestPath(requestId, "/activation-eligibility")
    );
    if (
      !data ||
      data.request_id !== requestId ||
      typeof data.eligible !== "boolean" ||
      !Array.isArray(data.blockers) ||
      data.blockers.some((blocker) => typeof blocker !== "string")
    ) {
      throw new Error("FastAPI returned invalid activation eligibility.");
    }
    setActivationEligibility(data);
  }, []);

  const refresh = useCallback(async () => {
    setError("");
    try {
      await loadRequests(offset);
      if (selected) {
        await Promise.all([
          loadDetail(selected.request_id),
          loadActivationEligibility(selected.request_id),
        ]);
      }
    } catch (caught) {
      reportFailure(caught);
    }
  }, [
    loadActivationEligibility,
    loadDetail,
    loadRequests,
    offset,
    selected,
  ]);

  useEffect(() => {
    let active = true;
    async function initialize() {
      try {
        const { data } = await requestJson<{
          operator_id?: unknown;
          csrf_token?: unknown;
        }>("api/v1/platform/auth/oidc/me");
        if (
          !data ||
          typeof data.operator_id !== "string" ||
          typeof data.csrf_token !== "string"
        ) {
          throw new Error("FastAPI returned an invalid platform identity.");
        }
        if (!active) return;
        setOperatorId(data.operator_id);
        setCsrfToken(data.csrf_token);
        setView("ready");
        await loadRequests(0);
      } catch (caught) {
        if (!active) return;
        const status = (caught as ApiFailure).status;
        setError(messageFromError(caught));
        setView(
          status === 401
            ? "signed-out"
            : status === 403
              ? "denied"
              : "unavailable"
        );
      }
    }
    void initialize();
    return () => {
      active = false;
    };
  }, [loadRequests]);

  async function perform(
    operation: () => Promise<void>,
    successMessage: string,
    refreshData = true
  ) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
      setNotice(successMessage);
      if (refreshData) {
        await loadRequests(offset);
        if (selected) {
          await Promise.all([
            loadDetail(selected.request_id),
            loadActivationEligibility(selected.request_id),
          ]);
        }
      }
    } catch (caught) {
      const status = (caught as ApiFailure).status;
      setError(messageFromError(caught));
      if (status === 401) {
        setView("signed-out");
        setCsrfToken("");
        setOperatorId("");
        setSelected(null);
        setItems([]);
      }
    } finally {
      setBusy(false);
    }
  }

  async function createRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const slug = tenantSlug.trim().toLowerCase();
    const hostname = domain.trim().toLowerCase();
    await perform(async () => {
      await requestJson("api/v1/platform/onboarding-requests", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": idempotencyKey(),
        },
        body: JSON.stringify({ tenant_slug: slug, domain: hostname }),
      });
      setTenantSlug("");
      setDomain("");
      setSelected(null);
      setChallengeName("");
      setChallengeValue("");
      setOffset(0);
      await loadRequests(0);
    }, "Onboarding request submitted for domain proof.", false);
  }

  async function post(
    path: string,
    successMessage: string,
    body?: object,
    idempotent = false,
    onResult?: (data: unknown) => void
  ) {
    await perform(async () => {
      const headers: Record<string, string> = {
        "X-CSRF-Token": csrfToken,
      };
      if (body) headers["Content-Type"] = "application/json";
      if (idempotent) headers["Idempotency-Key"] = idempotencyKey();
      const { data } = await requestJson<unknown>(path, {
        method: "POST",
        headers,
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      onResult?.(data);
    }, successMessage);
  }

  async function logout() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await requestJson("api/v1/platform/auth/oidc/logout", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      });
      setView("signed-out");
      setCsrfToken("");
      setOperatorId("");
      setSelected(null);
      setItems([]);
      setNotice("Platform session revoked.");
    } catch (caught) {
      setError(messageFromError(caught));
    } finally {
      setBusy(false);
    }
  }

  if (view === "checking") {
    return <p role="status" className="p-8 text-sm text-slate-600">Checking platform session…</p>;
  }
  if (view === "signed-out") {
    return (
      <section className="rounded-xl2 border border-slate-200 bg-white p-8 shadow-card">
        <h1 className="font-display text-2xl font-semibold text-navy-800">Platform sign-in required</h1>
        <p className="mt-2 text-sm leading-6 text-slate-600">
          Authenticate through the configured platform identity provider. Community membership does not grant platform access.
        </p>
        {notice && <p role="status" className="mt-3 text-sm text-emerald-800">{notice}</p>}
        {error && <p role="alert" className="mt-4 text-sm text-red-700">{error}</p>}
        <a
          href="/backend-api/api/v1/platform/auth/oidc/start"
          className="mt-5 inline-flex rounded-lg bg-navy-700 px-5 py-3 text-sm font-semibold text-white"
        >
          Sign in with platform identity provider
        </a>
      </section>
    );
  }
  if (view === "unavailable") {
    return (
      <section role="alert" className="rounded-xl2 border border-amber-200 bg-amber-50 p-8">
        <h1 className="font-display text-xl font-semibold text-amber-950">Platform access unavailable</h1>
        <p className="mt-2 text-sm leading-6 text-amber-900">{error}</p>
        <p className="mt-2 text-sm leading-6 text-amber-900">
          Access is denied until FastAPI can verify an active platform operator session.
        </p>
      </section>
    );
  }
  if (view === "denied") {
    return (
      <section role="alert" className="rounded-xl2 border border-red-200 bg-red-50 p-8">
        <h1 className="font-display text-xl font-semibold text-red-950">Platform permission required</h1>
        <p className="mt-2 text-sm leading-6 text-red-900">{error}</p>
        <p className="mt-2 text-sm leading-6 text-red-900">
          Your identity provider sign-in succeeded, but the backend did not authorize community onboarding access.
        </p>
        <button type="button" onClick={() => void logout()} disabled={busy} className="mt-4 rounded-lg border border-red-300 px-4 py-2 text-sm font-semibold text-red-900 disabled:opacity-50">Log out</button>
      </section>
    );
  }

  return (
    <div className="grid gap-6">
      <section className="flex flex-wrap items-start justify-between gap-4 rounded-xl2 border border-slate-200 bg-white p-6 shadow-card">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">Platform onboarding</p>
          <h1 className="mt-2 font-display text-2xl font-semibold text-navy-800">Community requests</h1>
          <p className="mt-2 text-sm text-slate-600">Operator ID: <span className="font-mono">{operatorId}</span></p>
        </div>
        <button
          type="button"
          onClick={() => void logout()}
          disabled={busy}
          className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 disabled:opacity-50"
        >
          Log out
        </button>
      </section>

      {error && <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">{error}</p>}
      {notice && <p role="status" className="rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800">{notice}</p>}

      <section className="rounded-xl2 border border-slate-200 bg-white p-6 shadow-card">
        <h2 className="font-display text-lg font-semibold text-navy-800">Submit a community request</h2>
        <p className="mt-1 text-sm leading-6 text-slate-600">
          This creates a pending onboarding request. It does not create a membership, administrator, grant, or trusted domain.
        </p>
        <form onSubmit={createRequest} className="mt-4 grid gap-3 sm:grid-cols-[1fr_1.5fr_auto]">
          <label className="grid gap-1 text-sm font-medium text-slate-700">
            Community slug
            <input required minLength={1} maxLength={63} pattern="[a-zA-Z0-9][a-zA-Z0-9-]*" value={tenantSlug} onChange={(event) => setTenantSlug(event.target.value)} className="rounded-lg border border-slate-300 px-3 py-2" />
          </label>
          <label className="grid gap-1 text-sm font-medium text-slate-700">
            Hostname
            <input required maxLength={253} type="text" value={domain} onChange={(event) => setDomain(event.target.value)} placeholder="community.example.org" className="rounded-lg border border-slate-300 px-3 py-2" />
          </label>
          <button disabled={busy} className="self-end rounded-lg bg-navy-700 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">Submit</button>
        </form>
      </section>

      <section className="rounded-xl2 border border-slate-200 bg-white p-6 shadow-card">
        <div className="flex items-center justify-between gap-3">
          <h2 className="font-display text-lg font-semibold text-navy-800">Onboarding requests</h2>
          <button type="button" onClick={() => void refresh()} disabled={busy} className="text-sm font-semibold text-emerald-800 disabled:opacity-50">Refresh</button>
        </div>
        {loadingList && items.length === 0 ? (
          <p role="status" className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">Loading onboarding requests…</p>
        ) : items.length === 0 ? (
          <p className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">No onboarding requests on this page.</p>
        ) : (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[650px] text-left text-sm">
              <thead className="border-b text-xs uppercase tracking-wide text-slate-500"><tr><th className="py-2 pr-4">Community</th><th className="py-2 pr-4">Hostname</th><th className="py-2 pr-4">State</th><th className="py-2 pr-4">Domain verified</th><th className="py-2">Details</th></tr></thead>
              <tbody className="divide-y divide-slate-100">
                {items.map((item) => (
                  <tr key={item.request_id}>
                    <td className="py-3 pr-4 font-medium text-navy-800">{item.tenant_slug}</td>
                    <td className="py-3 pr-4">{item.domain}</td>
                    <td className="py-3 pr-4">{item.state}</td>
                    <td className="py-3 pr-4">{item.domain_verified ? "Yes" : "No"}</td>
                    <td className="py-3"><button type="button" onClick={() => {
                      setChallengeValue("");
                      setChallengeName("");
                      setActivationEligibility(null);
                      setActivationIntentPending(false);
                      void Promise.all([
                        loadDetail(item.request_id),
                        loadActivationEligibility(item.request_id),
                      ]).catch(reportFailure);
                    }} className="font-semibold text-emerald-800 underline">View</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-4 flex items-center justify-between text-sm">
          <span className="text-slate-500">Page offset {offset}</span>
          <div className="flex gap-2">
            <button type="button" disabled={busy || loadingList || offset === 0} onClick={() => void loadRequests(Math.max(0, offset - 25)).catch(reportFailure)} className="rounded border px-3 py-1.5 disabled:opacity-40">Previous</button>
            <button type="button" disabled={busy || loadingList || items.length < 25} onClick={() => void loadRequests(offset + 25).catch(reportFailure)} className="rounded border px-3 py-1.5 disabled:opacity-40">Next</button>
          </div>
        </div>
      </section>

      {selected && (
        <section className="rounded-xl2 border border-slate-200 bg-white p-6 shadow-card">
          {activationEligibility && (
            <div
              role={activationEligibility.eligible ? "status" : "alert"}
              className={`mb-4 rounded-lg p-3 text-sm ${
                activationEligibility.eligible
                  ? "bg-emerald-50 text-emerald-900"
                  : "bg-amber-50 text-amber-950"
              }`}
            >
              {activationEligibility.eligible ? (
                <p>
                  Backend activation checks currently pass. Creating an intent
                  records an audited request only; it does not activate the
                  tenant.
                </p>
              ) : (
                <>
                  <p className="font-semibold">
                    Activation is blocked by backend readiness checks:
                  </p>
                  <ul className="mt-1 list-disc pl-5">
                    {activationEligibility.blockers.map((blocker) => (
                      <li key={blocker}>{blocker}</li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          )}
          {activationIntentPending && (
            <p role="status" className="mb-4 rounded-lg bg-blue-50 p-3 text-sm text-blue-900">
              An activation intent is awaiting external audit acknowledgement. The tenant is not active, and another intent cannot be submitted from this view.
            </p>
          )}
          {selected.history.some(
            (entry) =>
              entry.action === "onboarding.review.submit" &&
              entry.outcome === "success"
          ) && selected.state === "PENDING_ACTIVATION_REVIEW" && (
            <p role="status" className="mb-4 rounded-lg bg-blue-50 p-3 text-sm text-blue-900">
              Independent review is recorded. Tenant activation still requires separate server-side readiness and audit acknowledgement.
            </p>
          )}
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="font-display text-lg font-semibold text-navy-800">{selected.tenant_slug}</h2>
              <p className="mt-1 text-sm text-slate-600">{selected.domain} · {selected.state}</p>
            </div>
            <button type="button" onClick={() => setSelected(null)} className="text-sm font-semibold text-slate-600">Close details</button>
          </div>
          <p className="mt-3 text-xs text-slate-500">Request ID: <span className="font-mono">{selected.request_id}</span></p>
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" disabled={busy || selected.domain_verified || selected.state !== "PENDING_DOMAIN_PROOF"} onClick={() => void post(requestPath(selected.request_id, "/domains/challenge"), "DNS challenge issued.", undefined, true, (data) => {
              const result = data as { dns_txt_name?: unknown; dns_txt_value?: unknown } | null;
              setChallengeName(typeof result?.dns_txt_name === "string" ? result.dns_txt_name : "");
              setChallengeValue(typeof result?.dns_txt_value === "string" ? result.dns_txt_value : "");
            })} className="rounded-lg border px-3 py-2 text-sm disabled:opacity-40">Issue DNS challenge</button>
            <button type="button" disabled={busy || selected.domain_verified || selected.state !== "PENDING_DOMAIN_PROOF"} onClick={() => void post(requestPath(selected.request_id, "/domains/verify"), "Domain verification checked.")} className="rounded-lg border px-3 py-2 text-sm disabled:opacity-40">Verify DNS proof</button>
            <button type="button" disabled={busy || selected.state !== "PENDING_ACTIVATION_REVIEW" || selected.history.some((entry) => entry.action === "onboarding.review.submit" && entry.outcome === "success")} onClick={() => void post(requestPath(selected.request_id, "/review"), "Review decision recorded.", { decision: "approve", reason_code: "organization_verified" }, true)} className="rounded-lg border px-3 py-2 text-sm disabled:opacity-40">Approve review</button>
            <button type="button" disabled={busy || selected.state !== "PENDING_ACTIVATION_REVIEW" || selected.history.some((entry) => entry.action === "onboarding.review.submit" && entry.outcome === "success")} onClick={() => void post(requestPath(selected.request_id, "/reject"), "Request rejected.", { reason_code: "request_ineligible" }, true)} className="rounded-lg border border-red-200 px-3 py-2 text-sm text-red-800 disabled:opacity-40">Reject request</button>
            <button
              type="button"
              disabled={
                busy ||
                !activationEligibility?.eligible ||
                activationIntentPending
              }
              onClick={() =>
                void post(
                  requestPath(selected.request_id, "/activation-intents"),
                  "Activation intent recorded and awaiting external audit acknowledgement. The tenant is not active.",
                  undefined,
                  true,
                  (data) => {
                    if (
                      !data ||
                      typeof data !== "object" ||
                      !("request_id" in data) ||
                      data.request_id !== selected.request_id ||
                      !("activation_attempt_id" in data) ||
                      typeof data.activation_attempt_id !== "string" ||
                      !("state" in data) ||
                      data.state !== "PENDING_AUDIT_ACK" ||
                      !("created_at" in data) ||
                      typeof data.created_at !== "string" ||
                      !("replayed" in data) ||
                      typeof data.replayed !== "boolean"
                    ) {
                      throw new Error("FastAPI returned an invalid activation intent.");
                    }
                    const intent: ActivationIntentResult = {
                      request_id: data.request_id,
                      activation_attempt_id: data.activation_attempt_id,
                      state: data.state,
                      created_at: data.created_at,
                      replayed: data.replayed,
                    };
                    setActivationIntentPending(
                      intent.state === "PENDING_AUDIT_ACK"
                    );
                  }
                )
              }
              className="rounded-lg border border-emerald-300 px-3 py-2 text-sm text-emerald-900 disabled:opacity-40"
            >
              Request activation intent
            </button>
          </div>
          {challengeValue && (
            <div role="status" className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm">
              <p className="font-semibold text-amber-950">Publish this DNS TXT record:</p>
                <p className="mt-2 break-all font-mono">Name: {challengeName}</p>
                <p className="mt-1 break-all font-mono">Value: {challengeValue}</p>
            </div>
          )}
          <h3 className="mt-6 text-sm font-semibold text-navy-800">Audit history</h3>
          {selected.history.length === 0 ? (
            <p className="mt-2 text-sm text-slate-500">No history entries were returned.</p>
          ) : (
            <ol className="mt-2 grid gap-2">
              {selected.history.map((entry, index) => (
                <li key={`${entry.event_type}-${entry.occurred_at}-${index}`} className="rounded-lg bg-slate-50 p-3 text-sm">
                  <span className="font-medium">{entry.action}</span> · {entry.outcome} · {new Date(entry.occurred_at).toLocaleString()}
                  {entry.reason_code && <span> · {entry.reason_code}</span>}
                </li>
              ))}
            </ol>
          )}
        </section>
      )}
      <p className="text-xs leading-5 text-slate-500">Activation and administrator provisioning remain controlled by server-side readiness and separate approved identity/invitation contracts. This screen does not grant permissions or activate a tenant.</p>
    </div>
  );
}
