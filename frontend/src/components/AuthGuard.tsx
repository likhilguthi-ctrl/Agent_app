"use client";

import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import {
  AuthenticatedIdentity,
  checkSession,
  logoutSession,
} from "@/lib/backend-api";
import { Spinner } from "@/components/ui";

interface AuthContextValue {
  identity: AuthenticatedIdentity;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthGuard or AuthProvider");
  }
  return context;
}

export default function AuthGuard({
  allowedRoles,
  children,
}: {
  allowedRoles?: string[];
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [identity, setIdentity] = useState<AuthenticatedIdentity | null>(null);
  const [loading, setLoading] = useState(true);
  const [unauthorized, setUnauthorized] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const verifyAuth = useCallback(async () => {
    setLoading(true);
    setErrorMessage(null);
    setUnauthorized(false);

    const result = await checkSession();

    if (result.status === "unauthenticated") {
      router.replace("/login");
      return;
    }

    if (result.status === "error") {
      setErrorMessage(result.message);
      setLoading(false);
      return;
    }

    const currentIdentity = result.identity;

    if (allowedRoles && allowedRoles.length > 0 && !allowedRoles.includes(currentIdentity.role)) {
      // Role mismatch: redirect to the user's appropriate dashboard if recognized
      if (currentIdentity.role === "SUPER_ADMIN") {
        router.replace("/superadmin/dashboard");
        return;
      } else if (currentIdentity.role === "ADMIN") {
        router.replace("/admin/dashboard");
        return;
      } else if (currentIdentity.role === "AGENT") {
        router.replace("/agent/dashboard");
        return;
      } else {
        setUnauthorized(true);
        setLoading(false);
        return;
      }
    }

    setIdentity(currentIdentity);
    setLoading(false);
  }, [allowedRoles, router]);

  useEffect(() => {
    void verifyAuth();
  }, [verifyAuth]);

  async function handleLogout() {
    try {
      await logoutSession();
    } finally {
      router.replace("/login");
    }
  }

  // 1. Loading state
  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-sand-50">
        <div className="flex flex-col items-center gap-3">
          <Spinner className="w-8 h-8 text-emerald-600" />
          <p className="text-sm font-medium text-slate-500">Verifying session...</p>
        </div>
      </div>
    );
  }

  // 2. Service-error state (backend unavailable / network failure - prevents infinite redirect loop)
  if (errorMessage) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-sand-50 p-4">
        <div className="max-w-md w-full bg-white p-7 rounded-2xl border border-slate-200 shadow-card text-center">
          <div className="w-12 h-12 mx-auto rounded-full bg-amber-50 text-amber-600 flex items-center justify-center text-xl font-bold">
            !
          </div>
          <h2 className="mt-4 font-display text-lg font-semibold text-navy-800">
            Authentication Service Unavailable
          </h2>
          <p className="mt-2 text-sm text-slate-600 leading-relaxed">
            {errorMessage}
          </p>
          <div className="mt-6 flex flex-col sm:flex-row gap-3 justify-center">
            <button
              onClick={() => void verifyAuth()}
              className="inline-flex items-center justify-center px-4 py-2.5 rounded-lg bg-navy-700 text-white text-sm font-semibold hover:bg-navy-800 transition-colors"
            >
              Retry connection
            </button>
            <button
              onClick={() => void handleLogout()}
              className="inline-flex items-center justify-center px-4 py-2.5 rounded-lg border border-slate-300 bg-white text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
            >
              Sign out
            </button>
          </div>
        </div>
      </div>
    );
  }

  // 3. Unauthorized state
  if (unauthorized || !identity) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-sand-50 p-4">
        <div className="max-w-md w-full bg-white p-7 rounded-2xl border border-slate-200 shadow-card text-center">
          <div className="w-12 h-12 mx-auto rounded-full bg-red-50 text-red-600 flex items-center justify-center text-xl font-bold">
            ✕
          </div>
          <h2 className="mt-4 font-display text-lg font-semibold text-navy-800">
            Access Denied
          </h2>
          <p className="mt-2 text-sm text-slate-600 leading-relaxed">
            Your account does not have permission to access this dashboard.
          </p>
          <button
            onClick={() => void handleLogout()}
            className="mt-6 inline-flex items-center justify-center px-5 py-2.5 rounded-lg bg-navy-700 text-white text-sm font-semibold hover:bg-navy-800 transition-colors"
          >
            Sign in with a different account
          </button>
        </div>
      </div>
    );
  }

  // 4. Authenticated state
  return (
    <AuthContext.Provider value={{ identity, logout: handleLogout }}>
      {children}
    </AuthContext.Provider>
  );
}
