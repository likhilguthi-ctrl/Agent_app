# Community OTT — Notification Frontend Migration Implementation

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Date:** October 22, 2026  
**Auditor:** Automated System Audit & Architecture Verification Framework  
**Scope:** Frontend Notification Architecture, Safe Client Implementation, Call Sites Verification & Boundary Decision  
**Reference Document:** `docs/implementation/FRONTEND_INTEGRATION.md`  

---

## 1. Architectural Directive & Boundary Decision

### 1.1 The Security Mandate
In accordance with the project instructions:
> *"The guide explicitly describes this as a service-to-service API and says browsers must not contain `X-API-Key`, provider credentials, or webhook secrets.*  
> **Therefore, do not expose the documented service API key in frontend code.** Before implementing direct frontend requests, inspect the provider's actual documentation and determine whether it supports a separate public/browser API with a safe authentication mechanism.  
> If browser-safe access is not supported, stop the direct-call migration and document the precise blocker. Do not invent a public token, disable authentication, or put a private key in `NEXT_PUBLIC_*` environment variables.*  
> *If the provider only supports service-to-service authentication, do not implement direct browser calls to that API. Record that a browser-safe provider endpoint or authentication mechanism is required before this architecture can be completed securely.*  
> *If a safe direct integration is impossible, retain the minimum secure backend-to-provider boundary and explain why complete backend removal is incompatible with the supplied API contract."*

### 1.2 Boundary Decision & Impossibility of Direct Call
Following an exhaustive audit of `docs/implementation/FRONTEND_INTEGRATION.md`, the external Notification Microservice contract was verified to be strictly a server-to-server API:
- Endpoint: `POST /api/v1/messages`
- Required Authentication: `X-API-Key: <service-api-key>`
- Documentation directive: *"A browser or mobile app must not call it directly and must never contain `X-API-Key`, provider credentials, or webhook secrets."*

Because the external provider does not provide a public or browser-safe endpoint, **direct browser calls to the provider cannot be implemented securely**. Complete removal of the backend notification-delivery adapter is impossible without compromising platform security, invalidating phone OTP authentication, and exposing private service credentials.

Therefore, the **minimum secure backend-to-provider boundary is retained**:
1. The frontend initiates notification requests through the secure Next.js reverse proxy (`/backend-api/api/v1/auth/otp/request`).
2. The Community Backend maintains private service credentials (`NOTIFICATION_SERVICE_API_KEY`), generates the cryptographically random OTP, commits the challenge hash and PostgreSQL rate limit buckets, and dispatches the message via `NotificationServiceClient`.
3. The frontend receives an opaque response containing the challenge ID and cooldown duration, without ever seeing the plaintext OTP or provider credentials.

---

## 2. Centralized Frontend Notification Architecture

To establish clean typed abstractions and prepare for future provider enhancements, a centralized notification client module was authored at [`frontend/src/lib/notifications.ts`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/lib/notifications.ts).

### 2.1 Type Contracts (Mirroring `FRONTEND_INTEGRATION.md`):
The module defines strict TypeScript types matching the provider specification:

```typescript
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
```

### 2.2 Fail-Closed Runtime Safety Guard:
The module includes `assertBrowserSafeProvider` to prevent accidental inclusion of private service keys in browser runtime code:

```typescript
export class InsecureBrowserDeliveryBlockedError extends Error {
  constructor(message?: string) {
    super(
      message ||
        "Direct browser-to-provider notification delivery is blocked because the provider requires a private service X-API-Key. All notification dispatches must flow through the secure backend proxy."
    );
    this.name = "InsecureBrowserDeliveryBlockedError";
  }
}

export function assertBrowserSafeProvider(
  providerAuthType: "service_api_key" | "browser_token"
): void {
  if (providerAuthType === "service_api_key") {
    throw new InsecureBrowserDeliveryBlockedError();
  }
}
```

### 2.3 Idempotency Key Generation:
In accordance with Section 1 and Section 3, a stable idempotency generator ensures that retries reuse the same key, while new notification intents generate unique keys bounded to 200 characters:

```typescript
export function generateNotificationIntentKey(
  purpose: string,
  discriminator: string
): string {
  const safePurpose = purpose.replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 40);
  const safeDisc = discriminator.replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 100);
  return `notif_${safePurpose}_${safeDisc}`.slice(0, 200);
}
```

### 2.4 User Presentation Mapping:
The module implements the recommended presentation mappings from `FRONTEND_INTEGRATION.md`, explicitly treating `QUEUED` / `202` as pending (not delivered) and `UNKNOWN_OUTCOME` as uncertain:

```typescript
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
```

### 2.5 Backend-Mediated Notification Client:
The `requestOtpNotification` function provides a clean, typed interface for frontend UI components:

```typescript
export async function requestOtpNotification(
  params: OtpNotificationRequestParams
): Promise<OtpNotificationRequestResult> {
  const { fetchBackendApi } = await import("./backend-api");
  const response = await fetchBackendApi("api/v1/auth/otp/request", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      mobile_number: params.mobileNumber,
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
```

---

## 3. Frontend Call Sites Review & Verification

All existing frontend call sites were inspected and verified:

### 3.1 `CommunityOtpLogin.tsx` (Login Flow)
- **File:** `frontend/src/components/CommunityOtpLogin.tsx`
- **Behavior:** Invokes `api/v1/auth/otp/request` with `mobile_number`.
- **Verified State:**
  - Enforces 60-second cooldown timer.
  - Receives opaque `challenge_id` from backend.
  - Submits code via `api/v1/auth/otp/verify` with `challenge_id`.
  - Sets partitioned HttpOnly `community_session` cookie.
  - Resolves authorization context (`/api/v1/auth/context`).
- **No Private Secrets:** Uses `fetchBackendApi`, no direct credentials.

### 3.2 `frontend/src/app/agent/register/page.tsx` (Subscriber KYC Registration Flow)
- **File:** `frontend/src/app/agent/register/page.tsx`
- **Behavior:** Agent enters subscriber mobile number and clicks "Verify Mobile".
- **Verified State:**
  - Normalizes phone number to E.164.
  - Calls `api/v1/auth/otp/request` with `purpose: "user_registration"`.
  - Verifies code via `api/v1/auth/otp/verify`.
  - Receives opaque `verification_token` to attach to subscriber creation payload.
  - Captures subscriber photo and encrypts KYC documents.

### 3.3 `frontend/src/components/AddEmployeeForm.tsx` (Agent Nomination Flow)
- **File:** `frontend/src/components/AddEmployeeForm.tsx`
- **Behavior:** Community Admin nominates a new Agent candidate.
- **Verified State:**
  - Normalizes candidate mobile to E.164.
  - Calls `api/v1/auth/otp/request` with `purpose: "agent_registration"`.
  - Verifies code via `api/v1/auth/otp/verify`.
  - Obtains `verification_token` required by `POST /api/v1/admin/agents/nominate`.
  - Enforces two-eye appointment lifecycle (`PENDING` status awaiting Super Admin approval).

---

## 4. Provider Requirements for Future Direct Browser Integration

For a future version of the Notification Microservice to securely support direct browser-side API access, the external provider must implement the following architectural features:

1. **Browser-Safe Client Authentication Scheme:**
   - Instead of a single static `X-API-Key`, the provider must support short-lived, ephemeral **Client Tokens** (e.g. JWT signed by the backend with a 5-minute expiry) or public publishable keys with strict domain allowlists (similar to Stripe or Razorpay Checkout).
2. **Dedicated Client-Facing Endpoint:**
   - A dedicated public endpoint (e.g., `POST /api/v1/client/messages` or `POST /api/v1/client/challenges`) with Cross-Origin Resource Sharing (CORS) headers allowing requests from verified community web origins (`*.avanflix.com`).
3. **Template-Only Dispatch for Browser Clients:**
   - Browser clients must be restricted to pre-registered template codes (e.g., `LOGIN_OTP`, `PAYMENT_OTP`) with server-side variable injection. Browser clients must NEVER be permitted to submit arbitrary raw `TEXT` bodies, preventing phishing and message spoofing.
4. **Provider-Side Rate Limiting & Abuse Defense:**
   - The provider must enforce rate limiting by client IP, recipient number, and browser fingerprint to prevent toll fraud and SMS pumping without depending solely on backend application rate limiting.
5. **Client Status Polling Without Administrative Permissions:**
   - The status endpoint `GET /api/v1/messages/{messageId}` must be accessible using the client token associated with that specific message ID, without granting access to other messages across the tenant.

Until the external Notification Microservice introduces these capabilities, the Community Backend boundary remains the only cryptographically sound and secure delivery architecture.

