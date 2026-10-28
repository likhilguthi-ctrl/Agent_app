import assert from "node:assert/strict";
import { test } from "node:test";

const BASE_URL = "http://localhost:3000";

test("Mobile OTP Auth - Unknown mobile returns generic 202 Accepted (Anti-Enumeration)", async () => {
  const response = await fetch(`${BASE_URL}/backend-api/api/v1/auth/otp/request`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      mobile_number: "+442079460999",
      purpose: "login",
    }),
  });

  assert.equal(response.status, 202, "Anti-enumeration requires 202 Accepted for unknown mobile numbers");
  const data = await response.json();
  assert.equal(data.status, "accepted");
  assert.ok(data.challenge_id, "Response must include opaque challenge_id");
  assert.ok(data.cooldown_seconds >= 60, "Response must include cooldown seconds");
});

test("Mobile OTP Auth - Super Admin mobile OTP login lifecycle", async () => {
  const SUPER_ADMIN_MOBILE = "+15550000001";
  const DEV_OTP = "123456";

  // 1. Request OTP
  const reqRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/otp/request`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      mobile_number: SUPER_ADMIN_MOBILE,
      purpose: "login",
    }),
  });

  assert.equal(reqRes.status, 202, "OTP request should return 202 Accepted");
  const reqData = await reqRes.json();
  assert.ok(reqData.challenge_id, "Must return challenge_id");
  const challengeId = reqData.challenge_id;

  // 2. Test Invalid OTP returns 401
  const invalidRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/otp/verify`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      mobile_number: SUPER_ADMIN_MOBILE,
      otp: "999999",
      challenge_id: challengeId,
      purpose: "login",
    }),
  });

  assert.equal(invalidRes.status, 401, "Invalid OTP must return 401 Unauthorized");

  // 3. Test Valid OTP verification
  const verifyRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/otp/verify`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      mobile_number: SUPER_ADMIN_MOBILE,
      otp: DEV_OTP,
      challenge_id: challengeId,
      purpose: "login",
    }),
  });

  assert.equal(verifyRes.status, 200, "Valid OTP must return 200 OK");
  const verifyData = await verifyRes.json();
  assert.equal(verifyData.role, "SUPER_ADMIN", "Super Admin role must match");
  assert.ok(verifyData.csrf_token, "Must return session CSRF token");

  const setCookie = verifyRes.headers.get("set-cookie");
  assert.ok(setCookie, "Must set session cookies");
  assert.match(setCookie, /community_session=/, "Must include community_session cookie");

  const cookies = setCookie
    .split(",")
    .map((c) => c.split(";")[0].trim())
    .join("; ");

  // 4. Verify authenticated session via /me
  const meRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/me`, {
    headers: { Cookie: cookies },
  });
  assert.equal(meRes.status, 200, "Authenticated /me must return 200");
  const meData = await meRes.json();
  assert.equal(meData.role, "SUPER_ADMIN");

  // 5. Logout
  const logoutRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/logout`, {
    method: "POST",
    headers: {
      Cookie: cookies,
      Origin: BASE_URL,
      "X-CSRF-Token": meData.csrf_token,
    },
  });
  assert.equal(logoutRes.status, 204, "Logout must return 204 No Content");
});

test("Mobile OTP Registration - Inline verification returns opaque verification_token", async () => {
  const AGENT_REG_MOBILE = "+9198" + Math.floor(10000000 + Math.random() * 90000000);
  const DEV_OTP = "123456";

  // 1. Request OTP for agent registration
  const reqRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/otp/request`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      mobile_number: AGENT_REG_MOBILE,
      purpose: "agent_registration",
    }),
  });

  assert.equal(reqRes.status, 202, "Registration OTP request should return 202 Accepted");
  const reqData = await reqRes.json();
  assert.ok(reqData.challenge_id, "Must return challenge_id");

  // 2. Verify OTP for agent registration
  const verifyRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/otp/verify`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      mobile_number: AGENT_REG_MOBILE,
      otp: DEV_OTP,
      challenge_id: reqData.challenge_id,
      purpose: "agent_registration",
    }),
  });

  assert.equal(verifyRes.status, 200, "Registration OTP verification must return 200 OK");
  const verifyData = await verifyRes.json();
  assert.equal(verifyData.status, "verified");
  assert.equal(verifyData.purpose, "agent_registration");
  assert.ok(verifyData.verification_token, "Must issue server-verified opaque verification_token");
  assert.ok(verifyData.verification_token.length >= 32, "verification_token must be cryptographically secure");
});

test("Mobile OTP Registration - Subscriber registration returns verified token", async () => {
  const USER_REG_MOBILE = "+9197" + Math.floor(10000000 + Math.random() * 90000000);
  const DEV_OTP = "123456";

  const reqRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/otp/request`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      mobile_number: USER_REG_MOBILE,
      purpose: "user_registration",
    }),
  });

  assert.equal(reqRes.status, 202);
  const reqData = await reqRes.json();

  const verifyRes = await fetch(`${BASE_URL}/backend-api/api/v1/auth/otp/verify`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: BASE_URL,
    },
    body: JSON.stringify({
      mobile_number: USER_REG_MOBILE,
      otp: DEV_OTP,
      challenge_id: reqData.challenge_id,
      purpose: "user_registration",
    }),
  });

  assert.equal(verifyRes.status, 200);
  const verifyData = await verifyRes.json();
  assert.equal(verifyData.status, "verified");
  assert.equal(verifyData.purpose, "user_registration");
  assert.ok(verifyData.verification_token);
});

