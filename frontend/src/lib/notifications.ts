/**
 * Notification Service — Centralized Frontend Client & Type Contracts
 *
 * Implements the contract defined in docs/implementation/FRONTEND_INTEGRATION.md.
 * Base URL: https://handorg.duckdns.org/notifications
 * Endpoint: POST /api/v1/messages
 *
 * SECURITY BOUNDARY & ARCHITECTURAL DIRECTIVE:
 * The external Notification Microservice (POST /api/v1/messages) is strictly a
 * service-to-service API authenticated via private `X-API-Key`.
 * Browser-safe direct access is NOT supported by the provider.
 * Under no circumstances should `X-API-Key` or provider secrets be embedded in
 * frontend code, bundles, or NEXT_PUBLIC_* environment variables.
 *
 * All browser notification triggers (e.g. login OTP, registration verification)
 * are centralized through this shared client and mediated by the secure Next.js
 * reverse proxy (/backend-api/), ensuring that private service keys remain solely
 * on the backend server while enforcing PostgreSQL anti-abuse and rate limiting.
 */

export type NotificationChannel = "SMS" | "WHATSAPP" | "EMAIL";

export type NotificationMessageType =
  | "LOGIN_OTP"
  | "PAYMENT_OTP"
  | "PASSWORD_RESET_OTP"
  | "ACCOUNT_UPDATE"
  | "URGENT_SECURITY_ALERT"
  | "PAYMENT_RECEIPT";

export type NotificationPriority = "P0" | "P1" | "P2";

export type NotificationStatus =
  | "QUEUED"
  | "PROCESSING"
  | "PROVIDER_ACCEPTED"
  | "DELIVERED"
  | "READ"
  | "RETRY_PENDING"
  | "MOCK_SENT"
  | "FAILED"
  | "EXPIRED"
  | "UNKNOWN_OUTCOME";

export interface NotificationTextContent {
  type: "TEXT";
  text: string;
}

export interface NotificationTemplateContent {
  type: "TEMPLATE";
  templateCode: string;
  locale?: string;
  variables?: Record<string, string>;
}

export type NotificationContent =
  | NotificationTextContent
  | NotificationTemplateContent;

export interface NotificationSubmissionRequest {
  channel: NotificationChannel;
  messageType: NotificationMessageType;
  recipient: string;
  content: NotificationContent;
  metadata?: Record<string, string | number | boolean>;
  expiresAt?: string;
}

export interface NotificationSubmissionResponse {
  messageId: string;
  status: NotificationStatus;
  priority: NotificationPriority;
  channel: NotificationChannel;
  statusUrl?: string;
}

export interface NotificationStatusResponse {
  messageId: string;
  channel: NotificationChannel;
  status: NotificationStatus;
  priority: NotificationPriority;
  createdAt: string;
  updatedAt: string;
  providerMessageId?: string | null;
  providerAcceptedAt?: string | null;
  deliveredAt?: string | null;
  failureCode?: string | null;
  failureDetails?: string | null;
}

export class InsecureBrowserDeliveryBlockedError extends Error {
  constructor(message?: string) {
    super(
      message ||
        "Direct browser-to-provider notification delivery is blocked because the provider requires a private service X-API-Key. All notification dispatches must flow through the secure backend proxy."
    );
    this.name = "InsecureBrowserDeliveryBlockedError";
  }
}

/**
 * Validates whether an external notification provider API supports safe browser-side access.
 * If the provider requires service API keys (X-API-Key), direct browser dispatch fails closed.
 */
export function assertBrowserSafeProvider(
  providerAuthType: "service_api_key" | "browser_token"
): void {
  if (providerAuthType === "service_api_key") {
    throw new InsecureBrowserDeliveryBlockedError();
  }
}

const E164_REGEX = /^\+[1-9]\d{7,14}$/;
const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * Validates and normalizes recipient address by channel.
 */
export function validateNotificationRecipient(
  channel: NotificationChannel,
  recipient: string
): { valid: boolean; normalized: string; error?: string } {
  const trimmed = recipient.trim();
  if (!trimmed) {
    return { valid: false, normalized: "", error: "Recipient cannot be empty." };
  }

  if (channel === "SMS" || channel === "WHATSAPP") {
    let normalized = trimmed;
    if (!normalized.startsWith("+")) {
      normalized = `+${normalized.replace(/\D/g, "")}`;
    } else {
      normalized = `+${normalized.slice(1).replace(/\D/g, "")}`;
    }

    if (!E164_REGEX.test(normalized)) {
      return {
        valid: false,
        normalized,
        error: "Phone number must be in E.164 format (e.g. +919876543210).",
      };
    }
    return { valid: true, normalized };
  }

  if (channel === "EMAIL") {
    const normalized = trimmed.toLowerCase();
    if (!EMAIL_REGEX.test(normalized)) {
      return {
        valid: false,
        normalized,
        error: "Email address format is invalid.",
      };
    }
    return { valid: true, normalized };
  }

  return { valid: false, normalized: trimmed, error: `Unsupported channel: ${channel}` };
}

/**
 * Constructs a typed SMS payload following FRONTEND_INTEGRATION.md contract.
 */
export function buildSmsPayload(
  messageType: NotificationMessageType,
  recipient: string,
  text: string,
  metadata?: Record<string, string | number | boolean>,
  expiresAt?: string
): NotificationSubmissionRequest {
  const validation = validateNotificationRecipient("SMS", recipient);
  if (!validation.valid) {
    throw new Error(validation.error || "Invalid SMS recipient.");
  }
  if (!text.trim()) {
    throw new Error("SMS message text cannot be empty.");
  }
  if (text.length > 4000) {
    throw new Error("SMS message text exceeds maximum length of 4000 characters.");
  }

  return {
    channel: "SMS",
    messageType,
    recipient: validation.normalized,
    content: {
      type: "TEXT",
      text: text.trim(),
    },
    ...(metadata ? { metadata } : {}),
    ...(expiresAt ? { expiresAt } : {}),
  };
}

/**
 * Constructs a typed WhatsApp Template payload following FRONTEND_INTEGRATION.md contract.
 */
export function buildWhatsAppTemplatePayload(
  messageType: NotificationMessageType,
  recipient: string,
  templateCode: string,
  variables: Record<string, string>,
  locale: string = "en",
  metadata?: Record<string, string | number | boolean>,
  expiresAt?: string
): NotificationSubmissionRequest {
  const validation = validateNotificationRecipient("WHATSAPP", recipient);
  if (!validation.valid) {
    throw new Error(validation.error || "Invalid WhatsApp recipient.");
  }
  if (!templateCode.trim()) {
    throw new Error("WhatsApp templateCode cannot be empty.");
  }

  return {
    channel: "WHATSAPP",
    messageType,
    recipient: validation.normalized,
    content: {
      type: "TEMPLATE",
      templateCode: templateCode.trim(),
      locale: locale.trim(),
      variables,
    },
    ...(metadata ? { metadata } : {}),
    ...(expiresAt ? { expiresAt } : {}),
  };
}

/**
 * Generates a stable, bounded idempotency key for a specific notification intent.
 * Format: max 200 characters, alphanumeric with hyphens, strictly no sensitive data.
 */
export function generateNotificationIntentKey(
  purpose: string,
  discriminator: string
): string {
  const safePurpose = purpose.replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 40);
  const safeDisc = discriminator.replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 100);
  return `notif_${safePurpose}_${safeDisc}`.slice(0, 200);
}

/**
 * Generates direct WhatsApp chat URL for customer support deep links.
 */
export function getWhatsAppSupportUrl(params?: {
  phone?: string;
  message?: string;
}): string {
  const phone = (params?.phone || "911234567890").replace(/\D/g, "");
  const text = params?.message || "Hi, I need help with my Sahayak Partners account.";
  return `https://wa.me/${phone}?text=${encodeURIComponent(text)}`;
}

/**
 * Client presentation mapping for notification lifecycle statuses.
 * Distinguishes queued, provider-accepted, delivered, failed, and unknown states.
 */
export function mapNotificationStatusToPresentation(status: NotificationStatus): {
  label: string;
  category: "pending" | "sending" | "success" | "warning" | "error";
  description: string;
  terminal: boolean;
} {
  switch (status) {
    case "QUEUED":
      return {
        label: "Pending",
        category: "pending",
        description: "Accepted by service; awaiting dispatch worker.",
        terminal: false,
      };
    case "PROCESSING":
      return {
        label: "Sending",
        category: "sending",
        description: "Delivery in progress to provider gateway.",
        terminal: false,
      };
    case "PROVIDER_ACCEPTED":
      return {
        label: "Sent to Provider",
        category: "pending",
        description: "Provider gateway accepted the message; delivery pending.",
        terminal: false,
      };
    case "DELIVERED":
      return {
        label: "Delivered",
        category: "success",
        description: "Recipient device confirmed message delivery.",
        terminal: true,
      };
    case "READ":
      return {
        label: "Read",
        category: "success",
        description: "Recipient opened/read the message.",
        terminal: true,
      };
    case "MOCK_SENT":
      return {
        label: "Mock Sent (Dev)",
        category: "success",
        description: "Local mock adapter recorded message (development only).",
        terminal: true,
      };
    case "RETRY_PENDING":
      return {
        label: "Retrying",
        category: "warning",
        description: "Temporary provider error; retry attempt scheduled.",
        terminal: false,
      };
    case "EXPIRED":
      return {
        label: "Expired",
        category: "error",
        description: "Message validity window elapsed before delivery.",
        terminal: true,
      };
    case "FAILED":
      return {
        label: "Delivery Failed",
        category: "error",
        description: "Permanent provider delivery failure.",
        terminal: true,
      };
    case "UNKNOWN_OUTCOME":
    default:
      return {
        label: "Unknown Outcome",
        category: "warning",
        description: "Delivery status could not be confirmed. Avoid blind retries.",
        terminal: true,
      };
  }
}

export interface OtpNotificationRequestParams {
  channel?: NotificationChannel;
  mobileNumber: string;
  purpose?: "login" | "admin_registration" | "agent_registration" | "user_registration" | "whatsapp_verification";
  communityId?: string;
  idempotencyKey?: string;
}

export interface OtpNotificationRequestResult {
  accepted: boolean;
  challengeId: string;
  cooldownSeconds: number;
  status: NotificationStatus;
}

export interface OtpNotificationVerifyParams {
  mobileNumber: string;
  otp: string;
  challengeId: string;
  purpose?: "login" | "admin_registration" | "agent_registration" | "user_registration" | "whatsapp_verification";
  communityId?: string;
}

export interface OtpNotificationVerifyResult {
  success: boolean;
  role?: string;
  userId?: string;
  verificationToken?: string;
  rawResponse?: Record<string, unknown>;
}

/**
 * Centralized method to request an OTP notification.
 * Dispatches via backend proxy to enforce anti-enumeration, rate limiting, and private credential isolation.
 */
export async function requestOtpNotification(
  params: OtpNotificationRequestParams
): Promise<OtpNotificationRequestResult> {
  const validation = validateNotificationRecipient("SMS", params.mobileNumber);
  if (!validation.valid) {
    throw new Error(validation.error || "Invalid mobile number format.");
  }

  const { fetchBackendApi } = await import("./backend-api");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };

  const idemKey =
    params.idempotencyKey ||
    generateNotificationIntentKey(params.purpose || "login", validation.normalized);
  headers["Idempotency-Key"] = idemKey;

  const response = await fetchBackendApi("api/v1/auth/otp/request", {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: JSON.stringify({
      mobile_number: validation.normalized,
      purpose: params.purpose || "login",
      ...(params.communityId ? { community_id: params.communityId } : {}),
    }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 429) {
      const retryAfter = errorData?.detail?.retry_after || 60;
      throw new Error(`Rate limit exceeded. Please wait ${retryAfter} seconds before requesting another code.`);
    }
    throw new Error(errorData?.detail?.message || "Failed to request verification code.");
  }

  const data = (await response.json()) as {
    status?: string;
    challenge_id: string;
    cooldown_seconds?: number;
  };

  return {
    accepted: true,
    challengeId: data.challenge_id,
    cooldownSeconds: data.cooldown_seconds ?? 60,
    status: "QUEUED",
  };
}

/**
 * Centralized method to verify an OTP notification challenge.
 */
export async function verifyOtpNotification(
  params: OtpNotificationVerifyParams
): Promise<OtpNotificationVerifyResult> {
  const validation = validateNotificationRecipient("SMS", params.mobileNumber);
  if (!validation.valid) {
    throw new Error(validation.error || "Invalid mobile number format.");
  }
  const cleanOtp = params.otp.trim();
  if (!cleanOtp) {
    throw new Error("Verification code is required.");
  }
  if (!params.challengeId) {
    throw new Error("Challenge ID is required for verification.");
  }

  const { fetchBackendApi } = await import("./backend-api");
  const response = await fetchBackendApi("api/v1/auth/otp/verify", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      mobile_number: validation.normalized,
      otp: cleanOtp,
      challenge_id: params.challengeId,
      purpose: params.purpose || "login",
      ...(params.communityId ? { community_id: params.communityId } : {}),
    }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 401) {
      const rem = errorData?.detail?.attempts_remaining;
      if (typeof rem === "number") {
        throw new Error(`Invalid code. ${rem} attempt${rem === 1 ? "" : "s"} remaining.`);
      }
      throw new Error("The verification code is invalid or has expired.");
    }
    if (response.status === 429) {
      throw new Error("Too many failed attempts. Please request a new code.");
    }
    throw new Error(errorData?.detail?.message || "Verification failed.");
  }

  const data = (await response.json()) as Record<string, unknown>;

  return {
    success: true,
    role: typeof data.role === "string" ? data.role : undefined,
    userId: typeof data.user_id === "string" ? data.user_id : undefined,
    verificationToken: typeof data.verification_token === "string" ? data.verification_token : undefined,
    rawResponse: data,
  };
}

export interface EmailOtpRequestParams {
  email: string;
  purpose?: string;
  communityId?: string;
}

export interface EmailOtpVerifyParams {
  email: string;
  otp: string;
  challengeId: string;
  purpose?: string;
  communityId?: string;
}

export async function requestEmailOtpNotification(
  params: EmailOtpRequestParams
): Promise<OtpNotificationRequestResult> {
  const cleanEmail = params.email.trim().toLowerCase();
  if (!cleanEmail || !cleanEmail.includes("@")) {
    throw new Error("A valid email address is required.");
  }

  const { fetchBackendApi } = await import("./backend-api");
  const response = await fetchBackendApi("api/v1/auth/email-otp/request", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      email: cleanEmail,
      purpose: params.purpose || "email_verification",
      ...(params.communityId ? { community_id: params.communityId } : {}),
    }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail?.message || "Failed to send email verification code.");
  }

  const data = (await response.json()) as { challenge_id: string; cooldown_seconds?: number };
  return {
    accepted: true,
    challengeId: data.challenge_id,
    cooldownSeconds: data.cooldown_seconds ?? 60,
    status: "QUEUED",
  };
}

export async function verifyEmailOtpNotification(
  params: EmailOtpVerifyParams
): Promise<OtpNotificationVerifyResult> {
  const cleanEmail = params.email.trim().toLowerCase();
  const cleanOtp = params.otp.trim();
  if (!cleanEmail || !cleanEmail.includes("@")) {
    throw new Error("A valid email address is required.");
  }
  if (!cleanOtp) {
    throw new Error("Verification code is required.");
  }
  if (!params.challengeId) {
    throw new Error("Challenge ID is required for verification.");
  }

  const { fetchBackendApi } = await import("./backend-api");
  const response = await fetchBackendApi("api/v1/auth/email-otp/verify", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      email: cleanEmail,
      otp: cleanOtp,
      challenge_id: params.challengeId,
      purpose: params.purpose || "email_verification",
    }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 401) {
      const rem = errorData?.detail?.attempts_remaining;
      if (typeof rem === "number") {
        throw new Error(`Invalid code. ${rem} attempt${rem === 1 ? "" : "s"} remaining.`);
      }
      throw new Error("The verification code is invalid or has expired.");
    }
    throw new Error(errorData?.detail?.message || "Email verification failed.");
  }

  const data = (await response.json()) as Record<string, unknown>;
  return {
    success: true,
    verificationToken: typeof data.verification_token === "string" ? data.verification_token : undefined,
    rawResponse: data,
  };
}

export interface NotificationStatusDetails {
  messageId: string;
  channel: NotificationChannel;
  status: NotificationStatus;
  messageType?: NotificationMessageType;
  providerMessageId?: string;
  purpose?: string;
  occurredAt?: string;
  createdAt?: string;
  failureReason?: string;
}

/**
 * Centralized method to query delivery status of a notification message.
 * Scoped to active tenant session via Next.js proxy.
 */
export async function getNotificationStatus(
  messageId: string
): Promise<NotificationStatusDetails> {
  const cleanId = messageId.trim();
  if (!cleanId) {
    throw new Error("Message ID is required to retrieve status.");
  }

  const { fetchBackendApi } = await import("./backend-api");
  const response = await fetchBackendApi(`api/v1/notifications/status/${encodeURIComponent(cleanId)}`, {
    method: "GET",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
    },
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 404) {
      throw new Error("Notification not found or access denied.");
    }
    throw new Error(errorData?.detail?.message || "Failed to retrieve notification status.");
  }

  const data = (await response.json()) as NotificationStatusDetails;
  return data;
}

/**
 * Poll notification status until a terminal state is reached or max attempts exceeded.
 * Applies modest exponential backoff and never polls aggressively.
 */
export async function pollNotificationStatus(
  messageId: string,
  options?: {
    maxAttempts?: number;
    intervalMs?: number;
    onStatusChange?: (status: NotificationStatusDetails) => void;
  }
): Promise<NotificationStatusDetails> {
  const maxAttempts = options?.maxAttempts ?? 10;
  let intervalMs = options?.intervalMs ?? 1500;
  const terminalStatuses: Set<NotificationStatus> = new Set([
    "DELIVERED",
    "READ",
    "FAILED",
    "EXPIRED",
    "UNKNOWN_OUTCOME",
  ]);

  let lastStatus: NotificationStatusDetails | null = null;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    lastStatus = await getNotificationStatus(messageId);
    if (options?.onStatusChange) {
      options.onStatusChange(lastStatus);
    }
    if (terminalStatuses.has(lastStatus.status)) {
      return lastStatus;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    intervalMs = Math.min(intervalMs * 1.5, 8000);
  }
  return lastStatus ?? getNotificationStatus(messageId);
}

