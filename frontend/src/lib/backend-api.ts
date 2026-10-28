const backendApiPrefix = "/backend-api";

/** Build a same-origin URL handled by the bounded Next.js backend route. */
export function backendApiPath(path: string): string {
  const normalizedPath = path.replace(/^\/+/, "");

  if (!normalizedPath || normalizedPath.split("/").includes("..")) {
    throw new Error("A valid backend API path is required.");
  }

  return `${backendApiPrefix}/${normalizedPath}`;
}

export interface TenantSummary {
  id: string;
  slug: string;
  name?: string | null;
}

export interface AuthenticatedIdentity {
  user_id: string;
  username: string | null;
  role: "SUPER_ADMIN" | "ADMIN" | "AGENT" | string;
  status: string;
  tenant_id: string | null;
  membership_id: string | null;
  csrf_token: string;
  expires_at: string;
  must_change_password?: boolean;
  tenant?: TenantSummary | null;
  context?: Record<string, unknown> | null;
}

export interface LoginResponse {
  user_id: string;
  username: string;
  role: string;
  status: string;
  tenant_id: string | null;
  membership_id: string | null;
  tenant?: TenantSummary | null;
  csrf_token: string;
  expires_at: string;
  must_change_password?: boolean;
  context: Record<string, unknown>;
}

/** Call the narrowly allowlisted FastAPI proxy; other API paths fail closed. */
export function fetchBackendApi(
  path: string,
  init?: RequestInit
): Promise<Response> {
  return fetch(backendApiPath(path), init);
}

export type SessionCheckResult =
  | { status: "authenticated"; identity: AuthenticatedIdentity }
  | { status: "unauthenticated" }
  | { status: "error"; message: string };

export async function checkSession(): Promise<SessionCheckResult> {
  try {
    const response = await fetchBackendApi("api/v1/auth/me", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (response.status === 401) {
      return { status: "unauthenticated" };
    }
    if (!response.ok) {
      return {
        status: "error",
        message: "The authentication service is temporarily unavailable. Please try again shortly.",
      };
    }
    const identity = (await response.json()) as AuthenticatedIdentity;
    return { status: "authenticated", identity };
  } catch {
    return {
      status: "error",
      message: "Network connection to the backend server failed. Please check your connection.",
    };
  }
}

export async function getAuthenticatedIdentity(): Promise<AuthenticatedIdentity | null> {
  try {
    const response = await fetchBackendApi("api/v1/auth/me", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) return null;
    return (await response.json()) as AuthenticatedIdentity;
  } catch {
    return null;
  }
}

export async function logoutSession(): Promise<void> {
  const identityResponse = await fetchBackendApi("api/v1/auth/me", {
    credentials: "same-origin",
    cache: "no-store",
  });

  if (identityResponse.ok) {
    const identity = (await identityResponse.json()) as { csrf_token?: unknown };
    if (typeof identity.csrf_token !== "string" || !identity.csrf_token) {
      throw new Error("FastAPI returned an invalid session security token.");
    }
    const logoutResponse = await fetchBackendApi("api/v1/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": identity.csrf_token },
    });
    if (!logoutResponse.ok && logoutResponse.status !== 401) {
      throw new Error("FastAPI could not revoke the current session.");
    }
    const revokedSessionCheck = await fetchBackendApi("api/v1/auth/me", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (revokedSessionCheck.status !== 401) {
      throw new Error("FastAPI still accepts the current session.");
    }
  } else if (identityResponse.status !== 401) {
    throw new Error("FastAPI could not verify the current session.");
  }

  const legacyCookieResponse = await fetch("/api/auth/logout", {
    method: "POST",
    credentials: "same-origin",
  });
  if (!legacyCookieResponse.ok) {
    throw new Error("The browser session could not be cleared.");
  }
}
