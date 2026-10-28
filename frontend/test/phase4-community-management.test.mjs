import assert from "node:assert/strict";
import { test } from "node:test";

const BASE_URL = "http://localhost:3000";

test("Phase 4 - Community Management End-to-End Suite", async () => {
  // 1. Super Admin Login
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

  assert.equal(loginRes.status, 200, "Super Admin login must return 200");
  const loginData = await loginRes.json();
  assert.equal(loginData.role, "SUPER_ADMIN");
  const csrfToken = loginData.csrf_token;
  assert.ok(csrfToken, "CSRF token must be present");

  const cookieHeader = loginRes.headers.get("set-cookie");
  assert.ok(cookieHeader, "Cookie header must be present");
  const cookies = cookieHeader
    .split(",")
    .map((c) => c.split(";")[0].trim())
    .join("; ");

  // 2. List initial communities through proxy
  const listRes = await fetch(`${BASE_URL}/backend-api/api/v1/platform/communities`, {
    headers: { Cookie: cookies },
  });
  assert.equal(listRes.status, 200, "List communities must return 200");
  const listData = await listRes.json();
  assert.ok(Array.isArray(listData.items), "Items must be an array");
  assert.ok(typeof listData.total === "number", "Total must be a number");

  // 3. Create a unique community
  const uniqueSlug = `test-comm-${Date.now().toString().slice(-6)}`;
  const createRes = await fetch(`${BASE_URL}/backend-api/api/v1/platform/communities`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
      "X-CSRF-Token": csrfToken,
      Cookie: cookies,
    },
    body: JSON.stringify({
      slug: uniqueSlug,
      domain_hostname: `${uniqueSlug}.example.local`,
      status: "active",
    }),
  });

  assert.equal(createRes.status, 201, "Create community must return 201 Created");
  const createdCommunity = await createRes.json();
  assert.equal(createdCommunity.slug, uniqueSlug);
  assert.equal(createdCommunity.status, "active");
  assert.ok(createdCommunity.id, "Created community must have an id");
  const communityId = createdCommunity.id;

  // 4. View individual community details
  const detailRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}`,
    {
      headers: { Cookie: cookies },
    }
  );
  assert.equal(detailRes.status, 200, "Get community details must return 200");
  const detailData = await detailRes.json();
  assert.equal(detailData.id, communityId);
  assert.equal(detailData.slug, uniqueSlug);
  assert.ok(detailData.registration_policy, "Registration policy foundation must be present");

  // 5. Update community status (Deactivate)
  const deactivateRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}`,
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        Origin: BASE_URL,
        "X-CSRF-Token": csrfToken,
        Cookie: cookies,
      },
      body: JSON.stringify({ status: "inactive" }),
    }
  );
  assert.equal(deactivateRes.status, 200, "Deactivate community must return 200");
  const deactivated = await deactivateRes.json();
  assert.equal(deactivated.status, "inactive");

  // Reactivate
  const reactivateRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}`,
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        Origin: BASE_URL,
        "X-CSRF-Token": csrfToken,
        Cookie: cookies,
      },
      body: JSON.stringify({ status: "active" }),
    }
  );
  assert.equal(reactivateRes.status, 200, "Reactivate community must return 200");
  const reactivated = await reactivateRes.json();
  assert.equal(reactivated.status, "active");

  // 6. Create community administrator
  const uniqueAdminUsername = `lead_${Date.now().toString().slice(-6)}`;
  const uniquePhone = `+1555${Math.floor(1000000 + Math.random() * 9000000)}`;
  const createAdminRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}/administrators`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: BASE_URL,
        "X-CSRF-Token": csrfToken,
        Cookie: cookies,
      },
      body: JSON.stringify({
        username: uniqueAdminUsername,
        password: "AdminSecurePassword123!",
        mobile_e164: uniquePhone,
        status: "active",
      }),
    }
  );
  assert.equal(createAdminRes.status, 201, "Create administrator must return 201 Created");
  const createdAdmin = await createAdminRes.json();
  assert.equal(createdAdmin.username, uniqueAdminUsername);
  assert.equal(createdAdmin.role, "ADMIN");
  assert.equal(createdAdmin.mobile_e164, uniquePhone);
  const adminUserId = createdAdmin.user_id;

  // 7. List community administrators
  const listAdminsRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}/administrators`,
    {
      headers: { Cookie: cookies },
    }
  );
  assert.equal(listAdminsRes.status, 200, "List administrators must return 200");
  const adminsList = await listAdminsRes.json();
  assert.ok(adminsList.total >= 1);
  assert.ok(adminsList.items.some((a) => a.username === uniqueAdminUsername));

  // 8. Update administrator status (Deactivate)
  const patchAdminRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}/administrators/${adminUserId}`,
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        Origin: BASE_URL,
        "X-CSRF-Token": csrfToken,
        Cookie: cookies,
      },
      body: JSON.stringify({ status: "inactive" }),
    }
  );
  assert.equal(patchAdminRes.status, 200, "Deactivate administrator must return 200");
  const deactivatedAdmin = await patchAdminRes.json();
  assert.equal(deactivatedAdmin.status, "inactive");

  // 9. Community Admin login and verify access to platform communities is FORBIDDEN
  const adminLoginRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/login`, {
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
  assert.equal(adminLoginRes.status, 200);
  const adminCookies = adminLoginRes.headers
    .get("set-cookie")
    .split(",")
    .map((c) => c.split(";")[0].trim())
    .join("; ");

  const adminPlatformAccessRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities`,
    {
      headers: { Cookie: adminCookies },
    }
  );
  assert.ok(
    [401, 403].includes(adminPlatformAccessRes.status),
    "Community Admin must receive 401 or 403 Forbidden on platform communities"
  );

  // 10. Logout Super Admin
  const logoutRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/logout`, {
    method: "POST",
    headers: {
      Cookie: cookies,
      Origin: BASE_URL,
      "X-CSRF-Token": csrfToken,
    },
  });
  assert.equal(logoutRes.status, 204);
});

