import assert from "node:assert/strict";
import { test } from "node:test";

const BASE_URL = "http://localhost:3000";

test("Phase 5 - Agent, User Registration, Storage & Commission End-to-End Suite", async () => {
  // 1. Super Admin Login
  const superLoginRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/login`, {
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

  assert.equal(superLoginRes.status, 200, "Super Admin login must return 200");
  const superData = await superLoginRes.json();
  assert.equal(superData.role, "SUPER_ADMIN");
  const superCsrf = superData.csrf_token;
  assert.ok(superCsrf, "CSRF token must be present");

  const superCookies = superLoginRes.headers
    .get("set-cookie")
    .split(",")
    .map((c) => c.split(";")[0].trim())
    .join("; ");

  // 2. Multi-Admin across communities endpoint
  const multiAdminRes = await fetch(`${BASE_URL}/backend-api/api/v1/platform/administrators`, {
    headers: { Cookie: superCookies },
  });
  assert.equal(multiAdminRes.status, 200, "List all administrators across communities must return 200");
  const multiAdminData = await multiAdminRes.json();
  assert.ok(Array.isArray(multiAdminData.items), "Items must be an array");

  // 3. List communities to get target community ID
  const commRes = await fetch(`${BASE_URL}/backend-api/api/v1/platform/communities`, {
    headers: { Cookie: superCookies },
  });
  assert.equal(commRes.status, 200);
  const commData = await commRes.json();
  assert.ok(commData.items.length >= 1, "Must have at least one community");
  const targetCommunity = commData.items[0];
  const communityId = targetCommunity.id;

  // 4. Update community photo policy to REQUIRED
  const policyRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}/photo-policy`,
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        Origin: BASE_URL,
        "X-CSRF-Token": superCsrf,
        Cookie: superCookies,
      },
      body: JSON.stringify({
        photo_requirement: "REQUIRED",
      }),
    }
  );
  assert.equal(policyRes.status, 200, "Updating photo policy to REQUIRED must succeed");
  const policyData = await policyRes.json();
  assert.equal(policyData.photo_requirement, "REQUIRED");

  // 5. Update community default commission rate
  const commUpdateRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}/commission`,
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        Origin: BASE_URL,
        "X-CSRF-Token": superCsrf,
        Cookie: superCookies,
      },
      body: JSON.stringify({
        default_commission_rate: 12.50,
      }),
    }
  );
  assert.equal(commUpdateRes.status, 200, "Updating default commission must succeed");
  const commUpdateData = await commUpdateRes.json();
  assert.equal(Number(commUpdateData.default_commission_rate), 12.50);

  // 6. View Commission Audit Logs
  const auditRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}/commission/audit-logs`,
    {
      headers: { Cookie: superCookies },
    }
  );
  assert.equal(auditRes.status, 200, "Fetching commission audit logs must succeed");
  const auditData = await auditRes.json();
  assert.ok(Array.isArray(auditData.items), "Audit logs items must be an array");
  assert.ok(auditData.items.length >= 1, "Must contain at least 1 audit log");
  assert.equal(auditData.items[0].scope, "community");

  // 7. Register an Agent via Super Admin
  const samplePngBase64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

  async function uploadImage(category, cookies, csrf) {
    const res = await fetch(`${BASE_URL}/backend-api/api/v1/storage/upload`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: BASE_URL,
        "X-CSRF-Token": csrf,
        Cookie: cookies,
      },
      body: JSON.stringify({
        category,
        image_base64: samplePngBase64,
      }),
    });
    assert.equal(res.status, 201, `Upload for ${category} must return 201`);
    const data = await res.json();
    return data.object_key;
  }

  const uniqueAgentUser = `agent_p5_${Date.now().toString().slice(-6)}`;
  const photoKey = await uploadImage("agent-photo", superCookies, superCsrf);
  const aadhaarKey = await uploadImage("aadhaar-document", superCookies, superCsrf);
  const panKey = await uploadImage("pan-document", superCookies, superCsrf);

  const agentCreateRes = await fetch(
    `${BASE_URL}/backend-api/api/v1/platform/communities/${communityId}/agents`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: BASE_URL,
        "X-CSRF-Token": superCsrf,
        Cookie: superCookies,
      },
      body: JSON.stringify({
        first_name: "Rahul",
        last_name: "Sharma",
        username: uniqueAgentUser,
        password: "AgentSecurePass1!",
        mobile_e164: `+9198${Date.now().toString().slice(-8)}`,
        email: "rahul@example.com",
        address: "123 MG Road, Bengaluru",
        profile_photo_key: photoKey,
        aadhaar_number: "987654321098",
        aadhaar_document_key: aadhaarKey,
        pan_number: "ABCDE9876K",
        pan_document_key: panKey,
        commission_rate_override: 15.00,
        status: "active",
      }),
    }
  );

  assert.equal(agentCreateRes.status, 201, "Agent registration must return 201");
  const agentData = await agentCreateRes.json();
  assert.equal(agentData.username, uniqueAgentUser);
  assert.ok(agentData.ref_id, "Agent must have an 8-char ref_id");
  assert.equal(agentData.ref_id.length, 8);
  assert.equal(agentData.aadhaar_number_masked, "XXXX-XXXX-1098", "Aadhaar must be masked in response");
  assert.equal(agentData.pan_number_masked, "XXXXX9876K", "PAN must be masked in response");
  assert.equal(Number(agentData.effective_commission_rate), 15.00);

  // 8. Agent Login
  const agentLoginRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      username: uniqueAgentUser,
      password: "AgentSecurePass1!",
    }),
  });

  assert.equal(agentLoginRes.status, 200, "Agent login must return 200");
  const agentSession = await agentLoginRes.json();
  assert.equal(agentSession.role, "AGENT");
  const agentCsrf = agentSession.csrf_token;
  const agentCookies = agentLoginRes.headers
    .get("set-cookie")
    .split(",")
    .map((c) => c.split(";")[0].trim())
    .join("; ");

  // 9. Agent checks community policy
  const agentPolicyRes = await fetch(`${BASE_URL}/backend-api/api/v1/agent/policy`, {
    headers: { Cookie: agentCookies },
  });
  assert.equal(agentPolicyRes.status, 200);
  const agentPolicy = await agentPolicyRes.json();
  assert.equal(agentPolicy.photo_requirement, "REQUIRED");

  // 10. Agent registering subscriber without photo must be rejected (Policy is REQUIRED)
  const failSubRes = await fetch(`${BASE_URL}/backend-api/api/v1/agent/users`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
      "X-CSRF-Token": agentCsrf,
      Cookie: agentCookies,
    },
    body: JSON.stringify({
      full_name: "Vikram Malhotra",
      mobile_e164: `+9191${Date.now().toString().slice(-8)}`,
      photo_key: null,
    }),
  });
  assert.equal(failSubRes.status, 422, "Subscriber without photo must be rejected when REQUIRED");

  // 11. Agent registering subscriber WITH photo must succeed
  const userPhotoKey = await uploadImage("user-photo", agentCookies, agentCsrf);
  const subRes = await fetch(`${BASE_URL}/backend-api/api/v1/agent/users`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
      "X-CSRF-Token": agentCsrf,
      Cookie: agentCookies,
    },
    body: JSON.stringify({
      full_name: "Vikram Malhotra",
      mobile_e164: `+9191${Date.now().toString().slice(-8)}`,
      email: "vikram@example.com",
      address: "Indiranagar, Bengaluru",
      photo_key: userPhotoKey,
      package_plan: "Premium",
      billing_cycle: "quarterly",
    }),
  });

  assert.equal(subRes.status, 201, "Subscriber registration with photo must return 201");
  const subData = await subRes.json();
  assert.equal(subData.full_name, "Vikram Malhotra");
  assert.equal(subData.photo_key, userPhotoKey);

  // 12. Agent lists subscribers
  const agentSubsRes = await fetch(`${BASE_URL}/backend-api/api/v1/agent/users`, {
    headers: { Cookie: agentCookies },
  });
  assert.equal(agentSubsRes.status, 200);
  const agentSubsData = await agentSubsRes.json();
  assert.ok(agentSubsData.items.length >= 1, "Agent must see registered subscriber");
  assert.equal(agentSubsData.items[0].full_name, "Vikram Malhotra");
});

