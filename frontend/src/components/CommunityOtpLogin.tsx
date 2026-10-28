"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { fetchBackendApi, logoutSession } from "@/lib/backend-api";
import {
  requestOtpNotification,
  verifyOtpNotification,
} from "@/lib/notifications";

interface AuthorizationContext {
  authentication: {
    status: "authenticated";
    session_expires_at: string;
  };
  authorization: {
    effective_permissions: string[];
    community_scope: {
      communities: Array<{ id: string; slug: string }>;
    };
  };
  product_role: {
    status: "unavailable" | "resolved";
    reason_code?: string;
  };
  employee_authority: {
    status: "unavailable" | "resolved";
    reason_code?: string;
  };
}

function errorMessage(response: Response): string {
  if (response.status === 401) {
    return "The code is invalid, expired, or the membership is not active.";
  }
  if (response.status === 403) {
    return "This request did not satisfy the local development origin policy.";
  }
  return "Authentication is unavailable. Check the FastAPI development configuration.";
}

export default function CommunityOtpLogin({
  developmentRuntime,
}: {
  developmentRuntime: boolean;
}) {
  const [mobile, setMobile] = useState("");
  const [otp, setOtp] = useState("");
  const [challengeId, setChallengeId] = useState("");
  const [context, setContext] = useState<AuthorizationContext | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);

  async function loadContext(): Promise<boolean> {
    const identityResponse = await fetchBackendApi("api/v1/auth/me", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!identityResponse.ok) {
      if (identityResponse.status !== 401) {
        setError("FastAPI could not validate the community session.");
      }
      return false;
    }
    const contextResponse = await fetchBackendApi("api/v1/auth/context", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!contextResponse.ok) {
      setError(
        contextResponse.status === 401
          ? "The community session expired. Sign in again."
          : "FastAPI could not resolve trusted community access."
      );
      return false;
    }
    setContext((await contextResponse.json()) as AuthorizationContext);
    return true;
  }

  useEffect(() => {
    let mounted = true;
    if (!developmentRuntime) {
      setReady(true);
      return () => {
        mounted = false;
      };
    }
    void fetchBackendApi("api/v1/auth/me", {
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(async (response) => {
        if (!mounted) return;
        if (response.ok) {
          const contextResponse = await fetchBackendApi(
            "api/v1/auth/context",
            { credentials: "same-origin", cache: "no-store" }
          );
          if (!mounted) return;
          if (contextResponse.ok) {
            setContext((await contextResponse.json()) as AuthorizationContext);
          } else {
            setError(
              contextResponse.status === 401
                ? "The community session expired. Sign in again."
                : "FastAPI could not resolve trusted community access."
            );
          }
        } else if (response.status !== 401) {
          setError("FastAPI authentication is unavailable.");
        }
      })
      .catch(() => {
        if (mounted) setError("FastAPI authentication is unavailable.");
      })
      .finally(() => {
        if (mounted) setReady(true);
      });
    return () => {
      mounted = false;
    };
  }, [developmentRuntime]);

  useEffect(() => {
    if (!context) return;
    const expiry = Date.parse(context.authentication.session_expires_at);
    if (!Number.isFinite(expiry)) {
      setContext(null);
      setError("FastAPI returned an invalid session expiry.");
      return;
    }
    const timer = window.setTimeout(async () => {
      setContext(null);
      setChallengeId("");
      setOtp("");
      setError("The community session expired. Sign in again.");
      try {
        await logoutSession();
      } catch {
        setError(
          "The community session expired, but browser session cleanup could not be verified."
        );
      }
    }, Math.max(0, expiry + 1000 - Date.now()));
    return () => window.clearTimeout(timer);
  }, [context]);

  async function requestOtp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const accepted = await requestOtpNotification({
        mobileNumber: mobile,
        purpose: "login",
      });
      setChallengeId(accepted.challengeId);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Could not start authentication."
      );
    } finally {
      setBusy(false);
    }
  }

  async function verifyOtp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await verifyOtpNotification({
        mobileNumber: mobile,
        otp,
        challengeId,
        purpose: "login",
      });
      if (!(await loadContext())) {
        await logoutSession().catch(() => {
          throw new Error(
            "Trusted access could not be resolved and the session revocation could not be verified."
          );
        });
        throw new Error(
          "The session was revoked because trusted access could not be resolved."
        );
      }
      setOtp("");
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Authentication failed."
      );
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    setBusy(true);
    setError("");
    try {
      await logoutSession();
      setContext(null);
      setChallengeId("");
      setOtp("");
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Could not revoke the session."
      );
    } finally {
      setBusy(false);
    }
  }

  if (!developmentRuntime) return null;

  const community = context?.authorization.community_scope.communities[0];

  return (
    <section className="rounded-xl2 border border-slate-200 bg-white p-5 shadow-card sm:p-7">
      <p className="text-xs font-semibold uppercase tracking-wide text-emerald-700">
        Local development
      </p>
      <h2 className="mt-2 font-display text-xl font-semibold text-navy-800">
        Community member sign-in
      </h2>
      {!ready ? (
        <p className="mt-3 text-sm text-slate-600">Checking session…</p>
      ) : context ? (
        <div className="mt-4 space-y-3">
          <p role="status" className="text-sm text-slate-700">
            Signed in to <strong>{community?.slug ?? "your community"}</strong>.
          </p>
          <p className="text-sm leading-6 text-slate-600">
            Access is limited to permissions resolved by FastAPI for this
            session:
          </p>
          {context.authorization.effective_permissions.length > 0 ? (
            <ul className="list-inside list-disc text-sm text-slate-700">
              {context.authorization.effective_permissions.map((permission) => (
                <li key={permission}>{permission}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-slate-600">
              No application permissions are currently assigned.
            </p>
          )}
          <p className="text-xs leading-5 text-slate-500">
            Product-role and employee mappings are not approved, so privileged
            dashboard navigation remains unavailable.
          </p>
          <Link
            href="/community"
            className="inline-flex min-h-11 items-center justify-center rounded-lg bg-navy-700 px-4 py-2 text-sm font-semibold text-white"
          >
            View trusted context
          </Link>
          <button
            type="button"
            onClick={logout}
            disabled={busy}
            className="inline-flex min-h-11 items-center justify-center rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-navy-800 disabled:opacity-60"
          >
            {busy ? "Working…" : "Log out"}
          </button>
        </div>
      ) : (
        <>
          <p className="mt-3 text-sm leading-6 text-slate-600">
            Uses the configured development OTP only for an existing active
            membership. It does not create accounts or assign roles.
          </p>
          {!challengeId ? (
            <form onSubmit={requestOtp} className="mt-4 space-y-3">
              <label className="block text-sm font-medium text-slate-700">
                Mobile number
                <input
                  autoComplete="tel"
                  inputMode="tel"
                  required
                  value={mobile}
                  onChange={(event) => setMobile(event.target.value)}
                  className="mt-1 min-h-11 w-full rounded-lg border border-slate-300 px-3"
                />
              </label>
              <button
                type="submit"
                disabled={busy}
                className="inline-flex min-h-11 w-full items-center justify-center rounded-lg bg-navy-700 px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
              >
                {busy ? "Starting…" : "Continue"}
              </button>
            </form>
          ) : (
            <form onSubmit={verifyOtp} className="mt-4 space-y-3">
              <label className="block text-sm font-medium text-slate-700">
                Development OTP
                <input
                  autoComplete="one-time-code"
                  inputMode="numeric"
                  required
                  value={otp}
                  onChange={(event) => setOtp(event.target.value)}
                  className="mt-1 min-h-11 w-full rounded-lg border border-slate-300 px-3"
                />
              </label>
              <button
                type="submit"
                disabled={busy}
                className="inline-flex min-h-11 w-full items-center justify-center rounded-lg bg-navy-700 px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
              >
                {busy ? "Verifying…" : "Verify and sign in"}
              </button>
              <button
                type="button"
                onClick={() => setChallengeId("")}
                className="w-full py-2 text-sm font-medium text-slate-600"
              >
                Use a different mobile number
              </button>
            </form>
          )}
        </>
      )}
      {error && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {error}
        </p>
      )}
    </section>
  );
}
