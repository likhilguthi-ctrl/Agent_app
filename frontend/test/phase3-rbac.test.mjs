import assert from "node:assert/strict";
import { test } from "node:test";

const BASE_URL = "http://localhost:3000";

test("Phase 3 RBAC - Unauthenticated requests return 401 on /me", async () => {
  const response = await fetch(`${BASE_URL}/backend-api/api/v1/auth/me`);
  assert.equal(response.status, 401, "Unauthenticated session must return 401 Unauthorized");
});

test("Phase 3 RBAC - Super Admin login establishes valid SUPER_ADMIN session", async () => {
  const loginRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      username: "demo_superadmin",
      password: "SuperDemoPass123!",
    }),
  });

  assert.equal(loginRes.status, 200, "Super Admin login should return 200");
  const loginData = await loginRes.json();
  assert.equal(loginData.role, "SUPER_ADMIN", "Role must be SUPER_ADMIN");
  assert.equal(loginData.username, "demo_superadmin");

  const cookieHeader = loginRes.headers.get("set-cookie");
  assert.ok(cookieHeader, "Login response must set session cookie");
  assert.match(cookieHeader, /community_session=/, "Session cookie name must be community_session");

  // Extract cookies for authenticated calls
  const cookies = cookieHeader.split(",").map(c => c.split(";")[0].trim()).join("; ");

  // Verify /me endpoint
  const meRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/me`, {
    headers: { Cookie: cookies },
  });
  assert.equal(meRes.status, 200, "/me should return 200 for valid session");
  const meData = await meRes.json();
  assert.equal(meData.role, "SUPER_ADMIN");
  assert.equal(meData.username, "demo_superadmin");
  assert.ok(meData.csrf_token, "Me response must include CSRF token");

  // Verify dashboard page shell renders
  const pageRes = await fetch(`${BASE_URL}/superadmin/dashboard`, {
    headers: { Cookie: cookies },
  });
  assert.equal(pageRes.status, 200, "/superadmin/dashboard should return 200");
  const pageHtml = await pageRes.text();
  assert.ok(pageHtml.length > 0, "Dashboard page HTML should be returned");

  // Logout
  const logoutRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/logout`, {
    method: "POST",
    headers: {
      Cookie: cookies,
      Origin: BASE_URL,
      "X-CSRF-Token": meData.csrf_token,
    },
  });
  assert.equal(logoutRes.status, 204, "Logout should succeed with 204 No Content");

  // Verify session invalidated
  const afterLogoutRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/me`, {
    headers: { Cookie: cookies },
  });
  assert.equal(afterLogoutRes.status, 401, "Session must be invalidated after logout");
});

test("Phase 3 RBAC - Community Admin login establishes valid ADMIN session with tenant context", async () => {
  const loginRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      username: "demo_admin",
      password: "AdminDemoPass123!",
    }),
  });

  assert.equal(loginRes.status, 200, "Admin login should return 200");
  const loginData = await loginRes.json();
  assert.equal(loginData.role, "ADMIN", "Role must be ADMIN");
  assert.equal(loginData.username, "demo_admin");
  assert.ok(loginData.tenant, "Tenant object must be present for Community Admin");
  assert.equal(loginData.tenant.slug, "demo-community");

  const cookieHeader = loginRes.headers.get("set-cookie");
  const cookies = cookieHeader.split(",").map(c => c.split(";")[0].trim()).join("; ");

  // Verify /me endpoint
  const meRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/me`, {
    headers: { Cookie: cookies },
  });
  assert.equal(meRes.status, 200);
  const meData = await meRes.json();
  assert.equal(meData.role, "ADMIN");
  assert.equal(meData.username, "demo_admin");
  assert.equal(meData.tenant?.slug, "demo-community");

  // Verify dashboard page shell renders
  const pageRes = await fetch(`${BASE_URL}/admin/dashboard`, {
    headers: { Cookie: cookies },
  });
  assert.equal(pageRes.status, 200, "/admin/dashboard should return 200");

  // Logout
  const logoutRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/logout`, {
    method: "POST",
    headers: {
      Cookie: cookies,
      Origin: BASE_URL,
      "X-CSRF-Token": meData.csrf_token,
    },
  });
  assert.equal(logoutRes.status, 204, "Admin logout should succeed with 204 No Content");

  // Verify session invalidated
  const afterLogoutRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/me`, {
    headers: { Cookie: cookies },
  });
  assert.equal(afterLogoutRes.status, 401);
});

test("Phase 3 RBAC - Agent dashboard route exists and renders shell", async () => {
  const pageRes = await fetch(`${BASE_URL}/agent/dashboard`);
  assert.equal(pageRes.status, 200);
  const html = await pageRes.text();
  assert.ok(html.includes("Verifying session..."), "Page renders AuthGuard session verification shell");
});

test("Phase 3 RBAC - Invalid credentials return 401 Unauthorized", async () => {
  const loginRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      username: "demo_admin",
      password: "WrongPassword123!",
    }),
  });
  assert.equal(loginRes.status, 401, "Invalid password must return 401");
});

test("Phase 3 RBAC - Logout requires valid CSRF token", async () => {
  // First login
  const loginRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      username: "demo_admin",
      password: "AdminDemoPass123!",
    }),
  });
  assert.equal(loginRes.status, 200);
  const cookieHeader = loginRes.headers.get("set-cookie");
  const cookies = cookieHeader.split(",").map(c => c.split(";")[0].trim()).join("; ");

  // Attempt logout without CSRF token
  const badLogoutRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/logout`, {
    method: "POST",
    headers: {
      Cookie: cookies,
      Origin: BASE_URL,
    },
  });
  assert.equal(badLogoutRes.status, 403, "Logout without CSRF token must return 403");

  // Attempt logout with invalid CSRF token
  const badTokenLogoutRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/logout`, {
    method: "POST",
    headers: {
      Cookie: cookies,
      Origin: BASE_URL,
      "X-CSRF-Token": "invalid_csrf_token_value",
    },
  });
  assert.equal(badTokenLogoutRes.status, 403, "Logout with bad CSRF token must return 403");

  // Get real token and logout cleanly
  const meRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/me`, {
    headers: { Cookie: cookies },
  });
  const meData = await meRes.json();
  const cleanLogoutRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/logout`, {
    method: "POST",
    headers: {
      Cookie: cookies,
      Origin: BASE_URL,
      "X-CSRF-Token": meData.csrf_token,
    },
  });
  assert.equal(cleanLogoutRes.status, 204);
});

