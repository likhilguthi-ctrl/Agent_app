import assert from "node:assert/strict";
import { test } from "node:test";

import {
  generateNotificationIntentKey,
  assertBrowserSafeProvider,
  mapNotificationStatusToPresentation,
  validateNotificationRecipient,
  buildSmsPayload,
  buildWhatsAppTemplatePayload,
  getWhatsAppSupportUrl,
  InsecureBrowserDeliveryBlockedError,
  getNotificationStatus,
  pollNotificationStatus,
} from "../src/lib/notifications.ts";

test("Notification Client — assertBrowserSafeProvider enforces fail-closed boundary for service API keys", () => {
  // Provider requires private X-API-Key (service-to-service)
  assert.throws(
    () => {
      assertBrowserSafeProvider("service_api_key");
    },
    (err) => {
      assert.ok(err instanceof InsecureBrowserDeliveryBlockedError);
      assert.match(err.message, /service X-API-Key/);
      return true;
    },
    "Must throw InsecureBrowserDeliveryBlockedError when provider requires service API keys"
  );

  // Future hypothetical browser token auth does not throw
  assert.doesNotThrow(() => {
    assertBrowserSafeProvider("browser_token");
  });
});

test("Notification Client — validateNotificationRecipient validates and normalizes channels", () => {
  // SMS normalization
  const sms1 = validateNotificationRecipient("SMS", "+919876543210");
  assert.equal(sms1.valid, true);
  assert.equal(sms1.normalized, "+919876543210");

  const sms2 = validateNotificationRecipient("SMS", "919876543210");
  assert.equal(sms2.valid, true);
  assert.equal(sms2.normalized, "+919876543210");

  const smsInvalid = validateNotificationRecipient("SMS", "invalid-phone");
  assert.equal(smsInvalid.valid, false);
  assert.match(smsInvalid.error, /E\.164/);

  // WhatsApp normalization
  const wa1 = validateNotificationRecipient("WHATSAPP", " +91 98765 43210 ");
  assert.equal(wa1.valid, true);
  assert.equal(wa1.normalized, "+919876543210");

  // Email validation
  const email1 = validateNotificationRecipient("EMAIL", "user@example.com");
  assert.equal(email1.valid, true);
  assert.equal(email1.normalized, "user@example.com");

  const emailInvalid = validateNotificationRecipient("EMAIL", "not-an-email");
  assert.equal(emailInvalid.valid, false);
  assert.match(emailInvalid.error, /Email/);
});

test("Notification Client — buildSmsPayload constructs valid FRONTEND_INTEGRATION.md payload", () => {
  const payload = buildSmsPayload(
    "LOGIN_OTP",
    "+919876543210",
    "Your verification code is 123456.",
    { flow: "login" }
  );

  assert.equal(payload.channel, "SMS");
  assert.equal(payload.messageType, "LOGIN_OTP");
  assert.equal(payload.recipient, "+919876543210");
  assert.equal(payload.content.type, "TEXT");
  assert.equal(payload.content.text, "Your verification code is 123456.");
  assert.deepEqual(payload.metadata, { flow: "login" });

  // Empty text rejection
  assert.throws(() => {
    buildSmsPayload("LOGIN_OTP", "+919876543210", "   ");
  }, /empty/);
});

test("Notification Client — buildWhatsAppTemplatePayload constructs valid template payload", () => {
  const payload = buildWhatsAppTemplatePayload(
    "PAYMENT_OTP",
    "+919876543210",
    "PAYMENT_OTP",
    { otp: "123456" },
    "en",
    { referenceId: "payment-123" }
  );

  assert.equal(payload.channel, "WHATSAPP");
  assert.equal(payload.messageType, "PAYMENT_OTP");
  assert.equal(payload.recipient, "+919876543210");
  assert.equal(payload.content.type, "TEMPLATE");
  assert.equal(payload.content.templateCode, "PAYMENT_OTP");
  assert.equal(payload.content.locale, "en");
  assert.deepEqual(payload.content.variables, { otp: "123456" });
  assert.deepEqual(payload.metadata, { referenceId: "payment-123" });
});

test("Notification Client — getWhatsAppSupportUrl creates valid wa.me deep links", () => {
  const url1 = getWhatsAppSupportUrl();
  assert.match(url1, /^https:\/\/wa\.me\/911234567890\?text=/);

  const url2 = getWhatsAppSupportUrl({
    phone: "+91 999 888 7777",
    message: "Need assistance with membership",
  });
  assert.equal(
    url2,
    "https://wa.me/919998887777?text=Need%20assistance%20with%20membership"
  );
});

test("Notification Client — generateNotificationIntentKey generates bounded, sanitized keys", () => {
  const key1 = generateNotificationIntentKey("login", "+919876543210");
  assert.equal(key1, "notif_login_919876543210");
  assert.ok(key1.length <= 200, "Key length must be <= 200 characters");

  // Tests special characters sanitization
  const key2 = generateNotificationIntentKey("payment;DROP TABLE", "user@example.com!#$");
  assert.equal(key2, "notif_paymentDROPTABLE_userexamplecom");

  // Tests stability
  const key3 = generateNotificationIntentKey("login", "+919876543210");
  assert.equal(key1, key3, "Same inputs must produce identical idempotency key");
});

test("Notification Client — mapNotificationStatusToPresentation accurately maps lifecycle states", () => {
  // QUEUED / 202 is pending, NOT delivered
  const queued = mapNotificationStatusToPresentation("QUEUED");
  assert.equal(queued.category, "pending");
  assert.equal(queued.terminal, false);
  assert.equal(queued.label, "Pending");

  // PROVIDER_ACCEPTED is pending provider delivery, NOT delivered
  const provAccepted = mapNotificationStatusToPresentation("PROVIDER_ACCEPTED");
  assert.equal(provAccepted.category, "pending");
  assert.equal(provAccepted.terminal, false);
  assert.equal(provAccepted.label, "Sent to Provider");

  // DELIVERED is terminal success
  const delivered = mapNotificationStatusToPresentation("DELIVERED");
  assert.equal(delivered.category, "success");
  assert.equal(delivered.terminal, true);
  assert.equal(delivered.label, "Delivered");

  // FAILED is terminal error
  const failed = mapNotificationStatusToPresentation("FAILED");
  assert.equal(failed.category, "error");
  assert.equal(failed.terminal, true);

  // UNKNOWN_OUTCOME is non-blind retry warning
  const unknown = mapNotificationStatusToPresentation("UNKNOWN_OUTCOME");
  assert.equal(unknown.category, "warning");
  assert.equal(unknown.terminal, true);
  assert.match(unknown.description, /Avoid blind retries/);
});

test("Notification Security — Browser environment contains no private service keys or secrets", () => {
  // Verify no NEXT_PUBLIC_* notification secrets exist in runtime environment
  const nextPublicKeys = Object.keys(process.env).filter((k) =>
    k.startsWith("NEXT_PUBLIC_")
  );

  for (const k of nextPublicKeys) {
    assert.ok(
      !k.includes("API_KEY") && !k.includes("SECRET") && !k.includes("TOKEN"),
      `NEXT_PUBLIC environment variable must not expose sensitive keys: ${k}`
    );
  }

  // Explicit check on notification-specific variables
  assert.equal(
    process.env.NEXT_PUBLIC_NOTIFICATION_API_KEY,
    undefined,
    "NEXT_PUBLIC_NOTIFICATION_API_KEY must not exist"
  );
  assert.equal(
    process.env.NEXT_PUBLIC_NOTIFICATION_WEBHOOK_SECRET,
    undefined,
    "NEXT_PUBLIC_NOTIFICATION_WEBHOOK_SECRET must not exist"
  );
});

test("Notification Client — getNotificationStatus rejects empty messageId", async () => {
  await assert.rejects(
    async () => {
      await getNotificationStatus("   ");
    },
    /Message ID is required/
  );
});

