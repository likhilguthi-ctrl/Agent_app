"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { fetchBackendApi, logoutSession } from "@/lib/backend-api";

interface AuthorizationContext {
  authentication: {
    status: "authenticated";
    session_expires_at: string;
  };
  principal: {
    type: "tenant_member";
    id: string;
    membership_id: string;
  };
  authorization: {
    status: "resolved";
    effective_permissions: string[];
    community_scope: {
      status: "resolved";
      resolution: "current_trusted_tenant";
      communities: Array<{ id: string; slug: string }>;
    };
  };
  employee_authority: { status: "unavailable"; reason_code: string };
  product_role: { status: "unavailable"; reason_code: string };
}

type LoadState =
  | { status: "loading" }
  | { status: "unauthenticated" }
  | { status: "error"; message: string }
  | { status: "authenticated"; context: AuthorizationContext };

export default function CommunityContextScreen() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [logoutError, setLogoutError] = useState("");

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const identityResponse = await fetchBackendApi("api/v1/auth/me", {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (!active) return;
        if (identityResponse.status === 401) {
          setState({ status: "unauthenticated" });
          return;
        }
        if (!identityResponse.ok) {
          setState({
            status: "error",
            message: "FastAPI could not validate the current session.",
          });
          return;
        }
        const contextResponse = await fetchBackendApi(
          "api/v1/auth/context",
          { credentials: "same-origin", cache: "no-store" }
        );
        if (!active) return;
        if (contextResponse.status === 401) {
          setState({ status: "unauthenticated" });
          return;
        }
        if (contextResponse.status === 403) {
          setState({
            status: "error",
            message: "This session is not authorized for the current tenant.",
          });
          return;
        }
        if (!contextResponse.ok) {
          setState({
            status: "error",
            message: "FastAPI could not resolve authorization context.",
          });
          return;
        }
        const context =
          (await contextResponse.json()) as AuthorizationContext;
        if (active) setState({ status: "authenticated", context });
      } catch {
        if (active) {
          setState({
            status: "error",
            message: "The FastAPI authentication service is unavailable.",
          });
        }
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (state.status !== "authenticated") return;
    const expiry = Date.parse(
      state.context.authentication.session_expires_at
    );
    if (!Number.isFinite(expiry)) {
      setState({
        status: "error",
        message: "FastAPI returned an invalid session expiry.",
      });
      return;
    }
    const timer = window.setTimeout(
      () => setState({ status: "unauthenticated" }),
      Math.max(0, expiry - Date.now())
    );
    return () => window.clearTimeout(timer);
  }, [state]);

  async function logout() {
    if (state.status !== "authenticated") return;
    setBusy(true);
    setLogoutError("");
    try {
      await logoutSession();
      setState({ status: "unauthenticated" });
    } catch (cause) {
      setLogoutError(
        cause instanceof Error
          ? cause.message
          : "Logout could not be verified."
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="min-h-screen bg-sand-50 px-5 py-10 sm:px-8">
      <div className="mx-auto w-full max-w-3xl space-y-6">
        <header>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-emerald-700">
            Community workspace
          </p>
          <h1 className="mt-2 font-display text-3xl font-semibold text-navy-800">
            Trusted access context
          </h1>
          <p className="mt-3 text-sm leading-6 text-slate-600">
            This read-only view is loaded from FastAPI. The browser does not
            supply or select a tenant, identity, or role.
          </p>
        </header>

        <section
          aria-live="polite"
          className="rounded-xl2 border border-slate-200 bg-white p-5 shadow-card sm:p-7"
        >
          {state.status === "loading" ? (
            <p role="status" className="text-sm text-slate-600">
              Loading trusted authentication context…
            </p>
          ) : state.status === "unauthenticated" ? (
            <div className="space-y-3">
              <h2 className="font-display text-lg font-semibold text-navy-800">
                Sign-in required
              </h2>
              <p className="text-sm text-slate-600">
                FastAPI did not return an authenticated session for this
                request.
              </p>
              <Link
                href="/login"
                className="inline-flex min-h-11 items-center justify-center rounded-lg bg-navy-700 px-4 py-2 text-sm font-semibold text-white"
              >
                Go to sign in
              </Link>
            </div>
          ) : state.status === "error" ? (
            <div className="space-y-3">
              <h2 className="font-display text-lg font-semibold text-navy-800">
                Context unavailable
              </h2>
              <p role="alert" className="text-sm text-red-700">
                {state.message}
              </p>
              <Link href="/login" className="text-sm font-medium text-navy-700 underline">
                Return to sign in
              </Link>
            </div>
          ) : (
            <div className="space-y-6">
              <div>
                <h2 className="font-display text-lg font-semibold text-navy-800">
                  Authenticated community member
                </h2>
                <p role="status" className="mt-1 text-sm text-emerald-800">
                  Session and authorization context resolved by FastAPI.
                </p>
              </div>

              {state.context.authorization.community_scope.communities.length ? (
                <section aria-labelledby="community-scope-heading">
                  <h3
                    id="community-scope-heading"
                    className="text-sm font-semibold text-slate-800"
                  >
                    Current trusted community
                  </h3>
                  <ul className="mt-2 space-y-2">
                    {state.context.authorization.community_scope.communities.map(
                      (community) => (
                        <li
                          key={community.id}
                          className="rounded-lg border border-slate-200 p-3"
                        >
                          <p className="font-medium text-navy-800">
                            {community.slug}
                          </p>
                          <p className="mt-1 break-all font-mono text-xs text-slate-500">
                            {community.id}
                          </p>
                        </li>
                      )
                    )}
                  </ul>
                </section>
              ) : (
                <p className="text-sm text-slate-600">
                  No community scope was returned.
                </p>
              )}

              <section aria-labelledby="permissions-heading">
                <h3
                  id="permissions-heading"
                  className="text-sm font-semibold text-slate-800"
                >
                  Effective permissions
                </h3>
                {state.context.authorization.effective_permissions.length ? (
                  <ul className="mt-2 list-inside list-disc space-y-1 text-sm text-slate-700">
                    {state.context.authorization.effective_permissions.map(
                      (permission) => (
                        <li key={permission}>{permission}</li>
                      )
                    )}
                  </ul>
                ) : (
                  <p className="mt-2 text-sm text-slate-600">
                    The backend resolved authorization successfully; no
                    application permissions are currently assigned.
                  </p>
                )}
              </section>

              <dl className="grid gap-3 border-t border-slate-200 pt-4 text-sm sm:grid-cols-2">
                <div>
                  <dt className="font-medium text-slate-700">Principal</dt>
                  <dd className="mt-1 text-slate-600">
                    {state.context.principal.type}
                  </dd>
                </div>
                <div>
                  <dt className="font-medium text-slate-700">Session expiry</dt>
                  <dd className="mt-1 text-slate-600">
                    {new Date(
                      state.context.authentication.session_expires_at
                    ).toLocaleString()}
                  </dd>
                </div>
                <div>
                  <dt className="font-medium text-slate-700">Product role</dt>
                  <dd className="mt-1 text-slate-600">
                    Unavailable: {state.context.product_role.reason_code}
                  </dd>
                </div>
                <div>
                  <dt className="font-medium text-slate-700">Employee authority</dt>
                  <dd className="mt-1 text-slate-600">
                    Unavailable: {state.context.employee_authority.reason_code}
                  </dd>
                </div>
              </dl>

              <button
                type="button"
                onClick={logout}
                disabled={busy}
                className="inline-flex min-h-11 items-center justify-center rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-navy-800 disabled:opacity-60"
              >
                {busy ? "Revoking session…" : "Log out"}
              </button>
              {logoutError && (
                <p role="alert" className="text-sm text-red-700">
                  {logoutError}
                </p>
              )}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
