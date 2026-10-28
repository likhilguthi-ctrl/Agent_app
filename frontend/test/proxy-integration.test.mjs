import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { cp, copyFile, mkdtemp, rm } from "node:fs/promises";
import http from "node:http";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { after, before, test } from "node:test";

const frontendDirectory = fileURLToPath(new URL("../", import.meta.url));
const customServer = path.join(frontendDirectory, "server.cjs");

let mockServer;
let nextProcess;
let nextOrigin;
let testProjectDirectory;
let capturedRequests = [];
let nextOutput = "";
let mockResponseOptions = { status: 200, mode: null, setCookie: false };

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });
}

async function unusedPort() {
  const server = net.createServer();
  const port = await listen(server);
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
  return port;
}

async function waitForNext() {
  const deadline = Date.now() + 120_000;
  let lastError;

  while (Date.now() < deadline) {
    if (nextProcess.exitCode !== null) {
      throw new Error(`Next.js exited before becoming ready.\n${nextOutput}`);
    }
    try {
      const response = await fetch(`${nextOrigin}/backend-api/api/v1/health`, {
        signal: AbortSignal.timeout(2_000),
      });
      if (response.status === 200) return;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }

  throw new Error(
    `Next.js did not become ready: ${lastError?.message ?? "startup timed out"}\n${nextOutput}`
  );
}

function minimalProcessEnvironment(upstreamOrigin) {
  const inheritedKeys = [
    "APPDATA",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
  ];
  const environment = Object.fromEntries(
    inheritedKeys
      .map((key) => [key, process.env[key]])
      .filter(([, value]) => value !== undefined)
  );
  return {
    ...environment,
    BACKEND_API_BASE_URL: upstreamOrigin,
    CI: "1",
    HOST: "127.0.0.1",
    NODE_ENV: "development",
    NEXT_TELEMETRY_DISABLED: "1",
  };
}

function sendRawRequest({ method, path: requestPath, headers = {}, body }) {
  return new Promise((resolve, reject) => {
    const target = new URL(nextOrigin);
    const request = http.request(
      {
        hostname: target.hostname,
        port: target.port,
        path: requestPath,
        method,
        headers,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () =>
          resolve({
            status: response.statusCode,
            headers: response.headers,
            body: Buffer.concat(chunks).toString("utf8"),
          })
        );
      }
    );
    request.on("error", reject);
    if (body !== undefined) request.write(body);
    request.end();
  });
}

before(async () => {
  mockServer = http.createServer((request, response) => {
    const requestUrl = new URL(request.url ?? "/", "http://mock-upstream");
    const responseOptions = { ...mockResponseOptions };
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      capturedRequests.push({
        method: request.method,
        url: requestUrl.pathname,
        search: requestUrl.search,
        headers: request.headers,
        body: Buffer.concat(chunks),
      });

      if (responseOptions.mode === "disconnect") {
        request.socket.destroy();
        return;
      }
      if (responseOptions.mode === "timeout") {
        setTimeout(() => {
          if (!response.destroyed) response.end('{"status":"late"}');
        }, 3_000);
        return;
      }

      let status = responseOptions.status;
      const headers = { "Content-Type": "application/json" };
      let responseBody;
      if (requestUrl.pathname === "/api/v1/platform/auth/oidc/start") {
        status = 302;
        headers.Location = "https://identity.example.test/authorize?state=test";
        headers["Set-Cookie"] =
          "community_platform_oidc_binding=binding-token; Path=/backend-api/api/v1/platform/auth/oidc; HttpOnly; SameSite=Lax; Max-Age=300";
      } else if (requestUrl.pathname === "/api/v1/platform/auth/oidc/callback") {
        status = 303;
        headers.Location = "/";
        headers["Set-Cookie"] = [
          "community_platform_session=platform-session; Path=/backend-api/api/v1/platform; HttpOnly; SameSite=Lax; Max-Age=3600",
          "community_platform_oidc_binding=; Path=/backend-api/api/v1/platform/auth/oidc; HttpOnly; SameSite=Lax; Max-Age=0",
        ];
      } else if (requestUrl.pathname === "/api/v1/platform/auth/oidc/logout") {
        status = 204;
        headers["Set-Cookie"] =
          "community_platform_session=; Path=/backend-api/api/v1/platform; HttpOnly; SameSite=Lax; Max-Age=0";
      } else if (requestUrl.pathname.endsWith("/activation-intents")) {
        status = 202;
      } else if (requestUrl.pathname === "/api/v1/auth/login") {
        const bodyStr = Buffer.concat(chunks).toString("utf-8");
        if (bodyStr.includes("wrong-password")) {
          status = 401;
          responseBody = JSON.stringify({ detail: "Invalid username or password." });
        } else if (bodyStr.includes("simulate_503_messaging")) {
          status = 503;
          responseBody = JSON.stringify({
            error: {
              code: "MESSAGING_UNAVAILABLE",
              message: "The messaging service is temporarily unavailable.",
            },
          });
        } else if (bodyStr.includes("simulate_429_rate_limit")) {
          status = 429;
          responseBody = JSON.stringify({
            error: {
              code: "RATE_LIMIT_EXCEEDED",
              message: "Too many authentication attempts. Try again later.",
            },
          });
        } else if (bodyStr.includes("two_factor")) {
          status = 200;
          responseBody = JSON.stringify({
            status: "otp_required",
            challenge_id: "test-challenge-uuid-1234",
            masked_mobile: "+91 91002 ****05",
            cooldown_seconds: 60,
          });
        } else if (bodyStr.includes("demo_superadmin")) {
          headers["Set-Cookie"] = [
            "community_session=superadmin-session; Path=/backend-api/api/v1; HttpOnly; SameSite=Lax; Max-Age=43200",
            "community_platform_session=superadmin-platform; Path=/backend-api/api/v1/platform; HttpOnly; SameSite=Lax; Max-Age=3600",
          ];
          responseBody = JSON.stringify({
            user_id: "00000000-0000-0000-0000-000000000001",
            username: "demo_superadmin",
            role: "SUPER_ADMIN",
            status: "active",
            csrf_token: "super-csrf-token",
            expires_at: "2026-10-16T12:00:00Z",
          });
        } else {
          headers["Set-Cookie"] =
            "community_session=login-session; Path=/backend-api/api/v1; HttpOnly; SameSite=Lax; Max-Age=43200";
          responseBody = JSON.stringify({
            user_id: "00000000-0000-0000-0000-000000000002",
            username: "demo_admin",
            role: "ADMIN",
            status: "active",
            tenant_id: "00000000-0000-0000-0000-000000000003",
            tenant: { id: "00000000-0000-0000-0000-000000000003", slug: "demo-community" },
            csrf_token: "admin-csrf-token",
            expires_at: "2026-10-16T12:00:00Z",
          });
        }
      } else if (requestUrl.pathname === "/api/v1/auth/me") {
        const cookie = request.headers.cookie || "";
        if (cookie.includes("superadmin-session")) {
          responseBody = JSON.stringify({
            user_id: "00000000-0000-0000-0000-000000000001",
            username: "demo_superadmin",
            role: "SUPER_ADMIN",
            status: "active",
            csrf_token: "super-csrf-token",
            expires_at: "2026-10-16T12:00:00Z",
          });
        } else if (cookie.includes("login-session") || cookie.includes("trusted-session")) {
          responseBody = JSON.stringify({
            user_id: "00000000-0000-0000-0000-000000000002",
            username: "demo_admin",
            role: "ADMIN",
            status: "active",
            tenant_id: "00000000-0000-0000-0000-000000000003",
            tenant: { id: "00000000-0000-0000-0000-000000000003", slug: "demo-community" },
            csrf_token: "admin-csrf-token",
            expires_at: "2026-10-16T12:00:00Z",
          });
        }
      } else if (requestUrl.pathname === "/api/v1/auth/otp/verify") {
        headers["Set-Cookie"] =
          "community_session=dev-session; Path=/backend-api/api/v1; HttpOnly; SameSite=Lax; Max-Age=43200";
      } else if (requestUrl.pathname === "/api/v1/auth/logout") {
        headers["Set-Cookie"] =
          "community_session=; Path=/backend-api/api/v1; HttpOnly; SameSite=Lax; Max-Age=0";
      } else if (responseOptions.setCookie) {
        headers["Set-Cookie"] = [
          "upstream_session=opaque; Path=/api/v1/auth; HttpOnly; SameSite=Lax",
          "upstream_marker=present; Path=/",
        ];
      }
      response.writeHead(status, headers);
      if (requestUrl.pathname === "/api/v1/auth/context" && status === 200) {
        response.end(
          JSON.stringify({
            authentication: { status: "authenticated" },
            principal: { type: "tenant_member", id: "trusted-user" },
            authorization: {
              status: "resolved",
              effective_permissions: [],
              community_scope: {
                status: "resolved",
                resolution: "current_trusted_tenant",
                communities: [{ id: "trusted-tenant", slug: "approved-community" }],
              },
            },
            employee_authority: {
              status: "unavailable",
              reason_code: "EMPLOYEE_LINK_NOT_ESTABLISHED",
            },
            product_role: {
              status: "unavailable",
              reason_code: "PRODUCT_ROLE_MAPPING_NOT_APPROVED",
            },
          })
        );
      } else {
        response.end(responseBody ?? JSON.stringify({ status, search: requestUrl.search }));
      }
    });
  });
  const upstreamPort = await listen(mockServer);
  const upstreamOrigin = `http://127.0.0.1:${upstreamPort}`;

  testProjectDirectory = await mkdtemp(
    path.join(frontendDirectory, ".phase428-proxy-test-")
  );
  await Promise.all([
    cp(path.join(frontendDirectory, "src"), path.join(testProjectDirectory, "src"), {
      recursive: true,
    }),
    cp(
      path.join(frontendDirectory, "public"),
      path.join(testProjectDirectory, "public"),
      { recursive: true }
    ),
    ...[
      "next.config.mjs",
      "package.json",
      "postcss.config.mjs",
      "server.cjs",
      "tailwind.config.ts",
      "tsconfig.json",
    ].map((filename) =>
      copyFile(
        path.join(frontendDirectory, filename),
        path.join(testProjectDirectory, filename)
      )
    ),
  ]);

  const nextPort = await unusedPort();
  nextOrigin = `http://127.0.0.1:${nextPort}`;
  const nextEnvironment = minimalProcessEnvironment(upstreamOrigin);
  nextProcess = spawn(
    process.execPath,
    [customServer, "--hostname", "127.0.0.1", "--port", String(nextPort)],
    {
      cwd: testProjectDirectory,
      env: nextEnvironment,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    }
  );
  nextProcess.stdout.setEncoding("utf8").on("data", (chunk) => {
    nextOutput = `${nextOutput}${chunk}`.slice(-12_000);
  });
  nextProcess.stderr.setEncoding("utf8").on("data", (chunk) => {
    nextOutput = `${nextOutput}${chunk}`.slice(-12_000);
  });

  await waitForNext();
  capturedRequests = [];
});

after(async () => {
  if (nextProcess && nextProcess.exitCode === null) {
    const exited = once(nextProcess, "exit");
    nextProcess.kill();
    await Promise.race([
      exited,
      new Promise((resolve) => setTimeout(resolve, 5_000)),
    ]);
  }
  if (mockServer?.listening) {
    mockServer.closeAllConnections();
    await new Promise((resolve, reject) => {
      mockServer.close((error) => (error ? reject(error) : resolve()));
    });
  }
  if (testProjectDirectory) {
    await rm(testProjectDirectory, { recursive: true, force: true });
  }
});

test("only the fixed liveness endpoint and Accept header reach the upstream", async () => {
  const response = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/health",
    headers: {
      Accept: "application/json",
      Cookie:
        "community_session=tenant-session; unrelated=tracking; ep_session=%7B%22type%22%3A%22admin%22%7D",
      Authorization: "Bearer proxy-test-token",
      Origin: "http://127.0.0.1:3000",
      "X-CSRF-Token": "proxy-test-csrf",
      "X-Tenant-ID": "client-tenant",
      "X-Identity-ID": "client-identity",
      "X-User-ID": "client-user",
      "X-Forwarded-Host": "attacker.invalid",
    },
  });
  assert.equal(response.status, 200);

  const captured = capturedRequests.at(-1);
  assert.ok(captured, "the mock upstream should receive the liveness request");
  assert.equal(captured.method, "GET");
  assert.equal(captured.url, "/api/v1/health");
  assert.equal(captured.search, "");
  assert.equal(captured.headers.accept, "application/json");
  for (const header of [
    "cookie",
    "authorization",
    "origin",
    "x-csrf-token",
    "x-tenant-id",
    "x-identity-id",
    "x-user-id",
    "x-forwarded-host",
  ]) {
    assert.equal(captured.headers[header], undefined, `${header} must not be forwarded`);
  }
  assert.equal(captured.body.length, 0);
  assert.deepEqual(JSON.parse(response.body), {
    status: 200,
    search: "",
  });

  const beforeQuery = capturedRequests.length;
  const queryResponse = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/health?url=https%3A%2F%2Fattacker.invalid",
  });
  assert.equal(queryResponse.status, 400);
  assert.equal(capturedRequests.length, beforeQuery);
});

test("manifest is valid JSON and login renders mobile web app metadata", async () => {
  const manifest = await sendRawRequest({
    method: "GET",
    path: "/manifest.webmanifest",
  });

  assert.equal(manifest.status, 200);
  assert.match(manifest.headers["content-type"], /application\/manifest\+json/i);
  const parsedManifest = JSON.parse(manifest.body);
  assert.equal(parsedManifest.name, "Sahayak");
  assert.equal(parsedManifest.short_name, "Sahayak");
  assert.equal(parsedManifest.theme_color, "#ffffff");
  assert.equal(parsedManifest.background_color, "#ffffff");
  assert.deepEqual(
    parsedManifest.icons.map(({ src }) => src),
    ["/icon-192.png", "/icon-512.png"]
  );

  const login = await sendRawRequest({ method: "GET", path: "/login" });
  assert.equal(login.status, 200);
  assert.match(login.body, /name="mobile-web-app-capable" content="yes"/);
  assert.match(login.body, /name="apple-mobile-web-app-capable" content="yes"/);
  assert.match(login.body, /href="\/manifest\.webmanifest"/);
  assert.doesNotMatch(login.body, /Internal Server Error|Application error/i);
});

test("unsupported methods and request bodies are rejected without upstream access", async () => {
  const beforeRequests = capturedRequests.length;
  for (const method of ["HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]) {
    const response = await sendRawRequest({
      method,
      path: "/backend-api/api/v1/health",
      body: method === "POST" ? "do-not-forward" : undefined,
    });
    assert.equal(response.status, 405, `${method} should be rejected`);
    assert.equal(response.headers.allow, "GET");
  }
  const bodyOnGet = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/health",
    headers: { "Content-Length": "14" },
    body: "do-not-forward",
  });
  assert.equal(bodyOnGet.status, 400);
  assert.equal(capturedRequests.length, beforeRequests);
});

test("authentication and unknown API routes fail closed without forwarding", async () => {
  const beforeRequests = capturedRequests.length;
  for (const headers of [
    {
      Cookie: "community_session=tenant-session; ep_session=legacy-secret",
      Origin: "https://untrusted.invalid",
      "X-CSRF-Token": "csrf-token",
      "X-Tenant-ID": "client-tenant",
    },
    {
      Cookie: "community_session=tenant-session",
      Origin: "https://configured-origin.invalid",
      "X-CSRF-Token": "csrf-token",
    },
  ]) {
    const authResponse = await sendRawRequest({
      method: "POST",
      path: "/backend-api/api/v1/auth/logout",
      headers,
      body: "sensitive-body",
    });
    assert.equal(authResponse.status, 503);
    assert.equal(authResponse.body.includes("legacy-secret"), false);
    assert.equal(authResponse.body.includes("csrf-token"), false);
  }

  const identityResponse = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/platform/auth/oidc/me",
    headers: { Cookie: "community_session=community-secret" },
  });
  assert.equal(identityResponse.status, 200);
  assert.equal(capturedRequests.at(-1).headers.cookie, undefined);
  assert.equal(identityResponse.body.includes("community-secret"), false);

  const unknownApi = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/unimplemented",
  });
  assert.equal(unknownApi.status, 503);
  assert.equal(capturedRequests.length, beforeRequests + 1);
  for (const marker of [
    "legacy-secret",
    "csrf-token",
    "sensitive-body",
    "community-secret",
  ]) {
    assert.equal(nextOutput.includes(marker), false, `${marker} must not be logged`);
  }
});

test("platform proxy isolates OIDC cookies and enforces exact mutation boundaries", async () => {
  const started = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/platform/auth/oidc/start",
  });
  assert.equal(started.status, 302);
  assert.equal(started.headers.location, "https://identity.example.test/authorize?state=test");
  assert.equal(started.headers["referrer-policy"], "no-referrer");
  const startCookie = [].concat(started.headers["set-cookie"]).join("; ");
  assert.match(startCookie, /community_platform_oidc_binding=binding-token/);
  assert.match(startCookie, /Path=\/backend-api\/api\/v1\/platform\/auth\/oidc/);
  assert.equal(capturedRequests.at(-1).headers.cookie, undefined);

  const callback = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/platform/auth/oidc/callback?state=callback-state-secret&code=callback-code-secret",
    headers: {
      Cookie: "community_platform_oidc_binding=binding-token; community_session=community-secret; ep_session=legacy",
      "X-Internal-Platform-OIDC-Callback-Query": "state=forged&code=forged",
    },
  });
  assert.equal(callback.status, 303);
  assert.equal(callback.headers.location, "/superadmin/communities");
  assert.equal(callback.headers["referrer-policy"], "no-referrer");
  const callbackCookies = [].concat(callback.headers["set-cookie"]).join("; ");
  assert.match(callbackCookies, /community_platform_session=platform-session/);
  assert.match(callbackCookies, /community_platform_oidc_binding=;/);
  const callbackUpstream = capturedRequests.at(-1);
  assert.equal(callbackUpstream.headers.cookie, "community_platform_oidc_binding=binding-token");
  assert.equal(
    callbackUpstream.search,
    "?state=callback-state-secret&code=callback-code-secret"
  );
  assert.equal(nextOutput.includes("callback-state-secret"), false);
  assert.equal(nextOutput.includes("callback-code-secret"), false);

  const upstreamRequestCount = capturedRequests.length;
  const unsupportedCallback = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/platform/auth/oidc/callback?state=unsupported-state-secret&code=unsupported-code-secret&debug=unexpected",
  });
  assert.equal(unsupportedCallback.status, 400);
  assert.equal(capturedRequests.length, upstreamRequestCount);
  assert.equal(nextOutput.includes("unsupported-state-secret"), false);
  assert.equal(nextOutput.includes("unsupported-code-secret"), false);

  const callbackWithTrailingSlash = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/platform/auth/oidc/callback/?state=slash-state-secret&code=slash-code-secret",
  });
  assert.notEqual(callbackWithTrailingSlash.status, 303);
  assert.equal(capturedRequests.length, upstreamRequestCount);
  assert.equal(nextOutput.includes("slash-state-secret"), false);
  assert.equal(nextOutput.includes("slash-code-secret"), false);

  const oversizedCallback = await sendRawRequest({
    method: "GET",
    path: `/backend-api/api/v1/platform/auth/oidc/callback?state=oversized-state-secret&code=${"x".repeat(5000)}`,
  });
  assert.equal(oversizedCallback.status, 400);
  assert.equal(capturedRequests.length, upstreamRequestCount);
  assert.equal(nextOutput.includes("oversized-state-secret"), false);

  const origin = nextOrigin;
  const identity = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/platform/auth/oidc/me",
    headers: {
      Cookie: "community_platform_session=platform-session; community_session=community-secret; ep_session=legacy",
    },
  });
  assert.equal(identity.status, 200);
  assert.equal(
    capturedRequests.at(-1).headers.cookie,
    "community_platform_session=platform-session"
  );

  const logout = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/platform/auth/oidc/logout",
    headers: {
      Origin: origin,
      "X-CSRF-Token": "csrf-secret",
      Cookie: "community_platform_session=platform-session; community_session=community-secret",
    },
  });
  assert.equal(logout.status, 204);
  const logoutCookie = [].concat(logout.headers["set-cookie"]).join("; ");
  assert.match(logoutCookie, /community_platform_session=;/);
  assert.match(logoutCookie, /Path=\/backend-api\/api\/v1\/platform/);
  assert.equal(
    capturedRequests.at(-1).headers.cookie,
    "community_platform_session=platform-session"
  );

  const forbiddenOrigin = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/platform/onboarding-requests",
    headers: {
      Origin: "https://attacker.invalid",
      "Content-Type": "application/json",
      "X-CSRF-Token": "csrf-secret",
      "Idempotency-Key": "onboarding-create-01",
      Cookie: "community_platform_session=platform-session",
    },
    body: JSON.stringify({ tenant_slug: "sample", domain: "sample.example.org" }),
  });
  assert.equal(forbiddenOrigin.status, 403);
  assert.equal(capturedRequests.at(-1).url, "/api/v1/platform/auth/oidc/logout");

  const created = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/platform/onboarding-requests",
    headers: {
      Origin: origin,
      "Content-Type": "application/json",
      "X-CSRF-Token": "csrf-secret",
      "Idempotency-Key": "onboarding-create-01",
      Cookie: "community_platform_session=platform-session; community_session=community-secret; ep_session=legacy",
    },
    body: JSON.stringify({ tenant_slug: "sample", domain: "sample.example.org" }),
  });
  assert.equal(created.status, 200, created.body);
  const createUpstream = capturedRequests.at(-1);
  assert.equal(createUpstream.url, "/api/v1/platform/onboarding-requests");
  assert.equal(createUpstream.method, "POST");
  assert.equal(createUpstream.headers.cookie, "community_platform_session=platform-session");
  assert.equal(createUpstream.headers.origin, origin);
  assert.equal(createUpstream.headers["x-csrf-token"], "csrf-secret");
  assert.equal(createUpstream.headers["idempotency-key"], "onboarding-create-01");
  assert.equal(createUpstream.headers["x-tenant-id"], undefined);
  assert.equal(createUpstream.headers.authorization, undefined);

  const onboardingId = "8c4f73b4-81a8-49a2-85a0-48742fbc9b2d";
  const eligibility = await sendRawRequest({
    method: "GET",
    path: `/backend-api/api/v1/platform/onboarding-requests/${onboardingId}/activation-eligibility`,
    headers: {
      Cookie: "community_platform_session=platform-session; community_session=community-secret",
    },
  });
  assert.equal(eligibility.status, 200, eligibility.body);
  assert.equal(
    capturedRequests.at(-1).url,
    `/api/v1/platform/onboarding-requests/${onboardingId}/activation-eligibility`
  );
  assert.equal(
    capturedRequests.at(-1).headers.cookie,
    "community_platform_session=platform-session"
  );

  const activationIntent = await sendRawRequest({
    method: "POST",
    path: `/backend-api/api/v1/platform/onboarding-requests/${onboardingId}/activation-intents`,
    headers: {
      Origin: origin,
      "X-CSRF-Token": "csrf-secret",
      "Idempotency-Key": "activation-intent-01",
      Cookie: "community_platform_session=platform-session; community_session=community-secret",
    },
  });
  assert.equal(activationIntent.status, 202, activationIntent.body);
  assert.equal(
    capturedRequests.at(-1).url,
    `/api/v1/platform/onboarding-requests/${onboardingId}/activation-intents`
  );
  assert.equal(
    capturedRequests.at(-1).headers.cookie,
    "community_platform_session=platform-session"
  );
  assert.equal(capturedRequests.at(-1).headers.origin, origin);
  assert.equal(capturedRequests.at(-1).headers["x-csrf-token"], "csrf-secret");
  assert.equal(
    capturedRequests.at(-1).headers["idempotency-key"],
    "activation-intent-01"
  );

  const queryInjection = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/platform/onboarding-requests?url=https%3A%2F%2Fattacker.invalid",
  });
  assert.equal(queryInjection.status, 400);
  const unknownPlatform = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/platform/operators",
  });
  assert.equal(unknownPlatform.status, 405);
});

test("community auth proxy forwards only trusted host and session data on explicit routes", async () => {
  const requestOrigin = nextOrigin;
  const otpBody = JSON.stringify({ mobile_number: "+14155550123" });
  const request = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/otp/request",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
      Cookie: "community_session=must-not-forward; ep_session=legacy-secret; tracking=x",
      "X-CSRF-Token": "must-not-forward",
      "X-Tenant-ID": "forged-tenant",
      "X-Identity-ID": "forged-identity",
      "X-User-ID": "forged-user",
      "X-Role": "admin",
      "X-Forwarded-Host": "attacker.invalid",
      Authorization: "******",
    },
    body: otpBody,
  });
  assert.equal(request.status, 200);
  let captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/auth/otp/request");
  assert.equal(captured.method, "POST");
  assert.equal(captured.body.toString(), otpBody);
  assert.equal(captured.headers.origin, requestOrigin);
  assert.equal(captured.headers["x-forwarded-host"], new URL(requestOrigin).host);
  for (const header of [
    "cookie",
    "authorization",
    "x-csrf-token",
    "x-tenant-id",
    "x-identity-id",
    "x-user-id",
    "x-role",
  ]) {
    assert.equal(captured.headers[header], undefined, `${header} must not be forwarded`);
  }
  assert.notEqual(captured.headers["x-forwarded-host"], "attacker.invalid");

  const verify = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/otp/verify",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
      Cookie: "ep_session=legacy-secret; community_session=forged-session",
    },
    body: JSON.stringify({
      mobile_number: "+14155550123",
      otp: "123456",
      challenge_id: "79c71528-8e78-4adf-8765-5488a4dfd0b3",
    }),
  });
  assert.equal(verify.status, 200);
  const verifyCookie = [].concat(verify.headers["set-cookie"]).join("; ");
  assert.match(verifyCookie, /^community_session=dev-session;/);
  assert.match(verifyCookie, /Path=\/backend-api\/api\/v1;/);
  assert.equal(verifyCookie.includes("ep_session"), false);
  captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/auth/otp/verify");
  assert.equal(captured.headers.cookie, undefined);

  const login = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/login",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
      Cookie: "ep_session=legacy-secret; community_session=forged-session",
    },
    body: JSON.stringify({
      username: "demo_admin",
      password: "AdminDemoPass123!",
    }),
  });
  assert.equal(login.status, 200);
  const loginCookie = [].concat(login.headers["set-cookie"]).join("; ");
  assert.match(loginCookie, /^community_session=login-session;/);
  assert.match(loginCookie, /Path=\/backend-api\/api\/v1;/);
  assert.equal(loginCookie.includes("ep_session"), false);
  captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/auth/login");
  assert.equal(captured.headers.cookie, undefined);

  const me = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/auth/me",
    headers: {
      Cookie:
        "community_session=trusted-session; ep_session=legacy-secret; tracking=x",
    },
  });
  assert.equal(me.status, 200);
  captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/auth/me");
  assert.equal(captured.headers.cookie, "community_session=trusted-session");
  assert.equal(captured.headers.origin, undefined);

  const context = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/auth/context",
    headers: {
      Host: `approved-community.localhost:${new URL(nextOrigin).port}`,
      Cookie: "community_session=trusted-session; ep_session=legacy-secret",
    },
  });
  assert.equal(context.status, 200);
  const trustedContext = JSON.parse(context.body);
  assert.equal(
    trustedContext.authorization.community_scope.communities[0].slug,
    "approved-community"
  );
  assert.deepEqual(trustedContext.authorization.effective_permissions, []);
  captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/auth/context");
  assert.equal(
    captured.headers["x-forwarded-host"],
    `approved-community.localhost:${new URL(nextOrigin).port}`
  );
  assert.equal(captured.headers.cookie, "community_session=trusted-session");

  const logout = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/logout",
    headers: {
      Origin: requestOrigin,
      Cookie:
        "community_session=trusted-session; ep_session=legacy-secret; tracking=x",
      "X-CSRF-Token": "session-csrf-token",
    },
  });
  assert.equal(logout.status, 200);
  const logoutCookie = [].concat(logout.headers["set-cookie"]).join("; ");
  assert.match(logoutCookie, /Max-Age=0/);
  assert.match(logoutCookie, /Path=\/backend-api\/api\/v1;/);
  captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/auth/logout");
  assert.equal(captured.headers.cookie, "community_session=trusted-session");
  assert.equal(captured.headers.origin, requestOrigin);
  assert.equal(captured.headers["x-csrf-token"], "session-csrf-token");
  assert.equal(captured.headers["x-forwarded-host"], new URL(requestOrigin).host);
  assert.equal(captured.headers.authorization, undefined);
  assert.equal(captured.headers["x-tenant-id"], undefined);

  mockResponseOptions = { status: 401, mode: null, setCookie: false };
  const unauthenticated = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/auth/context",
    headers: { Cookie: "community_session=expired-session" },
  });
  mockResponseOptions = { status: 200, mode: null, setCookie: false };
  assert.equal(unauthenticated.status, 401);
  const expiredCookie = [].concat(unauthenticated.headers["set-cookie"]).join("; ");
  assert.match(expiredCookie, /^community_session=;/);
  assert.match(expiredCookie, /Path=\/backend-api\/api\/v1;/);
  assert.match(expiredCookie, /Max-Age=0/);
  assert.match(expiredCookie, /HttpOnly/);
  assert.match(unauthenticated.headers["cache-control"], /no-store/);
  assert.equal(capturedRequests.at(-1).url, "/api/v1/auth/context");

  mockResponseOptions = { status: 200, mode: "disconnect", setCookie: false };
  const unavailable = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/otp/request",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
    },
    body: otpBody,
  });
  mockResponseOptions = { status: 200, mode: null, setCookie: false };
  assert.equal(unavailable.status, 503);
  assert.deepEqual(JSON.parse(unavailable.body), {
    error: "BACKEND_PROXY_UNAVAILABLE",
  });
});

test("legacy business API families remain unavailable through the FastAPI proxy", async () => {
  const beforeRequests = capturedRequests.length;
  const requests = [
    ...[
      "packages",
      "packages/client-supplied-offering-id",
      "employees",
      "employee-profile",
      "users",
      "support",
      "activity",
      "pay-lookup",
      "pay-complete",
    ].map((path) => ({
      method: "GET",
      path: `/backend-api/api/v1/${path}`,
    })),
    {
      method: "POST",
      path: "/backend-api/api/v1/enrollments",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        packageId: "client-supplied-offering-id",
        amount: 1,
        discountPercent: 100,
        tenant_id: "client-selected-tenant",
      }),
    },
  ];

  for (const request of requests) {
    const response = await sendRawRequest({
      ...request,
      headers: {
        ...request.headers,
        Cookie: "ep_session=untrusted-legacy-session",
        "X-Tenant-ID": "client-selected-tenant",
        "X-Identity-ID": "client-selected-identity",
      },
    });
    assert.equal(response.status, 503, `${request.path} must remain unavailable`);
    assert.doesNotMatch(response.body, /untrusted-legacy-session|client-selected/);
    assert.doesNotMatch(response.body, /amount|discountPercent/i);
  }

  assert.equal(capturedRequests.length, beforeRequests);
});

test("legacy payment completion cannot mark a record paid without settlement evidence", async () => {
  const beforeRequests = capturedRequests.length;
  const token = "unverified-payment-token";
  const response = await sendRawRequest({
    method: "POST",
    path: "/api/pay-complete",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });

  assert.equal(response.status, 503);
  assert.equal(response.headers["cache-control"], "no-store");
  assert.deepEqual(JSON.parse(response.body), {
    error: "PAYMENT_SETTLEMENT_UNAVAILABLE",
  });
  assert.equal(response.body.includes(token), false);
  assert.equal(response.body.includes("ok"), false);
  assert.equal(capturedRequests.length, beforeRequests);
  assert.doesNotMatch(nextOutput, new RegExp(token));

  const legacySession = encodeURIComponent(
    JSON.stringify({
      type: "employee",
      id: "forged-employee-id",
      refId: "forged-employee-ref",
      name: "Forged Employee",
    })
  );
  const settlementAttempts = [
    {
      method: "POST",
      path: "/api/users",
      body: {
        name: "Test Subscriber",
        contact: "5555555555",
        packageId: "client-package",
        billingCycle: "monthly",
        subscriptionType: "existing",
        consentAccepted: true,
      },
    },
    {
      method: "PATCH",
      path: "/api/users/client-subscriber",
      body: { action: "mark-paid" },
    },
    {
      method: "PATCH",
      path: "/api/users/client-subscriber",
      body: { subscriptionType: "existing" },
    },
  ];
  for (const attempt of settlementAttempts) {
    const denied = await sendRawRequest({
      method: attempt.method,
      path: attempt.path,
      headers: {
        Cookie: `ep_session=${legacySession}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(attempt.body),
    });
    assert.equal(denied.status, 503, `${attempt.method} ${attempt.path}`);
    assert.deepEqual(JSON.parse(denied.body), {
      error:
        attempt.method === "POST"
          ? "SUBSCRIBER_ENROLLMENT_UNAVAILABLE"
          : "PAYMENT_SETTLEMENT_UNAVAILABLE",
    });
    assert.equal(denied.body.includes("forged-employee"), false);
  }
  assert.equal(capturedRequests.length, beforeRequests);
});

test("legacy subscriber enrollment stays unavailable without echoing submitted details", async () => {
  const marker = "untrusted-subscriber-personal-data";
  const forgedEmployee = encodeURIComponent(
    JSON.stringify({
      type: "employee",
      id: "forged-employee-id",
      refId: "forged-employee-ref",
      name: "Forged Employee",
    })
  );
  const beforeRequests = capturedRequests.length;

  for (const subscriptionType of ["payment_link", "notify_confirm"]) {
    const response = await sendRawRequest({
      method: "POST",
      path: "/api/users",
      headers: {
        Cookie: `ep_session=${forgedEmployee}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        name: marker,
        contact: "5555555555",
        packageId: "client-selected-package",
        amount: 1,
        subscriptionType,
      }),
    });

    assert.equal(response.status, 503);
    assert.equal(response.headers["cache-control"], "no-store");
    assert.deepEqual(JSON.parse(response.body), {
      error: "SUBSCRIBER_ENROLLMENT_UNAVAILABLE",
    });
    assert.equal(response.body.includes(marker), false);
  }

  assert.equal(capturedRequests.length, beforeRequests);
  assert.equal(nextOutput.includes(marker), false);
});

test("public payment lookup no longer discloses subscriber details by bearer token", async () => {
  const beforeRequests = capturedRequests.length;
  const token = "private-legacy-payment-token";
  const response = await sendRawRequest({
    method: "GET",
    path: `/api/pay-lookup?token=${token}`,
    headers: {
      Cookie: "ep_session=%7B%22type%22%3A%22admin%22%7D",
    },
  });

  assert.equal(response.status, 503);
  assert.equal(response.headers["cache-control"], "no-store");
  assert.deepEqual(JSON.parse(response.body), {
    error: "PAYMENT_LOOKUP_UNAVAILABLE",
  });
  assert.equal(response.body.includes(token), false);
  assert.doesNotMatch(response.body, /subscriber|amount|employee|package/i);
  assert.equal(capturedRequests.length, beforeRequests);
  assert.doesNotMatch(nextOutput, new RegExp(token));
});

test("legacy employee OTP endpoints fail closed without revealing or creating sessions", async () => {
  const beforeRequests = capturedRequests.length;
  const body = JSON.stringify({
    mobile: "5555555555",
    otp: "123456",
    name: "Forged Employee",
    refId: "forged-ref",
    role: "superadmin",
  });

  for (const path of [
    "/api/auth/employee-otp/request",
    "/api/auth/employee-otp/verify",
  ]) {
    const response = await sendRawRequest({
      method: "POST",
      path,
      headers: { "Content-Type": "application/json" },
      body,
    });

    assert.equal(response.status, 503, path);
    assert.equal(response.headers["cache-control"], "no-store");
    assert.deepEqual(JSON.parse(response.body), {
      error: "EMPLOYEE_AUTH_UNAVAILABLE",
    });
    assert.doesNotMatch(response.body, /demoOtp|Forged Employee|forged-ref|superadmin/i);
    assert.equal(response.headers["set-cookie"], undefined);
  }

  assert.equal(capturedRequests.length, beforeRequests);
});

test("employee provisioning cannot trust a forged admin cookie or write public documents", async () => {
  const marker = "sensitive-document-marker";
  const boundary = "phase440-employee-form";
  const body = [
    `--${boundary}`,
    'Content-Disposition: form-data; name="firstName"',
    "",
    marker,
    `--${boundary}`,
    'Content-Disposition: form-data; name="aadharImage"; filename="private.jpg"',
    "Content-Type: image/jpeg",
    "",
    marker,
    `--${boundary}--`,
    "",
  ].join("\r\n");
  const forgedAdmin = encodeURIComponent(
    JSON.stringify({
      type: "admin",
      id: "forged-id",
      role: "superadmin",
      name: "Forged Administrator",
    })
  );
  const response = await sendRawRequest({
    method: "POST",
    path: "/api/employees",
    headers: {
      Cookie: `ep_session=${forgedAdmin}`,
      "Content-Type": `multipart/form-data; boundary=${boundary}`,
    },
    body,
  });

  assert.equal(response.status, 503);
  assert.equal(response.headers["cache-control"], "no-store");
  assert.deepEqual(JSON.parse(response.body), {
    error: "EMPLOYEE_PROVISIONING_UNAVAILABLE",
  });
  assert.equal(response.body.includes(marker), false);
  assert.equal(response.headers["set-cookie"], undefined);
  assert.equal(nextOutput.includes(marker), false);
});

test("legacy employee logout only clears the unsigned compatibility cookie", async () => {
  const response = await sendRawRequest({
    method: "POST",
    path: "/api/auth/logout",
    headers: {
      Cookie: "ep_session=forged-session",
    },
  });

  assert.equal(response.status, 200);
  assert.deepEqual(JSON.parse(response.body), { ok: true });
  const cookieHeader = [].concat(response.headers["set-cookie"]).join("; ");
  assert.match(cookieHeader, /ep_session=;/);
  assert.match(cookieHeader, /Path=\//);
});

test("Mongo-backed legacy API requests return an error when runtime configuration is absent", async () => {
  const response = await sendRawRequest({
    method: "GET",
    path: "/api/packages",
  });

  assert.equal(response.status, 500);
  assert.doesNotMatch(response.body, /"packages"\s*:/);
  assert.doesNotMatch(response.body, /mongodb:\/\/|MONGODB_URI/i);
});

test("traversal and upstream URL manipulation cannot change the fixed target", async () => {
  const beforeRequests = capturedRequests.length;
  for (const path of [
    "/backend-api/api/v1/%2e%2e/health",
    "/backend-api/https://attacker.invalid/api/v1/health",
    "/backend-api/api/v1/health%2f..%2fauth",
  ]) {
    const response = await sendRawRequest({ method: "GET", path });
    assert.notEqual(response.status, 200, `${path} must not reach the health handler`);
  }
  assert.equal(capturedRequests.length, beforeRequests);
});

test("upstream status and body are preserved while Set-Cookie is suppressed", async () => {
  for (const status of [401, 403, 404, 503]) {
    mockResponseOptions = { status, mode: null, setCookie: false };
    const response = await sendRawRequest({
      method: "GET",
      path: "/backend-api/api/v1/health",
    });
    assert.equal(response.status, status);
    assert.deepEqual(JSON.parse(response.body), { status, search: "" });
  }

  mockResponseOptions = { status: 200, mode: null, setCookie: true };
  const cookieResponse = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/health",
  });
  mockResponseOptions = { status: 200, mode: null, setCookie: false };
  assert.equal(cookieResponse.status, 200);
  assert.equal(cookieResponse.headers["set-cookie"], undefined);
  assert.equal(cookieResponse.headers["cache-control"], "no-store");
});

test("upstream timeout and connection failures return a generic response without secret leakage", async () => {
  for (const mode of ["timeout", "disconnect"]) {
    mockResponseOptions = { status: 200, mode, setCookie: false };
    const response = await sendRawRequest({
      method: "GET",
      path: "/backend-api/api/v1/health",
      headers: {
        Cookie: "community_session=must-not-leak",
        Authorization: "Bearer must-not-leak",
      },
    });
    mockResponseOptions = { status: 200, mode: null, setCookie: false };
    assert.equal(response.status, 503);
    assert.deepEqual(JSON.parse(response.body), {
      error: "BACKEND_PROXY_UNAVAILABLE",
    });
    assert.equal(response.body.includes("must-not-leak"), false);
  }
  assert.equal(nextOutput.includes("must-not-leak"), false);
});

test("frontend login flow supports invalid credentials, role-based session establishment, and logout", async () => {
  const requestOrigin = nextOrigin;

  // 1. Invalid credentials
  const badLogin = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/login",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
    },
    body: JSON.stringify({
      username: "demo_admin",
      password: "wrong-password",
    }),
  });
  assert.equal(badLogin.status, 401);
  assert.equal(badLogin.headers["set-cookie"], undefined);

  // 1a-i. Upstream notification dispatch failure returns 503 with MESSAGING_UNAVAILABLE preserved
  const messagingUnavailable = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/login",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
    },
    body: JSON.stringify({
      username: "demo_admin",
      password: "simulate_503_messaging",
    }),
  });
  assert.equal(messagingUnavailable.status, 503);
  const msgErr = JSON.parse(messagingUnavailable.body);
  assert.equal(msgErr.error.code, "MESSAGING_UNAVAILABLE");

  // 1a-ii. Rate limited attempt returns 429 with RATE_LIMIT_EXCEEDED preserved
  const rateLimited = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/login",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
    },
    body: JSON.stringify({
      username: "demo_admin",
      password: "simulate_429_rate_limit",
    }),
  });
  assert.equal(rateLimited.status, 429);
  const rateErr = JSON.parse(rateLimited.body);
  assert.equal(rateErr.error.code, "RATE_LIMIT_EXCEEDED");

  // 1b. Two-Factor Step 1: credential verification returning otp_required without Set-Cookie
  const otpRequiredLogin = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/login",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
    },
    body: JSON.stringify({
      username: "demo_admin",
      password: "AdminDemoPass123!",
      two_factor: true,
    }),
  });
  assert.equal(otpRequiredLogin.status, 200);
  assert.equal(otpRequiredLogin.headers["set-cookie"], undefined);
  const otpBody = JSON.parse(otpRequiredLogin.body);
  assert.equal(otpBody.status, "otp_required");
  assert.equal(otpBody.challenge_id, "test-challenge-uuid-1234");
  assert.equal(otpBody.masked_mobile, "+91 91002 ****05");

  // 2. Super Admin login: sets both community_session and community_platform_session
  const superLogin = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/login",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
    },
    body: JSON.stringify({
      username: "demo_superadmin",
      password: "SuperDemoPass123!",
    }),
  });
  assert.equal(superLogin.status, 200);
  const superCookies = [].concat(superLogin.headers["set-cookie"]).join("; ");
  assert.match(superCookies, /community_session=superadmin-session/);
  assert.match(superCookies, /community_platform_session=superadmin-platform/);
  assert.match(superCookies, /Path=\/backend-api\/api\/v1;/);
  assert.match(superCookies, /Path=\/backend-api\/api\/v1\/platform/);

  // Verify session identity as SUPER_ADMIN
  const superIdentity = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/auth/me",
    headers: {
      Cookie: "community_session=superadmin-session",
    },
  });
  assert.equal(superIdentity.status, 200);
  const superIdentityBody = JSON.parse(superIdentity.body);
  assert.equal(superIdentityBody.role, "SUPER_ADMIN");
  assert.equal(superIdentityBody.username, "demo_superadmin");

  // 3. Tenant Admin login: sets community_session
  const adminLogin = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/login",
    headers: {
      "Content-Type": "application/json",
      Origin: requestOrigin,
    },
    body: JSON.stringify({
      username: "demo_admin",
      password: "AdminDemoPass123!",
    }),
  });
  assert.equal(adminLogin.status, 200);
  const adminCookies = [].concat(adminLogin.headers["set-cookie"]).join("; ");
  assert.match(adminCookies, /community_session=login-session/);
  assert.match(adminCookies, /Path=\/backend-api\/api\/v1;/);

  // Verify session identity as ADMIN
  const adminIdentity = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/auth/me",
    headers: {
      Cookie: "community_session=login-session",
    },
  });
  assert.equal(adminIdentity.status, 200);
  const adminIdentityBody = JSON.parse(adminIdentity.body);
  assert.equal(adminIdentityBody.role, "ADMIN");
  assert.equal(adminIdentityBody.tenant?.slug, "demo-community");

  // 4. Logout revokes session cookie
  const logout = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/auth/logout",
    headers: {
      Origin: requestOrigin,
      Cookie: "community_session=login-session",
      "X-CSRF-Token": adminIdentityBody.csrf_token,
    },
  });
  assert.equal(logout.status, 200);
  const logoutCookies = [].concat(logout.headers["set-cookie"]).join("; ");
  assert.match(logoutCookies, /Max-Age=0/);
});

test("sec01 and sec06: platform community routes strictly isolate platform session cookies and reject community session fallback", async () => {
  const communityId = "8c4f73b4-81a8-49a2-85a0-48742fbc9b2d";

  // 1. Sending community_session to platform appointment endpoint -> cookie is NOT forwarded upstream
  const resNoPlatformCookie = await sendRawRequest({
    method: "GET",
    path: `/backend-api/api/v1/platform/communities/${communityId}/appointments`,
    headers: {
      Cookie: "community_session=admin-session; ep_session=legacy-session",
    },
  });
  assert.equal(resNoPlatformCookie.status, 200);
  let captured = capturedRequests.at(-1);
  assert.equal(captured.url, `/api/v1/platform/communities/${communityId}/appointments`);
  assert.equal(captured.headers.cookie, undefined, "community_session must never be forwarded to platform endpoints");

  // 2. Sending community_platform_session alongside community_session -> ONLY platform session is forwarded
  const resWithPlatformCookie = await sendRawRequest({
    method: "GET",
    path: `/backend-api/api/v1/platform/communities/${communityId}/appointments`,
    headers: {
      Cookie: "community_platform_session=super-session; community_session=admin-session",
    },
  });
  assert.equal(resWithPlatformCookie.status, 200);
  captured = capturedRequests.at(-1);
  assert.equal(captured.url, `/api/v1/platform/communities/${communityId}/appointments`);
  assert.equal(captured.headers.cookie, "community_platform_session=super-session", "only community_platform_session must be forwarded");
});

test("payout proxy forwards community_session and Idempotency-Key on community payout routes and requires csrf on mutations", async () => {
  const requestOrigin = nextOrigin;

  // 1. GET eligible-balance forwards community_session
  const balanceRes = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/payouts/eligible-balance",
    headers: {
      Cookie: "community_session=agent-session-abc",
    },
  });
  assert.equal(balanceRes.status, 200);
  let captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/payouts/eligible-balance");
  assert.equal(captured.headers.cookie, "community_session=agent-session-abc");

  // 2. POST requests without CSRF fails with 403
  const csrfFail = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/payouts/requests",
    headers: {
      "Content-Type": "application/json",
      Cookie: "community_session=agent-session-abc",
    },
    body: JSON.stringify({ amount: 50.0, idempotency_key: "idemp-proxy-test-01" }),
  });
  assert.equal(csrfFail.status, 403);

  // 3. POST requests with CSRF and Idempotency-Key forwards correctly
  const postSuccess = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/payouts/requests",
    headers: {
      Origin: requestOrigin,
      "Content-Type": "application/json",
      Cookie: "community_session=agent-session-abc",
      "X-CSRF-Token": "valid-csrf-token-12345678",
      "Idempotency-Key": "idemp-proxy-test-01",
    },
    body: JSON.stringify({ amount: 50.0, idempotency_key: "idemp-proxy-test-01" }),
  });
  assert.equal(postSuccess.status, 200);
  captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/payouts/requests");
  assert.equal(captured.headers["idempotency-key"], "idemp-proxy-test-01");
  assert.equal(captured.headers.cookie, "community_session=agent-session-abc");
});

test("payout proxy forwards community_platform_session on platform payout routes and rejects unauthorized HTTP methods", async () => {
  const requestId = "7b2e35a1-4389-408a-b9a1-5d9c2e0b5c12";

  // 1. GET platform payout requests forwards platform session cookie
  const listRes = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/platform/payouts/requests",
    headers: {
      Cookie: "community_platform_session=super-operator-session; community_session=tenant-admin-session",
    },
  });
  assert.equal(listRes.status, 200);
  let captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/platform/payouts/requests");
  assert.equal(captured.headers.cookie, "community_platform_session=super-operator-session");

  // 2. DELETE on payout requests path is rejected with 405 Method Not Allowed
  const deleteRes = await sendRawRequest({
    method: "DELETE",
    path: `/backend-api/api/v1/payouts/requests/${requestId}`,
  });
  assert.equal(deleteRes.status, 405);
});

test("notification webhook proxy forwards raw body, required headers, and target URL unchanged", async () => {
  const eventId = "evt_local_proxy_test_001";
  const timestamp = "2026-10-23T10:30:00Z";
  const signature = "sha256=mockvalidsignaturehex123456789abcdef";
  const rawPayload = JSON.stringify({
    eventId,
    eventType: "message.status_changed",
    messageId: "msg_local_proxy_001",
    serviceId: "community-ott",
    status: "DELIVERED",
    channel: "WHATSAPP",
    occurredAt: timestamp,
  });

  const response = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/webhooks/notifications/status",
    headers: {
      "Content-Type": "application/json",
      "X-Notification-Timestamp": timestamp,
      "X-Notification-Signature": signature,
      "X-Event-ID": eventId,
    },
    body: rawPayload,
  });

  assert.equal(response.status, 200);
  const captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/webhooks/notifications/status");
  assert.equal(captured.method, "POST");
  assert.equal(captured.headers["x-notification-timestamp"], timestamp);
  assert.equal(captured.headers["x-notification-signature"], signature);
  assert.equal(captured.headers["x-event-id"], eventId);
  assert.equal(captured.headers["content-type"], "application/json");
  assert.equal(captured.body.toString("utf-8"), rawPayload, "raw request body must be forwarded byte-for-byte unchanged");

  // Unsupported method DELETE on webhook endpoint is rejected with 405
  const deleteRes = await sendRawRequest({
    method: "DELETE",
    path: "/backend-api/api/v1/webhooks/notifications/status",
  });
  assert.equal(deleteRes.status, 405);
  assert.equal(deleteRes.headers["allow"], "GET, POST");

  // Non-JSON Content-Type is rejected with 415
  const textRes = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/webhooks/notifications/status",
    headers: {
      "Content-Type": "text/plain",
      "X-Notification-Timestamp": timestamp,
      "X-Notification-Signature": signature,
      "X-Event-ID": eventId,
    },
    body: "hello world",
  });
  assert.equal(textRes.status, 415);
});

test("storage upload proxy forwards X-Registration-Verification-Token, Authorization, Idempotency-Key and multipart body", async () => {
  const regToken = "reg_verification_token_test_123";
  const idempotencyKey = "storage-upload-idem-001";
  const csrfToken = "csrf-token-storage-test";
  const authBearer = "Bearer session-token-storage-test";
  const payload = JSON.stringify({ category: "agent-photo", image_base64: "dGVzdA==" });

  const response = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/storage/upload",
    headers: {
      "Content-Type": "application/json",
      Origin: nextOrigin,
      "X-CSRF-Token": csrfToken,
      "X-Registration-Verification-Token": regToken,
      "Authorization": authBearer,
      "Idempotency-Key": idempotencyKey,
    },
    body: payload,
  });

  assert.equal(response.status, 200);
  const captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/storage/upload");
  assert.equal(captured.method, "POST");
  assert.equal(captured.headers["x-registration-verification-token"], regToken);
  assert.equal(captured.headers["authorization"], authBearer);
  assert.equal(captured.headers["idempotency-key"], idempotencyKey);
  assert.equal(captured.headers["content-type"], "application/json");
  assert.equal(captured.body.toString("utf-8"), payload);

  // Missing CSRF token on upload mutation is rejected with 403
  const noCsrfRes = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/storage/upload",
    headers: {
      "Content-Type": "application/json",
      Origin: nextOrigin,
    },
    body: payload,
  });
  assert.equal(noCsrfRes.status, 403);
});

test("storage preview proxy forwards query verification_token and enforces security", async () => {
  const regToken = "reg_preview_token_test_456";
  const objectKey = "agent-photos/da5e708d9f3c421ab1218924b6549231.jpg";

  const response = await sendRawRequest({
    method: "GET",
    path: `/backend-api/api/v1/storage/${objectKey}/preview?verification_token=${regToken}`,
  });

  assert.equal(response.status, 200);
  const captured = capturedRequests.at(-1);
  assert.equal(captured.url, `/api/v1/storage/${objectKey}/preview`);
  assert.equal(captured.search, `?verification_token=${regToken}`);
  assert.equal(captured.method, "GET");

  // Invalid query parameter is rejected with 400
  const badQueryRes = await sendRawRequest({
    method: "GET",
    path: `/backend-api/api/v1/storage/${objectKey}/preview?unsupported=123`,
  });
  assert.equal(badQueryRes.status, 400);
});

test("agent routes proxy forwards community_session and enforces CSRF and cookie path", async () => {
  // 1. GET /backend-api/api/v1/agents/regions forwards community_session
  const regionsRes = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/agents/regions",
    headers: {
      Cookie: "community_session=admin-session-xyz",
    },
  });
  assert.equal(regionsRes.status, 200);
  let captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/agents/regions");
  assert.equal(captured.headers.cookie, "community_session=admin-session-xyz");

  // 2. POST /backend-api/api/v1/agents forwards community_session, X-CSRF-Token and body
  const payload = JSON.stringify({
    first_name: "Ramesh",
    last_name: "Kumar",
    username: "ramesh_agent",
    mobile_e164: "+919876543210",
    verification_token: "test-verif-token",
    aadhaar_number: "123456789012",
    pan_number: "ABCDE1234F",
    profile_photo_key: "agent-photos/test.jpg",
    aadhaar_document_key: "agent-photos/aadhaar.jpg",
    pan_document_key: "agent-photos/pan.jpg",
    region_ids: [],
  });

  const createAgentRes = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/agents",
    headers: {
      "Content-Type": "application/json",
      Origin: nextOrigin,
      "X-CSRF-Token": "agent-csrf-token",
      Cookie: "community_session=admin-session-xyz",
    },
    body: payload,
  });
  assert.equal(createAgentRes.status, 200);
  captured = capturedRequests.at(-1);
  assert.equal(captured.url, "/api/v1/agents");
  assert.equal(captured.headers.cookie, "community_session=admin-session-xyz");
  assert.equal(captured.headers["x-csrf-token"], "agent-csrf-token");
  assert.equal(captured.headers.origin, nextOrigin);

  // 3. POST /backend-api/api/v1/agents requires CSRF
  const noCsrfAgentRes = await sendRawRequest({
    method: "POST",
    path: "/backend-api/api/v1/agents",
    headers: {
      "Content-Type": "application/json",
      Cookie: "community_session=admin-session-xyz",
    },
    body: payload,
  });
  assert.equal(noCsrfAgentRes.status, 403);

  // 4. Upstream 401 returns Set-Cookie expiring community_session with Path=/backend-api/api/v1
  mockResponseOptions = { status: 401, mode: null, setCookie: false };
  const unauthRegions = await sendRawRequest({
    method: "GET",
    path: "/backend-api/api/v1/agents/regions",
    headers: {
      Cookie: "community_session=expired-token",
    },
  });
  mockResponseOptions = { status: 200, mode: null, setCookie: false };
  assert.equal(unauthRegions.status, 401);
  const expiredSetCookie = [].concat(unauthRegions.headers["set-cookie"]).join("; ");
  assert.match(expiredSetCookie, /^community_session=;/);
  assert.match(expiredSetCookie, /Path=\/backend-api\/api\/v1;/);
  assert.match(expiredSetCookie, /Max-Age=0/);
});



