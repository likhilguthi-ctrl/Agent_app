import { isIP } from "node:net";
import { NextRequest, NextResponse } from "next/server";

const healthPath = ["api", "v1", "health"];
const authRoutes = new Map([
  ["api/v1/auth/login", { method: "POST", cookie: false }],
  ["api/v1/auth/otp/request", { method: "POST", cookie: false }],
  ["api/v1/auth/otp/verify", { method: "POST", cookie: false }],
  ["api/v1/auth/email-otp/request", { method: "POST", cookie: false }],
  ["api/v1/auth/email-otp/verify", { method: "POST", cookie: false }],
  ["api/v1/auth/otp/select-context", { method: "POST", cookie: false }],
  ["api/v1/auth/me", { method: "GET", cookie: true }],
  ["api/v1/auth/context", { method: "GET", cookie: true }],
  ["api/v1/auth/password/change", { method: "POST", cookie: true }],
  ["api/v1/auth/password-reset/request", { method: "POST", cookie: false }],
  ["api/v1/auth/password-reset/verify", { method: "POST", cookie: false }],
  ["api/v1/auth/password-reset/complete", { method: "POST", cookie: false }],
  ["api/v1/auth/logout", { method: "POST", cookie: true }],
]);
const authCookieName = "community_session";
const authCookiePath = "/backend-api/api/v1";
const platformSessionCookieName = "community_platform_session";
const platformSessionCookiePath = "/backend-api/api/v1/platform";
const platformBindingCookieName = "community_platform_oidc_binding";
const platformBindingCookiePath =
  "/backend-api/api/v1/platform/auth/oidc";
const platformCallbackQueryHeader =
  "x-internal-platform-oidc-callback-query";
const upstreamTimeoutMilliseconds = 2_000;
const uuidPattern =
  "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}";

function unavailableResponse(): NextResponse {
  return NextResponse.json(
    { error: "BACKEND_PROXY_UNAVAILABLE" },
    { status: 503, headers: { "Cache-Control": "no-store" } }
  );
}

function validateBackendOrigin(): URL | null {
  const configuredOrigin = (process.env.BACKEND_API_BASE_URL || "http://127.0.0.1:8000").trim();
  if (!configuredOrigin) return null;

  try {
    const url = new URL(configuredOrigin);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      !url.hostname ||
      url.username ||
      url.password ||
      url.pathname !== "/" ||
      url.search ||
      url.hash ||
      (process.env.NODE_ENV === "production" && url.protocol !== "https:")
    ) {
      return null;
    }
    return new URL(url.origin);
  } catch {
    return null;
  }
}

function pathSegments(context: { params: { path: string[] } }): string[] {
  return context.params.path;
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase();
  return (
    normalized === "localhost" ||
    normalized.endsWith(".localhost") ||
    normalized === "::1" ||
    normalized === "0.0.0.0" ||
    (isIP(normalized) === 4 && (normalized.startsWith("127.") || normalized === "0.0.0.0"))
  );
}

function isLocalDevelopmentRequest(
  request: NextRequest,
  requireOrigin: boolean
): boolean {
  const origin = request.headers.get("origin");
  const requestHost = request.headers.get("host");
  let originIsLocalRequest = false;
  if (origin && requestHost) {
    try {
      const parsedOrigin = new URL(origin);
      const hostHostname = (requestHost.split(":")[0] ?? "").toLowerCase();
      const hostPort = requestHost.includes(":")
        ? requestHost.split(":")[1]
        : request.nextUrl.protocol === "https:"
        ? "443"
        : "80";
      const originPort =
        parsedOrigin.port ||
        (parsedOrigin.protocol === "https:" ? "443" : "80");
      const samePort = hostPort === originPort;
      const bothLoopback =
        isLoopbackHostname(parsedOrigin.hostname) &&
        isLoopbackHostname(hostHostname);

      originIsLocalRequest =
        parsedOrigin.protocol === request.nextUrl.protocol &&
        (parsedOrigin.host.toLowerCase() === requestHost.toLowerCase() ||
          (bothLoopback && samePort)) &&
        isLoopbackHostname(parsedOrigin.hostname);
    } catch {
      originIsLocalRequest = false;
    }
  }
  const hostHeader = requestHost?.split(":")[0] ?? "";
  const requestHostIsLocal =
    isLoopbackHostname(request.nextUrl.hostname) ||
    isLoopbackHostname(hostHeader);
  return (
    process.env.NODE_ENV !== "production" &&
    requestHostIsLocal &&
    (originIsLocalRequest || (!requireOrigin && origin === null))
  );
}

function noStoreJson(body: { error: string }, status: number): NextResponse {
  return NextResponse.json(body, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

function expiredAuthCookie(request: NextRequest): string {
  const secure = request.nextUrl.protocol === "https:" ? "; Secure" : "";
  return `${authCookieName}=; Path=${authCookiePath}; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Lax${secure}`;
}

function expiredCookie(
  request: NextRequest,
  name: string,
  path: string
): string {
  const secure = request.nextUrl.protocol === "https:" ? "; Secure" : "";
  return `${name}=; Path=${path}; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Lax${secure}`;
}

function hasAllowedCookie(
  value: string,
  name: string,
  path: string,
  deleting: boolean,
  requireSecure: boolean
): boolean {
  const parts = value.split(";").map((part) => part.trim());
  const first = parts.shift() ?? "";
  const token = first.slice(name.length + 1);
  if (
    !first.startsWith(`${name}=`) ||
    (deleting ? (first !== `${name}=` && first !== `${name}=""`) : !/^[A-Za-z0-9_-]{1,128}$/.test(token))
  ) {
    return false;
  }
  const attributes = new Map(
    parts.map((part) => {
      const separator = part.indexOf("=");
      return separator === -1
        ? [part.toLowerCase(), ""]
        : [part.slice(0, separator).toLowerCase(), part.slice(separator + 1)];
    })
  );
  return (
    attributes.get("path") === path &&
    attributes.has("httponly") &&
    ["lax", "strict"].includes((attributes.get("samesite") ?? "").toLowerCase()) &&
    !attributes.has("domain") &&
    (!requireSecure || attributes.has("secure")) &&
    (!deleting || attributes.get("max-age") === "0")
  );
}

type PlatformRoute = {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  session: boolean;
  body: "none" | "json";
  csrf: boolean;
  callback?: boolean;
  start?: boolean;
};

function platformRoute(
  routePath: string,
  method: string
): PlatformRoute | null {
  const fixed: Record<string, PlatformRoute> = {
    "api/v1/platform/auth/oidc/start": {
      method: "GET",
      session: false,
      body: "none",
      csrf: false,
      start: true,
    },
    "api/v1/platform/auth/oidc/callback": {
      method: "GET",
      session: false,
      body: "none",
      csrf: false,
      callback: true,
    },
    "api/v1/platform/auth/oidc/me": {
      method: "GET",
      session: true,
      body: "none",
      csrf: false,
    },
    "api/v1/platform/auth/oidc/logout": {
      method: "POST",
      session: true,
      body: "none",
      csrf: true,
    },
    "api/v1/platform/onboarding-requests": {
      method: "GET",
      session: true,
      body: "none",
      csrf: false,
    },
    "api/v1/platform/activity": {
      method: "GET",
      session: true,
      body: "none",
      csrf: false,
    },
  };
  const exact = fixed[routePath];
  if (exact && exact.method === method) return exact;
  if (
    routePath === "api/v1/platform/onboarding-requests" &&
    method === "POST"
  ) {
    return {
      method: "POST",
      session: true,
      body: "json",
      csrf: true,
    };
  }

  if (routePath === "api/v1/platform/communities") {
    if (method === "GET") {
      return { method: "GET", session: true, body: "none", csrf: false };
    }
    if (method === "POST") {
      return { method: "POST", session: true, body: "json", csrf: true };
    }
    return null;
  }

  if (routePath === "api/v1/platform/administrators") {
    if (method === "GET") {
      return { method: "GET", session: true, body: "none", csrf: false };
    }
    return null;
  }

  const platformAdminsBase = "api/v1/platform/administrators/";
  if (routePath.startsWith(platformAdminsBase)) {
    const suffix = routePath.slice(platformAdminsBase.length);
    const id = uuidPattern;
    if (new RegExp(`^${id}$`).test(suffix) && method === "GET") {
      return { method: "GET", session: true, body: "none", csrf: false };
    }
    return null;
  }

  if (routePath === "api/v1/platform/notifications") {
    if (method === "GET") {
      return { method: "GET", session: true, body: "none", csrf: false };
    }
    return null;
  }

  if (routePath === "api/v1/platform/notifications/unread-count") {
    if (method === "GET") {
      return { method: "GET", session: true, body: "none", csrf: false };
    }
    return null;
  }

  if (routePath === "api/v1/platform/notifications/mark-all-read") {
    if (method === "POST") {
      return { method: "POST", session: true, body: "none", csrf: true };
    }
    return null;
  }

  const platformNotifsBase = "api/v1/platform/notifications/";
  if (routePath.startsWith(platformNotifsBase)) {
    const suffix = routePath.slice(platformNotifsBase.length);
    const id = uuidPattern;
    if (new RegExp(`^${id}/read$`).test(suffix) && method === "PATCH") {
      return { method: "PATCH", session: true, body: "none", csrf: true };
    }
    return null;
  }

  if (routePath === "api/v1/platform/agents") {
    if (method === "GET") {
      return { method: "GET", session: true, body: "none", csrf: false };
    }
    return null;
  }

  if (routePath === "api/v1/platform/creation-requests") {
    if (method === "GET") {
      return { method: "GET", session: true, body: "none", csrf: false };
    }
    return null;
  }

  const platformReviewBase = "api/v1/platform/creation-requests/";
  if (routePath.startsWith(platformReviewBase)) {
    const suffix = routePath.slice(platformReviewBase.length);
    const id = uuidPattern;
    if (new RegExp(`^${id}/review$`).test(suffix) && method === "POST") {
      return { method: "POST", session: true, body: "json", csrf: true };
    }
    return null;
  }

  if (routePath === "api/v1/platform/agent-lifecycle-requests") {
    if (method === "GET") {
      return { method: "GET", session: true, body: "none", csrf: false };
    }
    return null;
  }

  const alrPlatformBase = "api/v1/platform/agent-lifecycle-requests/";
  if (routePath.startsWith(alrPlatformBase)) {
    const suffix = routePath.slice(alrPlatformBase.length);
    const id = uuidPattern;
    if (new RegExp(`^${id}/decision$`).test(suffix) && method === "POST") {
      return { method: "POST", session: true, body: "json", csrf: true };
    }
    return null;
  }

  if (routePath === "api/v1/platform/subscription-plans") {
    if (method === "GET") {
      return { method: "GET", session: true, body: "none", csrf: false };
    }
    if (method === "POST") {
      return { method: "POST", session: true, body: "json", csrf: true };
    }
    return null;
  }

  const platformPlansBase = "api/v1/platform/subscription-plans/";
  if (routePath.startsWith(platformPlansBase)) {
    const suffix = routePath.slice(platformPlansBase.length);
    const id = uuidPattern;
    if (new RegExp(`^${id}$`).test(suffix)) {
      if (method === "GET") {
        return { method: "GET", session: true, body: "none", csrf: false };
      }
      if (method === "PATCH") {
        return { method: "PATCH", session: true, body: "json", csrf: true };
      }
      if (method === "DELETE") {
        return { method: "DELETE", session: true, body: "none", csrf: true };
      }
    }
    return null;
  }

  const communitiesBase = "api/v1/platform/communities/";
  if (routePath.startsWith(communitiesBase)) {
    const suffix = routePath.slice(communitiesBase.length);
    const id = uuidPattern;
    const communityPatterns: Array<[RegExp, PlatformRoute]> = [
      [
        new RegExp(`^${id}$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}$`),
        { method: "PATCH", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/administrators$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/administrators$`),
        { method: "POST", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/administrators/${id}$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/administrators/${id}$`),
        { method: "PATCH", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/managers$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/managers$`),
        { method: "POST", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/managers/${id}$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/managers/${id}/agents$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/agents/${id}/subscribers$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/agents$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/agents$`),
        { method: "POST", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/commission$`),
        { method: "PATCH", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/commission/audit-logs$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/photo-policy$`),
        { method: "PATCH", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/regions$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/appointments$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/appointments$`),
        { method: "POST", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/appointments/${id}/approve$`),
        { method: "POST", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/appointments/${id}/reject$`),
        { method: "POST", session: true, body: "json", csrf: true },
      ],
      [
        new RegExp(`^${id}/appointments/${id}$`),
        { method: "GET", session: true, body: "none", csrf: false },
      ],
      [
        new RegExp(`^${id}/appointments/${id}/cancel$`),
        { method: "POST", session: true, body: "json", csrf: true },
      ],
    ];
    return (
      communityPatterns.find(
        ([pattern, route]) => pattern.test(suffix) && route.method === method
      )?.[1] ?? null
    );
  }

  const base = "api/v1/platform/onboarding-requests/";
  if (!routePath.startsWith(base)) return null;
  const suffix = routePath.slice(base.length);
  const requestId = uuidPattern;
  const patterns: Array<[RegExp, PlatformRoute]> = [
    [
      new RegExp(`^${requestId}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^${requestId}/domains/challenge$`),
      { method: "POST", session: true, body: "none", csrf: true },
    ],
    [
      new RegExp(`^${requestId}/domains/verify$`),
      { method: "POST", session: true, body: "none", csrf: true },
    ],
    [
      new RegExp(`^${requestId}/reject$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^${requestId}/review$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^${requestId}/activation-eligibility$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^${requestId}/activation-intents$`),
      { method: "POST", session: true, body: "none", csrf: true },
    ],
  ];
  return patterns.find(([pattern, route]) =>
    pattern.test(suffix) && route.method === method
  )?.[1] ?? null;
}

function platformQuery(request: NextRequest, callback: boolean): string | null {
  const callbackSearch = request.headers.get(platformCallbackQueryHeader);
  const params = callback
    ? new URLSearchParams(callbackSearch?.startsWith("?") ? callbackSearch.slice(1) : "")
    : request.nextUrl.searchParams;
  if (!callback) {
    const allowedKeys = [
      "state",
      "limit",
      "offset",
      "search",
      "status",
      "max_depth",
      "relationship_type",
      "include_revoked",
      "community_id",
      "page",
      "page_size",
      "state_id",
      "district_id",
      "mandal_id",
      "pincode",
    ];
    if ([...params.keys()].some((key) => !allowedKeys.includes(key))) {
      return null;
    }
    if (
      params.getAll("state").length > 1 ||
      params.getAll("limit").length > 1 ||
      params.getAll("offset").length > 1 ||
      params.getAll("search").length > 1 ||
      params.getAll("status").length > 1 ||
      params.getAll("max_depth").length > 1 ||
      params.getAll("relationship_type").length > 1 ||
      params.getAll("include_revoked").length > 1 ||
      params.getAll("community_id").length > 1 ||
      params.getAll("page").length > 1 ||
      params.getAll("page_size").length > 1 ||
      params.getAll("state_id").length > 1 ||
      params.getAll("district_id").length > 1 ||
      params.getAll("mandal_id").length > 1 ||
      params.getAll("pincode").length > 1
    ) {
      return null;
    }
    const state = params.get("state");
    const limit = params.get("limit");
    const offset = params.get("offset");
    const search = params.get("search");
    const status = params.get("status");
    const maxDepth = params.get("max_depth");
    const relationshipType = params.get("relationship_type");
    const includeRevoked = params.get("include_revoked");
    const communityId = params.get("community_id");
    const page = params.get("page");
    const pageSize = params.get("page_size");
    const stateId = params.get("state_id");
    const districtId = params.get("district_id");
    const mandalId = params.get("mandal_id");
    const pincode = params.get("pincode");

    if (
      (state !== null &&
        ![
          "PENDING_DOMAIN_PROOF",
          "PENDING_ACTIVATION_REVIEW",
          "REJECTED",
          "EXPIRED",
          "CANCELLED",
          "ACTIVE",
          "Andhra Pradesh",
          "Telangana",
        ].includes(state)) ||
      (status !== null && !["active", "inactive", "suspended", "revoked", "PENDING", "APPROVED", "REJECTED", "PENDING_MANAGER", "PENDING_SUPER_ADMIN", "FULFILLED"].includes(status)) ||
      (search !== null && (search.length > 100 || !/^[A-Za-z0-9 ._-]*$/.test(search))) ||
      (limit !== null && !/^(?:[1-9]|[1-9][0-9]|100)$/.test(limit)) ||
      (offset !== null && !/^(?:0|[1-9][0-9]{0,3}|10000)$/.test(offset)) ||
      (page !== null && !/^[1-9][0-9]{0,4}$/.test(page)) ||
      (pageSize !== null && !/^(?:[1-9]|[1-9][0-9]|100)$/.test(pageSize)) ||
      (stateId !== null && !new RegExp(`^${uuidPattern}$`).test(stateId)) ||
      (districtId !== null && !new RegExp(`^${uuidPattern}$`).test(districtId)) ||
      (mandalId !== null && !new RegExp(`^${uuidPattern}$`).test(mandalId)) ||
      (pincode !== null && !/^[1-9][0-9]{5}$/.test(pincode)) ||
      (maxDepth !== null && !/^(?:[1-9]|10)$/.test(maxDepth)) ||
      (relationshipType !== null && !["REPORTS_TO", "REFERRED_BY", "MANAGES"].includes(relationshipType)) ||
      (includeRevoked !== null && !["true", "false"].includes(includeRevoked)) ||
      (communityId !== null && !new RegExp(`^${uuidPattern}$`).test(communityId))
    ) {
      return null;
    }
    return params.toString();
  }
  if ([...params.keys()].some((key) => !["state", "code", "error"].includes(key))) {
    return null;
  }
  if (
    params.getAll("state").length > 1 ||
    params.getAll("code").length > 1 ||
    params.getAll("error").length > 1 ||
    (params.get("state")?.length ?? 0) > 256 ||
    (params.get("code")?.length ?? 0) > 2048 ||
    (params.get("error")?.length ?? 0) > 128
  ) {
    return null;
  }
  return params.toString();
}

function sameOriginMutation(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  if (!origin || !host) return false;
  try {
    const parsed = new URL(origin);
    return (
      parsed.origin === origin &&
      parsed.protocol === request.nextUrl.protocol &&
      parsed.host.toLowerCase() === host.toLowerCase()
    );
  } catch {
    return false;
  }
}

function cookieHeaders(upstream: Response): string[] {
  const headers = upstream.headers as Headers & { getSetCookie?: () => string[] };
  return headers.getSetCookie?.() ?? (upstream.headers.get("set-cookie") ? [upstream.headers.get("set-cookie")!] : []);
}

function validIdentityProviderLocation(value: string): boolean {
  try {
    const location = new URL(value);
    return (
      ["https:", "http:"].includes(location.protocol) &&
      !location.username &&
      !location.password &&
      !location.hash &&
      (location.protocol === "https:" ||
        (process.env.NODE_ENV === "development" &&
          isLoopbackHostname(location.hostname)))
    );
  } catch {
    return false;
  }
}

async function proxyPlatform(
  request: NextRequest,
  route: PlatformRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }
  const query = platformQuery(request, Boolean(route.callback));
  if (query === null || (route.start && query)) {
    return noStoreJson({ error: "INVALID_QUERY" }, 400);
  }
  if (
    route.csrf &&
    (!sameOriginMutation(request) || !request.headers.get("x-csrf-token"))
  ) {
    return noStoreJson({ error: "CSRF_VALIDATION_FAILED" }, 403);
  }
  if (
    Number(request.headers.get("content-length") ?? "0") > 16_384
  ) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !==
      "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }
  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, 16_384);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }
  if (route.body === "none" && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }

  const headers = new Headers({ Accept: "application/json" });
  if (route.csrf) {
    headers.set("Origin", request.headers.get("origin") ?? "");
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");
  }
  if (route.session) {
    if (routePath.startsWith("api/v1/platform/")) {
      const token = request.cookies.get(platformSessionCookieName)?.value;
      if (token && /^[A-Za-z0-9_-]{1,128}$/.test(token)) {
        headers.set("Cookie", `${platformSessionCookieName}=${token}`);
      }
    } else {
      const authToken = request.cookies.get(authCookieName)?.value;
      if (authToken && /^[A-Za-z0-9_-]{1,128}$/.test(authToken)) {
        headers.set("Cookie", `${authCookieName}=${authToken}`);
      }
    }
  }
  if (route.callback) {
    const binding = request.cookies.get(platformBindingCookieName)?.value;
    if (binding && /^[A-Za-z0-9_-]{1,128}$/.test(binding)) {
      headers.set("Cookie", `${platformBindingCookieName}=${binding}`);
    }
  }
  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }
  const idempotencyKey = request.headers.get("idempotency-key");
  if (idempotencyKey !== null) {
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(idempotencyKey)) {
      return noStoreJson({ error: "INVALID_IDEMPOTENCY_KEY" }, 400);
    }
    headers.set("Idempotency-Key", idempotencyKey);
  }
  if (
    routePath.endsWith("/domains/challenge") ||
    routePath.endsWith("/reject") ||
    routePath.endsWith("/review") ||
    routePath.endsWith("/activation-intents") ||
    routePath === "api/v1/platform/onboarding-requests"
  ) {
    if (!idempotencyKey) return noStoreJson({ error: "IDEMPOTENCY_KEY_REQUIRED" }, 400);
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), upstreamTimeoutMilliseconds);
  try {
    const target = new URL(`/${routePath}${query ? `?${query}` : ""}`, origin);
    const upstream = await fetch(target, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);
    if (route.start || route.callback) {
      responseHeaders.set("Referrer-Policy", "no-referrer");
    }
    if (route.start) {
      const location = upstream.headers.get("location");
      const setCookies = cookieHeaders(upstream);
      const binding = setCookies.find((value) =>
        value.startsWith(`${platformBindingCookieName}=`)
      );
      if (
        upstream.status !== 302 ||
        !location ||
        !validIdentityProviderLocation(location) ||
        !binding ||
        !hasAllowedCookie(
          binding,
          platformBindingCookieName,
          platformBindingCookiePath,
          false,
          request.nextUrl.protocol === "https:"
        )
      ) {
      return unavailableResponse();
      }
      responseHeaders.set("Location", location);
      responseHeaders.append("Set-Cookie", binding);
    } else if (route.callback) {
      const setCookies = cookieHeaders(upstream);
      const session = setCookies.find((value) =>
        value.startsWith(`${platformSessionCookieName}=`)
      );
      const binding = setCookies.find((value) =>
        value.startsWith(`${platformBindingCookieName}=`)
      );
      if (
        upstream.status === 303 &&
        upstream.headers.get("location") === "/" &&
        session &&
        binding &&
        hasAllowedCookie(
          session,
          platformSessionCookieName,
          platformSessionCookiePath,
          false,
          request.nextUrl.protocol === "https:"
        ) &&
        hasAllowedCookie(
          binding,
          platformBindingCookieName,
          platformBindingCookiePath,
          true,
          request.nextUrl.protocol === "https:"
        )
      ) {
        responseHeaders.set("Location", "/superadmin/communities");
        responseHeaders.append("Set-Cookie", session);
        responseHeaders.append("Set-Cookie", binding);
      } else {
        if (upstream.status < 400 || upstream.status > 599) {
        return unavailableResponse();
        }
        responseHeaders.append(
          "Set-Cookie",
          expiredCookie(request, platformBindingCookieName, platformBindingCookiePath)
        );
      }
    } else if (
      route.session &&
      upstream.status === 401
    ) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, platformSessionCookieName, platformSessionCookiePath)
      );
    } else if (routePath.endsWith("/logout") && upstream.status === 204) {
      const deletion = cookieHeaders(upstream).find((value) =>
        value.startsWith(`${platformSessionCookieName}=`)
      );
      if (
        !deletion ||
        !hasAllowedCookie(
          deletion,
          platformSessionCookieName,
          platformSessionCookiePath,
          true,
          request.nextUrl.protocol === "https:"
        )
      ) {
      return unavailableResponse();
      }
      responseHeaders.append("Set-Cookie", deletion);
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody =
      (route.start && upstream.status === 302) ||
      (route.callback && upstream.status === 303)
        ? null
        : await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
  return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

type AgentRoute = {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  session: boolean;
  body: "none" | "json";
  csrf: boolean;
};

function isAgentPath(routePath: string): boolean {
  const id = uuidPattern;
  return (
    routePath === "api/v1/agents" ||
    routePath === "api/v1/agents/me" ||
    routePath === "api/v1/agents/regions" ||
    routePath === "api/v1/community/regions" ||
    new RegExp(`^api/v1/agents/${id}$`).test(routePath) ||
    new RegExp(`^api/v1/agents/${id}/lifecycle-requests$`).test(routePath) ||
    routePath === "api/v1/agent/lifecycle-requests" ||
    routePath === "api/v1/agent/policy" ||
    routePath === "api/v1/agent/users" ||
    routePath === "api/v1/agent/subscribers" ||
    routePath === "api/v1/agent/payments/confirm" ||
    routePath === "api/v1/agent/collections/summary" ||
    routePath === "api/v1/agent/descendants" ||
    routePath === "api/v1/agent/descendants/stats" ||
    routePath === "api/v1/members/relationships" ||
    new RegExp(`^api/v1/members/${id}/relationships$`).test(routePath) ||
    new RegExp(`^api/v1/members/relationships/${id}$`).test(routePath) ||
    routePath === "api/v1/agent-appointments" ||
    routePath === "api/v1/agents/appointments" ||
    new RegExp(`^api/v1/(agent-appointments|agents/appointments)/${id}$`).test(routePath) ||
    new RegExp(`^api/v1/(agent-appointments|agents/appointments)/${id}/(approve|reject|cancel)$`).test(routePath) ||
    routePath.startsWith("api/v1/locations") ||
    routePath === "api/v1/agent/creation-requests" ||
    routePath === "api/v1/agent/agents-from-request" ||
    routePath === "api/v1/manager/creation-requests" ||
    new RegExp(`^api/v1/manager/creation-requests/${id}/review$`).test(routePath)
  );
}

function agentRoute(routePath: string, method: string): AgentRoute | null {
  const id = uuidPattern;
  const routes: Array<[RegExp, AgentRoute]> = [
    [
      /^api\/v1\/agents$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agents\/me$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agents\/regions$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/community\/regions$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agents$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/agents/${id}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/agents/${id}$`),
      { method: "PATCH", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/agents/${id}/lifecycle-requests$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/agent\/lifecycle-requests$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agent\/policy$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agent\/users$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agent\/users$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/agent\/subscribers$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agent\/payments\/confirm$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/agent\/collections\/summary$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agent\/descendants$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agent\/descendants$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/agent\/descendants\/stats$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/members\/relationships$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/members/${id}/relationships$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/members/relationships/${id}$`),
      { method: "DELETE", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/(agent-appointments|agents\/appointments)$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/(agent-appointments|agents\/appointments)$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/(agent-appointments|agents/appointments)/${id}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/(agent-appointments|agents/appointments)/${id}/approve$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/(agent-appointments|agents/appointments)/${id}/reject$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/(agent-appointments|agents/appointments)/${id}/cancel$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/locations\/states$/,
      { method: "GET", session: false, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/locations\/districts$/,
      { method: "GET", session: false, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/locations\/mandals$/,
      { method: "GET", session: false, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/locations\/manager-scope$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/locations/communities/${id}/managers/${id}/scope$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/agent\/creation-requests$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/agent\/creation-requests$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/agent\/agents-from-request$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/manager\/creation-requests$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/manager/creation-requests/${id}/review$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
  ];

  return (
    routes.find(([pattern, r]) => pattern.test(routePath) && r.method === method)?.[1] ??
    null
  );
}

async function proxyAgent(
  request: NextRequest,
  route: AgentRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }
  const query = platformQuery(request, false);
  if (query === null) {
    return noStoreJson({ error: "INVALID_QUERY" }, 400);
  }
  if (
    route.csrf &&
    (!sameOriginMutation(request) || !request.headers.get("x-csrf-token"))
  ) {
    return noStoreJson({ error: "CSRF_VALIDATION_FAILED" }, 403);
  }
  if (Number(request.headers.get("content-length") ?? "0") > 32_768) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, 32_768);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }
  if (route.body === "none" && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }

  const headers = new Headers({
    Accept: "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });
  if (route.csrf) {
    headers.set("Origin", request.headers.get("origin") ?? "");
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");
  }
  if (route.session) {
    const sessionCookie = request.cookies.get(authCookieName)?.value;
    const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
    const cookieParts: string[] = [];
    if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
      cookieParts.push(`${authCookieName}=${sessionCookie}`);
    }
    if (platformCookie && /^[A-Za-z0-9_-]{1,128}$/.test(platformCookie)) {
      cookieParts.push(`${platformSessionCookieName}=${platformCookie}`);
    }
    if (cookieParts.length > 0) {
      headers.set("Cookie", cookieParts.join("; "));
    }
  }
  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }

  const targetUrl = new URL(`/${routePath}`, origin);
  if (query) targetUrl.search = query;

  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds * 2
  );
  try {
    const upstream = await fetch(targetUrl, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);
    if (upstream.status === 401) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, authCookieName, authCookiePath)
      );
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
  return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

type SupportRoute = {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  session: boolean;
  body: "none" | "json";
  csrf: boolean;
};

function isSupportPath(routePath: string): boolean {
  const id = uuidPattern;
  return (
    routePath === "api/v1/support-tickets" ||
    new RegExp(`^api/v1/support-tickets/${id}$`).test(routePath) ||
    new RegExp(`^api/v1/support-tickets/${id}/resolve$`).test(routePath)
  );
}

function supportRoute(routePath: string, method: string): SupportRoute | null {
  const id = uuidPattern;
  if (routePath === "api/v1/support-tickets") {
    if (method === "GET") return { method: "GET", session: true, body: "none", csrf: false };
    if (method === "POST") return { method: "POST", session: true, body: "json", csrf: true };
    return null;
  }
  if (
    new RegExp(`^api/v1/support-tickets/${id}$`).test(routePath) ||
    new RegExp(`^api/v1/support-tickets/${id}/resolve$`).test(routePath)
  ) {
    if (method === "PATCH") return { method: "PATCH", session: true, body: "none", csrf: true };
    return null;
  }
  return null;
}

function supportQuery(request: NextRequest): string | null {
  const params = request.nextUrl.searchParams;
  const allowed = ["limit", "offset", "community_id"];
  if ([...params.keys()].some((key) => !allowed.includes(key))) {
    return null;
  }
  const communityId = params.get("community_id");
  if (communityId !== null && !new RegExp(`^${uuidPattern}$`).test(communityId)) {
    return null;
  }
  const limit = params.get("limit");
  const offset = params.get("offset");
  if (limit !== null && !/^(?:[1-9]|[1-9][0-9]|100)$/.test(limit)) {
    return null;
  }
  if (offset !== null && !/^(?:0|[1-9][0-9]{0,3}|10000)$/.test(offset)) {
    return null;
  }
  return params.toString();
}

async function proxySupport(
  request: NextRequest,
  route: SupportRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }
  const query = supportQuery(request);
  if (query === null) {
    return noStoreJson({ error: "INVALID_QUERY" }, 400);
  }
  if (
    route.csrf &&
    (!sameOriginMutation(request) || !request.headers.get("x-csrf-token"))
  ) {
    return noStoreJson({ error: "CSRF_VALIDATION_FAILED" }, 403);
  }
  if (Number(request.headers.get("content-length") ?? "0") > 32_768) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, 32_768);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }
  if (route.body === "none" && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }

  const headers = new Headers({
    Accept: "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });
  if (route.csrf) {
    headers.set("Origin", request.headers.get("origin") ?? "");
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");
  }
  if (route.session) {
    const sessionCookie = request.cookies.get(authCookieName)?.value;
    const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
    const cookieParts: string[] = [];
    if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
      cookieParts.push(`${authCookieName}=${sessionCookie}`);
    }
    if (platformCookie && /^[A-Za-z0-9_-]{1,128}$/.test(platformCookie)) {
      cookieParts.push(`${platformSessionCookieName}=${platformCookie}`);
    }
    if (cookieParts.length > 0) {
      headers.set("Cookie", cookieParts.join("; "));
    }
  }
  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }

  const targetUrl = new URL(`/${routePath}`, origin);
  if (query) targetUrl.search = query;

  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds * 2
  );
  try {
    const upstream = await fetch(targetUrl, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);

    if (upstream.status === 401) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, authCookieName, authCookiePath)
      );
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
  return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

type StorageRoute = {
  method: "GET" | "POST" | "DELETE";
  type: "upload" | "preview" | "presigned" | "delete";
};

function isStoragePath(routePath: string): boolean {
  if (routePath === "api/v1/storage/upload") return true;
  if (!routePath.startsWith("api/v1/storage/")) return false;
  const keyPart = routePath.slice("api/v1/storage/".length);
  return Boolean(
    keyPart &&
      !keyPart.includes("..") &&
      /^[a-zA-Z0-9_\-\./]+$/.test(keyPart)
  );
}

function storageRoute(routePath: string, method: string): StorageRoute | null {
  if (routePath === "api/v1/storage/upload") {
    if (method === "POST") return { method: "POST", type: "upload" };
    return null;
  }
  if (routePath.startsWith("api/v1/storage/")) {
    const keyPart = routePath.slice("api/v1/storage/".length);
    if (!keyPart || keyPart.includes("..") || !/^[a-zA-Z0-9_\-\./]+$/.test(keyPart)) {
      return null;
    }
    if (method === "GET") {
      if (routePath.endsWith("/preview")) {
        return { method: "GET", type: "preview" };
      }
      return { method: "GET", type: "presigned" };
    }
    if (method === "DELETE") {
      return { method: "DELETE", type: "delete" };
    }
    return null;
  }
  return null;
}

async function proxyStorage(
  request: NextRequest,
  route: StorageRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }
  let query = "";
  if (request.nextUrl.search) {
    if (route.type === "preview") {
      const token = request.nextUrl.searchParams.get("verification_token");
      if (!token || !/^[A-Za-z0-9_-]{1,128}$/.test(token) || request.nextUrl.searchParams.size > 1) {
        return noStoreJson({ error: "QUERY_NOT_SUPPORTED" }, 400);
      }
      query = `?verification_token=${encodeURIComponent(token)}`;
    } else if (route.type === "presigned") {
      const expires = request.nextUrl.searchParams.get("expires_in_seconds");
      if (!expires || !/^\d+$/.test(expires) || request.nextUrl.searchParams.size > 1) {
        return noStoreJson({ error: "QUERY_NOT_SUPPORTED" }, 400);
      }
      query = `?expires_in_seconds=${encodeURIComponent(expires)}`;
    } else {
      return noStoreJson({ error: "QUERY_NOT_SUPPORTED" }, 400);
    }
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const headers = new Headers({
    "X-Forwarded-Host": request.headers.get("host") ?? "",
    Accept: route.type === "preview" ? "*/*" : "application/json",
  });

  const sessionCookie = request.cookies.get(authCookieName)?.value;
  const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
  const cookieParts: string[] = [];
  if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
    cookieParts.push(`${authCookieName}=${sessionCookie}`);
  }
  if (platformCookie && /^[A-Za-z0-9_-]{1,128}$/.test(platformCookie)) {
    cookieParts.push(`${platformSessionCookieName}=${platformCookie}`);
  }
  if (cookieParts.length > 0) {
    headers.set("Cookie", cookieParts.join("; "));
  }

  const regVerificationToken = request.headers.get("x-registration-verification-token");
  if (regVerificationToken && /^[A-Za-z0-9_-]{1,128}$/.test(regVerificationToken)) {
    headers.set("X-Registration-Verification-Token", regVerificationToken);
  }

  const authHeader = request.headers.get("authorization");
  if (authHeader && /^Bearer\s+[A-Za-z0-9_-]{1,128}$/.test(authHeader)) {
    headers.set("Authorization", authHeader);
  }

  const idempotencyKey = request.headers.get("idempotency-key");
  if (idempotencyKey && /^[A-Za-z0-9_-]{1,200}$/.test(idempotencyKey)) {
    headers.set("Idempotency-Key", idempotencyKey);
  }

  let body: ArrayBuffer | undefined;
  if (route.type === "delete") {
    if (!sameOriginMutation(request) || !request.headers.get("x-csrf-token")) {
      return noStoreJson({ error: "CSRF_VALIDATION_FAILED" }, 403);
    }
    headers.set("Origin", request.headers.get("origin") ?? "");
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");
  }

  if (route.type === "upload") {
    if (!sameOriginMutation(request) || !request.headers.get("x-csrf-token")) {
      return noStoreJson({ error: "CSRF_VALIDATION_FAILED" }, 403);
    }
    headers.set("Origin", request.headers.get("origin") ?? "");
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");

    const contentType = request.headers.get("content-type") ?? "";
    if (
      !contentType.includes("multipart/form-data") &&
      !contentType.includes("application/json")
    ) {
      return noStoreJson({ error: "UNSUPPORTED_MEDIA_TYPE" }, 415);
    }

    const contentLength = Number(request.headers.get("content-length") ?? "0");
    if (contentLength > 10 * 1024 * 1024) {
      return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
    }

    headers.set("Content-Type", contentType);
    const rawBuffer = await request.arrayBuffer();
    if (rawBuffer.byteLength > 10 * 1024 * 1024) {
      return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
    }
    body = rawBuffer;
  }

  const targetUrl = new URL(`/${routePath}`, origin);
  if (query) {
    targetUrl.search = query;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10_000);
  try {
    const upstream = await fetch(targetUrl, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({
      "Cache-Control": route.type === "preview" ? "private, no-store, max-age=0" : "no-store",
    });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);
    if (upstream.status === 401 && sessionCookie) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, authCookieName, authCookiePath)
      );
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
  return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}


type CommissionRoute = {
  method: "GET" | "POST";
  session: boolean;
  body: "none" | "json";
  csrf: boolean;
};

function isCommissionPath(routePath: string): boolean {
  const id = uuidPattern;
  return (
    routePath === "api/v1/commissions/accounts/me" ||
    new RegExp(`^api/v1/commissions/accounts/${id}$`).test(routePath) ||
    new RegExp(`^api/v1/commissions/accounts/${id}/entries$`).test(routePath) ||
    routePath === "api/v1/commissions/journals" ||
    new RegExp(`^api/v1/commissions/journals/${id}$`).test(routePath) ||
    new RegExp(`^api/v1/commissions/journals/${id}/reversal$`).test(routePath)
  );
}

function commissionRoute(routePath: string, method: string): CommissionRoute | null {
  const id = uuidPattern;
  const routes: Array<[RegExp, CommissionRoute]> = [
    [
      /^api\/v1\/commissions\/accounts\/me$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/commissions/accounts/${id}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/commissions/accounts/${id}/entries$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/commissions\/journals$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/commissions/journals/${id}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/commissions/journals/${id}/reversal$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
  ];

  return (
    routes.find(([pattern, r]) => pattern.test(routePath) && r.method === method)?.[1] ??
    null
  );
}

function commissionQuery(request: NextRequest): string | null {
  const params = request.nextUrl.searchParams;
  const allowed = ["limit", "offset"];
  if ([...params.keys()].some((key) => !allowed.includes(key))) {
    return null;
  }
  const limit = params.get("limit");
  const offset = params.get("offset");
  if (limit !== null && !/^(?:[1-9]|[1-9][0-9]|100)$/.test(limit)) {
    return null;
  }
  if (offset !== null && !/^(?:0|[1-9][0-9]{0,3}|10000)$/.test(offset)) {
    return null;
  }
  return params.toString();
}

async function proxyCommission(
  request: NextRequest,
  route: CommissionRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }
  const query = commissionQuery(request);
  if (query === null) {
    return noStoreJson({ error: "INVALID_QUERY" }, 400);
  }
  if (
    route.csrf &&
    (!sameOriginMutation(request) || !request.headers.get("x-csrf-token"))
  ) {
    return noStoreJson({ error: "CSRF_VALIDATION_FAILED" }, 403);
  }
  if (Number(request.headers.get("content-length") ?? "0") > 32_768) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, 32_768);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }
  if (route.body === "none" && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }

  const headers = new Headers({
    Accept: "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });
  if (route.csrf) {
    headers.set("Origin", request.headers.get("origin") ?? "");
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");
  }
  if (route.session) {
    const sessionCookie = request.cookies.get(authCookieName)?.value;
    const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
    const cookieParts: string[] = [];
    if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
      cookieParts.push(`${authCookieName}=${sessionCookie}`);
    }
    if (platformCookie && /^[A-Za-z0-9_-]{1,128}$/.test(platformCookie)) {
      cookieParts.push(`${platformSessionCookieName}=${platformCookie}`);
    }
    if (cookieParts.length > 0) {
      headers.set("Cookie", cookieParts.join("; "));
    }
  }
  const idempotencyKey = request.headers.get("idempotency-key");
  if (idempotencyKey !== null) {
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(idempotencyKey)) {
      return noStoreJson({ error: "INVALID_IDEMPOTENCY_KEY" }, 400);
    }
    headers.set("Idempotency-Key", idempotencyKey);
  }
  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }

  const targetUrl = new URL(`/${routePath}`, origin);
  if (query) targetUrl.search = query;

  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds * 2
  );
  try {
    const upstream = await fetch(targetUrl, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);

    if (upstream.status === 401) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, authCookieName, authCookiePath)
      );
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
  return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

type SubscriptionRoute = {
  method: "GET" | "POST";
  session: boolean;
  body: "none" | "json";
  csrf: boolean;
};

function isSubscriptionPath(routePath: string): boolean {
  const id = uuidPattern;
  return (
    routePath === "api/v1/subscription-plans" ||
    new RegExp(`^api/v1/subscription-plans/${id}$`).test(routePath) ||
    routePath === "api/v1/subscriptions" ||
    new RegExp(`^api/v1/subscriptions/${id}$`).test(routePath) ||
    new RegExp(`^api/v1/subscriptions/${id}/cancel$`).test(routePath) ||
    new RegExp(`^api/v1/subscriptions/${id}/activate$`).test(routePath)
  );
}

function subscriptionRoute(routePath: string, method: string): SubscriptionRoute | null {
  const id = uuidPattern;
  const routes: Array<[RegExp, SubscriptionRoute]> = [
    [
      /^api\/v1\/subscription-plans$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/subscription-plans/${id}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/subscriptions$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/subscriptions$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/subscriptions/${id}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/subscriptions/${id}/cancel$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/subscriptions/${id}/activate$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
  ];

  return (
    routes.find(([pattern, r]) => pattern.test(routePath) && r.method === method)?.[1] ??
    null
  );
}

function subscriptionQuery(request: NextRequest): string | null {
  const params = request.nextUrl.searchParams;
  const allowed = ["limit", "offset", "subscriber_membership_id", "status"];
  if ([...params.keys()].some((key) => !allowed.includes(key))) {
    return null;
  }
  const limit = params.get("limit");
  const offset = params.get("offset");
  const subscriberId = params.get("subscriber_membership_id");
  const status = params.get("status");

  if (limit !== null && !/^(?:[1-9]|[1-9][0-9]|100)$/.test(limit)) {
    return null;
  }
  if (offset !== null && !/^(?:0|[1-9][0-9]{0,3}|10000)$/.test(offset)) {
    return null;
  }
  if (subscriberId !== null && !new RegExp(`^${uuidPattern}$`).test(subscriberId)) {
    return null;
  }
  if (
    status !== null &&
    !["PENDING_PAYMENT", "ACTIVE", "EXPIRED", "CANCELLED"].includes(status)
  ) {
    return null;
  }
  return params.toString();
}

async function proxySubscription(
  request: NextRequest,
  route: SubscriptionRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }
  const query = subscriptionQuery(request);
  if (query === null) {
    return noStoreJson({ error: "INVALID_QUERY" }, 400);
  }
  if (
    route.csrf &&
    (!sameOriginMutation(request) || !request.headers.get("x-csrf-token"))
  ) {
    return noStoreJson({ error: "CSRF_VALIDATION_FAILED" }, 403);
  }
  if (Number(request.headers.get("content-length") ?? "0") > 32_768) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, 32_768);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }
  if (route.body === "none" && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }

  const headers = new Headers({
    Accept: "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });
  if (route.csrf) {
    headers.set("Origin", request.headers.get("origin") ?? "");
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");
  }
  if (route.session) {
    const sessionCookie = request.cookies.get(authCookieName)?.value;
    const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
    const cookieParts: string[] = [];
    if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
      cookieParts.push(`${authCookieName}=${sessionCookie}`);
    }
    if (platformCookie && /^[A-Za-z0-9_-]{1,128}$/.test(platformCookie)) {
      cookieParts.push(`${platformSessionCookieName}=${platformCookie}`);
    }
    if (cookieParts.length > 0) {
      headers.set("Cookie", cookieParts.join("; "));
    }
  }

  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), upstreamTimeoutMilliseconds);
  try {
    const target = new URL(`/${routePath}${query ? `?${query}` : ""}`, origin);
    const upstream = await fetch(target, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);

    if (upstream.status === 401) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, authCookieName, authCookiePath)
      );
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
  return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

type PaymentRoute = {
  method: "POST";
  session: boolean;
  body: "json";
  csrf: boolean;
  webhook?: boolean;
};

function isPaymentPath(routePath: string): boolean {
  return (
    routePath === "api/v1/payments/orders" ||
    routePath === "api/v1/payments/verify" ||
    routePath === "api/v1/payments/webhook" ||
    routePath === "api/v1/webhooks/payments/status"
  );
}

function paymentRoute(routePath: string, method: string): PaymentRoute | null {
  const routes: Array<[string, PaymentRoute]> = [
    [
      "api/v1/payments/orders",
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      "api/v1/payments/verify",
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      "api/v1/payments/webhook",
      { method: "POST", session: false, body: "json", csrf: false, webhook: true },
    ],
    [
      "api/v1/webhooks/payments/status",
      { method: "POST", session: false, body: "json", csrf: false, webhook: true },
    ],
  ];

  return routes.find(([path, r]) => path === routePath && r.method === method)?.[1] ?? null;
}

async function proxyPayment(
  request: NextRequest,
  route: PaymentRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }
  if (
    route.csrf &&
    (!sameOriginMutation(request) || !request.headers.get("x-csrf-token"))
  ) {
    return noStoreJson({ error: "CSRF_VALIDATION_FAILED" }, 403);
  }
  const maxBytes = route.webhook ? 65_536 : 32_768;
  if (Number(request.headers.get("content-length") ?? "0") > maxBytes) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, maxBytes);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }

  const headers = new Headers({
    Accept: "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });
  if (route.csrf) {
    headers.set("Origin", request.headers.get("origin") ?? "");
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");
  }
  if (route.session) {
    const sessionCookie = request.cookies.get(authCookieName)?.value;
    if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
      headers.set("Cookie", `${authCookieName}=${sessionCookie}`);
    }
  }
  if (route.webhook) {
    const sig = request.headers.get("x-razorpay-signature");
    if (sig) {
      headers.set("X-Razorpay-Signature", sig);
    }
    const paySig = request.headers.get("x-payment-signature") || request.headers.get("x-signature");
    if (paySig) {
      headers.set("X-Payment-Signature", paySig);
    }
    const payTs = request.headers.get("x-payment-timestamp") || request.headers.get("x-timestamp");
    if (payTs) {
      headers.set("X-Payment-Timestamp", payTs);
    }
    const evtId = request.headers.get("x-event-id");
    if (evtId) {
      headers.set("X-Event-ID", evtId);
    }
  }
  const idempotencyKey = request.headers.get("idempotency-key");
  if (idempotencyKey !== null) {
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(idempotencyKey)) {
      return noStoreJson({ error: "INVALID_IDEMPOTENCY_KEY" }, 400);
    }
    headers.set("Idempotency-Key", idempotencyKey);
  }
  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }

  const targetUrl = new URL(`/${routePath}`, origin);
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds * 2
  );
  try {
    const upstream = await fetch(targetUrl, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);

    if (upstream.status === 401) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, authCookieName, authCookiePath)
      );
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

type PayoutRoute = {
  method: "GET" | "POST";
  session: boolean;
  platform?: boolean;
  body: "none" | "json";
  csrf: boolean;
};

function isPayoutPath(routePath: string): boolean {
  const id = uuidPattern;
  return (
    routePath === "api/v1/payouts/eligible-balance" ||
    routePath === "api/v1/payouts/requests" ||
    new RegExp(`^api/v1/payouts/requests/${id}$`).test(routePath) ||
    new RegExp(`^api/v1/payouts/requests/${id}/review$`).test(routePath) ||
    new RegExp(`^api/v1/payouts/requests/${id}/cancel$`).test(routePath) ||
    routePath === "api/v1/platform/payouts/requests" ||
    new RegExp(`^api/v1/platform/payouts/requests/${id}$`).test(routePath) ||
    new RegExp(`^api/v1/platform/payouts/requests/${id}/disburse$`).test(routePath) ||
    new RegExp(`^api/v1/platform/payouts/requests/${id}/fail$`).test(routePath)
  );
}

function payoutRoute(routePath: string, method: string): PayoutRoute | null {
  const id = uuidPattern;
  const routes: Array<[RegExp, PayoutRoute]> = [
    [
      /^api\/v1\/payouts\/eligible-balance$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/payouts\/requests$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/payouts\/requests$/,
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/payouts/requests/${id}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/payouts/requests/${id}/review$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/payouts/requests/${id}/cancel$`),
      { method: "POST", session: true, body: "json", csrf: true },
    ],
    [
      /^api\/v1\/platform\/payouts\/requests$/,
      { method: "GET", session: true, platform: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/platform/payouts/requests/${id}$`),
      { method: "GET", session: true, platform: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/platform/payouts/requests/${id}/disburse$`),
      { method: "POST", session: true, platform: true, body: "json", csrf: true },
    ],
    [
      new RegExp(`^api/v1/platform/payouts/requests/${id}/fail$`),
      { method: "POST", session: true, platform: true, body: "json", csrf: true },
    ],
  ];

  return routes.find(([pattern, r]) => pattern.test(routePath) && r.method === method)?.[1] ?? null;
}

function payoutQuery(request: NextRequest): string | null {
  const params = request.nextUrl.searchParams;
  const allowed = ["limit", "offset", "status", "agent_membership_id", "tenant_id"];
  if ([...params.keys()].some((key) => !allowed.includes(key))) {
    return null;
  }
  const limit = params.get("limit");
  const offset = params.get("offset");
  const agentId = params.get("agent_membership_id");
  const tenantId = params.get("tenant_id");
  const status = params.get("status");

  if (limit !== null && !/^(?:[1-9]|[1-9][0-9]|100)$/.test(limit)) {
    return null;
  }
  if (offset !== null && !/^(?:0|[1-9][0-9]{0,3}|10000)$/.test(offset)) {
    return null;
  }
  if (agentId !== null && !new RegExp(`^${uuidPattern}$`).test(agentId)) {
    return null;
  }
  if (tenantId !== null && !new RegExp(`^${uuidPattern}$`).test(tenantId)) {
    return null;
  }
  if (
    status !== null &&
    !["PENDING_REVIEW", "APPROVED", "REJECTED", "PROCESSING", "PAID", "FAILED", "CANCELLED"].includes(status)
  ) {
    return null;
  }
  return params.toString();
}

async function proxyPayout(
  request: NextRequest,
  route: PayoutRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }
  const query = payoutQuery(request);
  if (query === null) {
    return noStoreJson({ error: "INVALID_QUERY" }, 400);
  }
  if (
    route.csrf &&
    (!sameOriginMutation(request) || !request.headers.get("x-csrf-token"))
  ) {
    return noStoreJson({ error: "CSRF_VALIDATION_FAILED" }, 403);
  }
  if (Number(request.headers.get("content-length") ?? "0") > 32_768) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, 32_768);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }
  if (route.body === "none" && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }

  const headers = new Headers({
    Accept: "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });
  if (route.csrf) {
    headers.set("Origin", request.headers.get("origin") ?? "");
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");
  }
  if (route.session) {
    if (route.platform) {
      const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
      if (platformCookie && /^[A-Za-z0-9_-]{1,128}$/.test(platformCookie)) {
        headers.set("Cookie", `${platformSessionCookieName}=${platformCookie}`);
      }
    } else {
      const sessionCookie = request.cookies.get(authCookieName)?.value;
      if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
        headers.set("Cookie", `${authCookieName}=${sessionCookie}`);
      }
    }
  }

  const idempotencyKey = request.headers.get("idempotency-key");
  if (idempotencyKey !== null) {
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(idempotencyKey)) {
      return noStoreJson({ error: "INVALID_IDEMPOTENCY_KEY" }, 400);
    }
    headers.set("Idempotency-Key", idempotencyKey);
  }
  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }

  const targetUrl = new URL(`/${routePath}`, origin);
  if (query) targetUrl.search = query;

  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds * 2
  );
  try {
    const upstream = await fetch(targetUrl, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);

    const activeCookieName = route.platform ? platformSessionCookieName : authCookieName;
    const activeCookiePath = route.platform ? platformSessionCookiePath : authCookiePath;
    if (upstream.status === 401) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, activeCookieName, activeCookiePath)
      );
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

type NotificationProxyRoute = {
  method: "GET" | "POST";
  session: boolean;
  body: "none" | "json";
  csrf: boolean;
  webhook?: boolean;
};

function isNotificationPath(routePath: string): boolean {
  return (
    routePath.startsWith("api/v1/notifications/") ||
    routePath === "api/v1/webhooks/notifications/status"
  );
}

function notificationRoute(
  routePath: string,
  method: string
): NotificationProxyRoute | null {
  const messageIdPattern = "[A-Za-z0-9_.-]{1,128}";
  const routes: Array<[RegExp, NotificationProxyRoute]> = [
    [
      new RegExp(`^api/v1/notifications/status/${messageIdPattern}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/webhooks\/notifications\/status$/,
      { method: "POST", session: false, body: "json", csrf: false, webhook: true },
    ],
  ];

  return routes.find(([pattern, r]) => pattern.test(routePath) && r.method === method)?.[1] ?? null;
}

async function proxyNotification(
  request: NextRequest,
  route: NotificationProxyRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }

  const maxBytes = route.webhook ? 65_536 : 32_768;
  if (Number(request.headers.get("content-length") ?? "0") > maxBytes) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, maxBytes);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }
  if (route.body === "none" && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }

  const headers = new Headers({
    Accept: "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });

  if (route.session) {
    const sessionCookie = request.cookies.get(authCookieName)?.value;
    const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
    const cookieParts: string[] = [];
    if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
      cookieParts.push(`${authCookieName}=${sessionCookie}`);
    }
    if (platformCookie && /^[A-Za-z0-9_-]{1,128}$/.test(platformCookie)) {
      cookieParts.push(`${platformSessionCookieName}=${platformCookie}`);
    }
    if (cookieParts.length > 0) {
      headers.set("Cookie", cookieParts.join("; "));
    }
  }

  if (route.webhook) {
    const ts = request.headers.get("x-notification-timestamp");
    const sig = request.headers.get("x-notification-signature");
    const evtId = request.headers.get("x-event-id");
    if (ts) headers.set("X-Notification-Timestamp", ts);
    if (sig) headers.set("X-Notification-Signature", sig);
    if (evtId) headers.set("X-Event-ID", evtId);
  }

  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }

  const targetUrl = new URL(`/${routePath}`, origin);
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds * 2
  );
  try {
    const upstream = await fetch(targetUrl, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);

    if (upstream.status === 401 && route.session) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, authCookieName, authCookiePath)
      );
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

async function readLimitedBody(
  request: NextRequest,
  maximumBytes: number
): Promise<{ body: string; tooLarge: boolean; invalidEncoding: boolean }> {
  if (!request.body) return { body: "", tooLarge: false, invalidEncoding: false };
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      totalBytes += value.byteLength;
      if (totalBytes > maximumBytes) {
        await reader.cancel();
        return { body: "", tooLarge: true, invalidEncoding: false };
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return {
      body: new TextDecoder("utf-8", { fatal: true }).decode(bytes),
      tooLarge: false,
      invalidEncoding: false,
    };
  } catch {
    return { body: "", tooLarge: false, invalidEncoding: true };
  }
}

function isAllowedAuthCookie(
  value: string,
  deleting: boolean,
  requireSecure: boolean
): boolean {
  return (
    hasAllowedCookie(value, authCookieName, authCookiePath, deleting, requireSecure) ||
    hasAllowedCookie(
      value,
      platformSessionCookieName,
      platformSessionCookiePath,
      deleting,
      requireSecure
    )
  );
}

function hasAllowedCookieAttributes(value: string, deleting: boolean): boolean {
  const parts = value.split(";").map((part) => part.trim());
  const first = parts.shift() ?? "";
  if (
    !first.startsWith(`${authCookieName}=`) ||
    (!deleting && first.length <= authCookieName.length + 1) ||
    (deleting && first !== `${authCookieName}=` && first !== `${authCookieName}=""`)
  ) {
    return false;
  }
  const attributes = new Map(
    parts.map((part) => {
      const separator = part.indexOf("=");
      return separator === -1
        ? [part.toLowerCase(), ""]
        : [part.slice(0, separator).toLowerCase(), part.slice(separator + 1)];
    })
  );
  return (
    attributes.get("path") === authCookiePath &&
    attributes.has("httponly") &&
    ["lax", "strict"].includes((attributes.get("samesite") ?? "").toLowerCase()) &&
    !attributes.has("domain") &&
    (!deleting || attributes.get("max-age") === "0")
  );
}

async function proxyLiveness(request: NextRequest): Promise<Response> {
  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  if (
    Number(request.headers.get("content-length") ?? "0") > 0 ||
    request.headers.has("transfer-encoding") ||
    request.body !== null
  ) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }
  if (request.nextUrl.search) {
    return noStoreJson({ error: "QUERY_NOT_SUPPORTED" }, 400);
  }

  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds
  );
  try {
    const upstream = await fetch(new URL("/api/v1/health", origin), {
      method: "GET",
      headers: request.headers.has("accept")
        ? { Accept: request.headers.get("accept") ?? "" }
        : {},
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);
    const body =
      upstream.status === 204 ||
      upstream.status === 205 ||
      upstream.status === 304
        ? null
        : await upstream.arrayBuffer();
    return new Response(body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
  return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

async function proxyCommunityAuth(
  request: NextRequest,
  route: { method: string; cookie: boolean },
  routePath: string
): Promise<Response> {
  const requiresOrigin =
    route.method === "POST" || request.headers.has("origin");
  if (!isLocalDevelopmentRequest(request, requiresOrigin)) {
  return unavailableResponse();
  }
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }
  if (request.nextUrl.search) {
    return noStoreJson({ error: "QUERY_NOT_SUPPORTED" }, 400);
  }
  if (
    Number(request.headers.get("content-length") ?? "0") > 16_384
  ) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (route.method === "GET" && request.body !== null) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }
  if (
    route.method === "POST" &&
    !routePath.endsWith("/logout") &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !==
      "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }
  if (routePath.endsWith("/logout") && !request.headers.has("x-csrf-token")) {
    return noStoreJson({ error: "CSRF_TOKEN_REQUIRED" }, 403);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const headers = new Headers({
    Accept: request.headers.get("accept") ?? "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });
  const browserOrigin = request.headers.get("origin");
  if (browserOrigin) headers.set("Origin", browserOrigin);
  if (route.method === "POST" && !routePath.endsWith("/logout")) {
    headers.set("Content-Type", "application/json");
  }
  if (routePath.endsWith("/logout")) {
    headers.set("X-CSRF-Token", request.headers.get("x-csrf-token") ?? "");
  }
  if (route.cookie) {
    const sessionCookie = request.cookies.get(authCookieName)?.value;
    const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
    const cookieParts: string[] = [];
    if (sessionCookie) cookieParts.push(`${authCookieName}=${sessionCookie}`);
    if (platformCookie) cookieParts.push(`${platformSessionCookieName}=${platformCookie}`);
    if (cookieParts.length > 0) headers.set("Cookie", cookieParts.join("; "));
  }

  const bodyResult = await readLimitedBody(request, 16_384);
  if (bodyResult.tooLarge) {
      return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (bodyResult.invalidEncoding) {
    return noStoreJson({ error: "INVALID_REQUEST_ENCODING" }, 400);
  }
  const bodyRequired = request.method === "POST" && !routePath.endsWith("/logout");
  if (!bodyRequired && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }
  const body = bodyRequired ? bodyResult.body : undefined;

  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds * 6
  );
  try {
    const upstream = await fetch(new URL(`/${routePath}`, origin), {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);
    if (
      upstream.status === 401 &&
      ["api/v1/auth/me", "api/v1/auth/context", "api/v1/auth/logout"].includes(
        routePath
      )
    ) {
      responseHeaders.set("Set-Cookie", expiredAuthCookie(request));
    }

    const incomingCookies = cookieHeaders(upstream);
    const successfulAuth =
      (routePath.endsWith("/login") || routePath.endsWith("/otp/verify") || routePath.endsWith("/otp/select-context")) &&
      upstream.status >= 200 &&
      upstream.status < 300;
    const isLogout =
      routePath.endsWith("/logout") &&
      upstream.status >= 200 &&
      upstream.status < 300;

    if (successfulAuth || isLogout) {
      if (incomingCookies.length > 0) {
        for (const cookie of incomingCookies) {
          if (
            !isAllowedAuthCookie(
              cookie,
              isLogout,
              request.nextUrl.protocol === "https:"
            )
          ) {
            return unavailableResponse();
          }
          responseHeaders.append("Set-Cookie", cookie);
        }
      }
    }

    const responseBody =
      upstream.status === 204 ||
      upstream.status === 205 ||
      upstream.status === 304
        ? null
        : await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
  return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

type TargetProxyRoute = {
  method: "GET" | "POST";
  session: boolean;
  body: "none" | "json";
  csrf: boolean;
};

function isTargetPath(routePath: string): boolean {
  return routePath.startsWith("api/v1/targets");
}

function targetRoute(
  routePath: string,
  method: string
): TargetProxyRoute | null {
  const routes: Array<[RegExp, TargetProxyRoute]> = [
    [
      /^api\/v1\/targets\/types$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/targets\/administrators$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/targets\/admin$/,
      { method: "POST", session: true, body: "json", csrf: false },
    ],
    [
      /^api\/v1\/targets\/admin$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/targets\/allocate$/,
      { method: "POST", session: true, body: "json", csrf: false },
    ],
    [
      /^api\/v1\/targets\/agent$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/targets\/summary$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/targets/${uuidPattern}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/targets/${uuidPattern}/adjust$`),
      { method: "POST", session: true, body: "json", csrf: false },
    ],
    [
      new RegExp(`^api/v1/targets/${uuidPattern}/adjustments$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
  ];

  return routes.find(([pattern, r]) => pattern.test(routePath) && r.method === method)?.[1] ?? null;
}

async function proxyTarget(
  request: NextRequest,
  route: TargetProxyRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }

  const maxBytes = 32_768;
  if (Number(request.headers.get("content-length") ?? "0") > maxBytes) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, maxBytes);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }
  if (route.body === "none" && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }

  const headers = new Headers({
    Accept: "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });

  const sessionCookie = request.cookies.get(authCookieName)?.value;
  const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
  const cookieParts: string[] = [];
  if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
    cookieParts.push(`${authCookieName}=${sessionCookie}`);
  }
  if (platformCookie && /^[A-Za-z0-9_-]{1,128}$/.test(platformCookie)) {
    cookieParts.push(`${platformSessionCookieName}=${platformCookie}`);
  }
  if (cookieParts.length > 0) {
    headers.set("Cookie", cookieParts.join("; "));
  }

  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }

  const targetUrl = new URL(`/${routePath}`, origin);
  const query = request.nextUrl.search;
  if (query) targetUrl.search = query;

  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds * 2
  );
  try {
    const upstream = await fetch(targetUrl, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);

    if (upstream.status === 401 && route.session) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, authCookieName, authCookiePath)
      );
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

interface ProfileProxyRoute {
  method: "GET" | "PATCH" | "POST";
  session: boolean;
  body: "none" | "json";
  csrf: boolean;
}

function isProfilePath(routePath: string): boolean {
  return routePath.startsWith("api/v1/profile");
}

function profileRoute(routePath: string, method: string): ProfileProxyRoute | null {
  const routes: [RegExp, ProfileProxyRoute][] = [
    [
      /^api\/v1\/profile\/me$/,
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      /^api\/v1\/profile\/me$/,
      { method: "PATCH", session: true, body: "json", csrf: false },
    ],
    [
      /^api\/v1\/profile\/mobile-change\/request$/,
      { method: "POST", session: true, body: "json", csrf: false },
    ],
    [
      /^api\/v1\/profile\/mobile-change\/verify$/,
      { method: "POST", session: true, body: "json", csrf: false },
    ],
    [
      new RegExp(`^api/v1/profile/${uuidPattern}$`),
      { method: "GET", session: true, body: "none", csrf: false },
    ],
    [
      new RegExp(`^api/v1/profile/${uuidPattern}$`),
      { method: "PATCH", session: true, body: "json", csrf: false },
    ],
  ];

  return routes.find(([pattern, r]) => pattern.test(routePath) && r.method === method)?.[1] ?? null;
}

async function proxyProfile(
  request: NextRequest,
  route: ProfileProxyRoute,
  routePath: string
): Promise<Response> {
  if (request.method !== route.method) {
    return new Response(null, {
      status: 405,
      headers: { Allow: route.method, "Cache-Control": "no-store" },
    });
  }

  const maxBytes = 65_536;
  if (Number(request.headers.get("content-length") ?? "0") > maxBytes) {
    return noStoreJson({ error: "REQUEST_BODY_TOO_LARGE" }, 413);
  }
  if (
    route.body === "json" &&
    request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json"
  ) {
    return noStoreJson({ error: "JSON_REQUIRED" }, 415);
  }

  const origin = validateBackendOrigin();
  if (!origin) return unavailableResponse();

  const bodyResult = await readLimitedBody(request, maxBytes);
  if (bodyResult.tooLarge || bodyResult.invalidEncoding) {
    return noStoreJson(
      { error: bodyResult.tooLarge ? "REQUEST_BODY_TOO_LARGE" : "INVALID_REQUEST_ENCODING" },
      bodyResult.tooLarge ? 413 : 400
    );
  }
  if (route.body === "none" && bodyResult.body.length > 0) {
    return noStoreJson({ error: "REQUEST_BODY_NOT_SUPPORTED" }, 400);
  }

  const headers = new Headers({
    Accept: "application/json",
    "X-Forwarded-Host": request.headers.get("host") ?? "",
  });

  const sessionCookie = request.cookies.get(authCookieName)?.value;
  const platformCookie = request.cookies.get(platformSessionCookieName)?.value;
  const cookieParts: string[] = [];
  if (sessionCookie && /^[A-Za-z0-9_-]{1,128}$/.test(sessionCookie)) {
    cookieParts.push(`${authCookieName}=${sessionCookie}`);
  }
  if (platformCookie && /^[A-Za-z0-9_-]{1,128}$/.test(platformCookie)) {
    cookieParts.push(`${platformSessionCookieName}=${platformCookie}`);
  }
  if (cookieParts.length > 0) {
    headers.set("Cookie", cookieParts.join("; "));
  }

  let body: string | undefined;
  if (route.body === "json") {
    if (!bodyResult.body) return noStoreJson({ error: "INVALID_REQUEST_BODY" }, 400);
    body = bodyResult.body;
    const incomingContentType = request.headers.get("content-type");
    headers.set("Content-Type", incomingContentType ?? "application/json");
  }

  const targetUrl = new URL(`/${routePath}`, origin);
  const query = request.nextUrl.search;
  if (query) targetUrl.search = query;

  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    upstreamTimeoutMilliseconds * 2
  );
  try {
    const upstream = await fetch(targetUrl, {
      method: route.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: controller.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("Content-Type", contentType);

    if (upstream.status === 401 && route.session) {
      responseHeaders.append(
        "Set-Cookie",
        expiredCookie(request, authCookieName, authCookiePath)
      );
    }
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) {
      return new Response(null, { status: upstream.status, headers: responseHeaders });
    }
    const responseBody = await upstream.arrayBuffer();
    return new Response(responseBody, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return unavailableResponse();
  } finally {
    clearTimeout(timeout);
  }
}

async function handleRequest(
  request: NextRequest,
  context: { params: { path: string[] } }
): Promise<Response> {
  const segments = pathSegments(context);
  const routePath = segments.join("/");
  if (segments[0] !== "api" || segments[1] !== "v1") {
    return noStoreJson({ error: "NOT_FOUND" }, 404);
  }
  if (routePath === healthPath.join("/")) {
    if (request.method !== "GET") {
      return new Response(null, {
        status: 405,
        headers: { Allow: "GET", "Cache-Control": "no-store" },
      });
    }
    return proxyLiveness(request);
  }
  const authRoute = authRoutes.get(routePath);
  if (authRoute) return proxyCommunityAuth(request, authRoute, routePath);

  const payoutRouteMatch = payoutRoute(routePath, request.method);
  if (payoutRouteMatch) return proxyPayout(request, payoutRouteMatch, routePath);
  if (isPayoutPath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, POST", "Cache-Control": "no-store" },
    });
  }

  const platformRouteMatch = platformRoute(routePath, request.method);
  if (platformRouteMatch) return proxyPlatform(request, platformRouteMatch, routePath);
  if (
    routePath.startsWith("api/v1/platform/") ||
    routePath === "api/v1/platform"
  ) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, POST, PATCH, DELETE", "Cache-Control": "no-store" },
    });
  }

  const agentRouteMatch = agentRoute(routePath, request.method);
  if (agentRouteMatch) return proxyAgent(request, agentRouteMatch, routePath);
  if (isAgentPath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, POST, PATCH, DELETE", "Cache-Control": "no-store" },
    });
  }

  const supportRouteMatch = supportRoute(routePath, request.method);
  if (supportRouteMatch) return proxySupport(request, supportRouteMatch, routePath);
  if (isSupportPath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, POST, PATCH, DELETE", "Cache-Control": "no-store" },
    });
  }

  const storageRouteMatch = storageRoute(routePath, request.method);
  if (storageRouteMatch) return proxyStorage(request, storageRouteMatch, routePath);
  if (isStoragePath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, POST", "Cache-Control": "no-store" },
    });
  }

  const commissionRouteMatch = commissionRoute(routePath, request.method);
  if (commissionRouteMatch) return proxyCommission(request, commissionRouteMatch, routePath);

  const subscriptionRouteMatch = subscriptionRoute(routePath, request.method);
  if (subscriptionRouteMatch) return proxySubscription(request, subscriptionRouteMatch, routePath);
  if (isSubscriptionPath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, POST", "Cache-Control": "no-store" },
    });
  }

  const paymentRouteMatch = paymentRoute(routePath, request.method);
  if (paymentRouteMatch) return proxyPayment(request, paymentRouteMatch, routePath);
  if (isPaymentPath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "POST", "Cache-Control": "no-store" },
    });
  }

  const notificationRouteMatch = notificationRoute(routePath, request.method);
  if (notificationRouteMatch) return proxyNotification(request, notificationRouteMatch, routePath);
  if (isNotificationPath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, POST", "Cache-Control": "no-store" },
    });
  }
  const targetRouteMatch = targetRoute(routePath, request.method);
  if (targetRouteMatch) return proxyTarget(request, targetRouteMatch, routePath);
  if (isTargetPath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, POST", "Cache-Control": "no-store" },
    });
  }

  const profileRouteMatch = profileRoute(routePath, request.method);
  if (profileRouteMatch) return proxyProfile(request, profileRouteMatch, routePath);
  if (isProfilePath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, PATCH, POST", "Cache-Control": "no-store" },
    });
  }

  if (isCommissionPath(routePath)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: "GET, POST", "Cache-Control": "no-store" },
    });
  }

return unavailableResponse();
}

export function GET(
  request: NextRequest,
  context: { params: { path: string[] } }
): Promise<Response> {
  return handleRequest(request, context);
}

export function HEAD(
  request: NextRequest,
  context: { params: { path: string[] } }
): Promise<Response> {
  return handleRequest(request, context);
}

export function POST(
  request: NextRequest,
  context: { params: { path: string[] } }
): Promise<Response> {
  return handleRequest(request, context);
}

export function PUT(
  request: NextRequest,
  context: { params: { path: string[] } }
): Promise<Response> {
  return handleRequest(request, context);
}

export function PATCH(
  request: NextRequest,
  context: { params: { path: string[] } }
): Promise<Response> {
  return handleRequest(request, context);
}

export function DELETE(
  request: NextRequest,
  context: { params: { path: string[] } }
): Promise<Response> {
  return handleRequest(request, context);
}

export function OPTIONS(
  request: NextRequest,
  context: { params: { path: string[] } }
): Promise<Response> {
  return handleRequest(request, context);
}
