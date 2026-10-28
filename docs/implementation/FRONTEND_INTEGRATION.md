# Notification Service: Frontend Integration Guide

This guide describes how a frontend should request and display SMS, WhatsApp, and email notifications through the platform backend and Notification Microservice.

## Important security boundary

The Notification Microservice is a **service-to-service API**. A browser or mobile app must not call it directly and must never contain `X-API-Key`, provider credentials, or webhook secrets. The frontend should call the Community Backend using its normal user authentication. The backend determines the active tenant and calls the Notification Microservice with its service credentials.

Do not send a `tenantId` in the notification request. The current Notification Microservice maps its configured API key to a requesting service and tenant. Tenant registration and dynamic tenant credential resolution are planned separately; the current registry is configuration based.

## Base URL and authentication

For the local Compose stack, the service is normally available at:

```text
http://localhost:8100
```

The backend-to-service request uses:

```http
X-API-Key: <service-api-key>
Idempotency-Key: <unique-key-for-this-notification-intent>
Content-Type: application/json
```

`Idempotency-Key` is optional to the API but should be included for safe retries. Use the same key when retrying the same notification intent, and a new key for a different notification.

## Submit a notification

```http
POST /api/v1/messages
```

### Common request fields

| Field | Required | Meaning |
|---|---:|---|
| `channel` | Yes | `SMS`, `WHATSAPP`, or `EMAIL`. The caller must be allowed to use the channel. |
| `messageType` | Yes | Uppercase code such as `LOGIN_OTP`, `PAYMENT_OTP`, or `ACCOUNT_UPDATE`. It must be enabled for the caller. |
| `recipient` | Yes | E.164 phone number for SMS/WhatsApp; email address for Email. Maximum 320 characters. |
| `content.type` | Yes | `TEXT` or `TEMPLATE`. |
| `content.text` | For `TEXT` | Message body. Maximum 4,000 characters. |
| `content.templateCode` | For `TEMPLATE` | Template identifier. Maximum 120 characters. |
| `content.locale` | Optional | Template locale, for example `en`. |
| `content.variables` | Optional | Template variables, for example an OTP value. |
| `metadata` | Optional | Application reference data. Do not put credentials or unnecessary personal data here. |
| `expiresAt` | Optional | ISO-8601 instant. The message must still be valid when delivery starts. |

### SMS text

```json
{
  "channel": "SMS",
  "messageType": "PAYMENT_OTP",
  "recipient": "+919876543210",
  "content": {
    "type": "TEXT",
    "text": "Your payment verification code is 123456."
  },
  "metadata": {
    "referenceId": "payment-123"
  }
}
```

### WhatsApp approved template

```json
{
  "channel": "WHATSAPP",
  "messageType": "PAYMENT_OTP",
  "recipient": "+919876543210",
  "content": {
    "type": "TEMPLATE",
    "templateCode": "PAYMENT_OTP",
    "locale": "en",
    "variables": {
      "otp": "123456"
    }
  },
  "metadata": {
    "referenceId": "payment-123"
  }
}
```

WhatsApp also accepts `TEXT` content in the current adapter. For production WhatsApp template sends, the named template and parameter layout must be approved and configured with the provider.

### Email text (local mock only)

```json
{
  "channel": "EMAIL",
  "messageType": "ACCOUNT_UPDATE",
  "recipient": "viewer@example.com",
  "content": {
    "type": "TEXT",
    "text": "Your account details were updated."
  },
  "metadata": {
    "referenceId": "account-update-123"
  }
}
```

The local profile has an email mock adapter. A production email provider has **not** been selected or integrated. Do not present a local `MOCK_SENT` result as an email sent to a real inbox.

## Acceptance response

Successful acceptance returns HTTP `202 Accepted` after the message and outbox row are committed:

```json
{
  "messageId": "msg_5db4ad9a4d8743859b15d5a06713c2f1",
  "status": "QUEUED",
  "priority": "P0",
  "channel": "WHATSAPP",
  "statusUrl": "/api/v1/messages/msg_5db4ad9a4d8743859b15d5a06713c2f1"
}
```

`202` means accepted for asynchronous processing. It does not mean a provider accepted or delivered the message. Save `messageId` and use the status endpoint below.

Priority is assigned by the service from `messageType`; callers cannot set it in the body:

| Priority | Current message types |
|---|---|
| `P0` | `LOGIN_OTP`, `PAYMENT_OTP`, `PASSWORD_RESET_OTP`, `URGENT_SECURITY_ALERT` |
| `P1` | `PAYMENT_RECEIPT`, `ACCOUNT_UPDATE` |
| `P2` | All other allowed message types |

## Status lookup and polling

```http
GET /api/v1/messages/{messageId}
GET /api/v1/messages/{messageId}/attempts
```

Both endpoints require the same service authentication and are scoped to the owning service and tenant. Frontends should call a Community Backend endpoint that proxies or exposes the needed status; do not expose the Notification Microservice API key to a browser.

The attempts endpoint returns an array with `attemptNumber`, `provider`, `providerMessageId`, `status`, `errorCode`, `errorDetails`, `startedAt`, and `finishedAt`. It may be empty before the worker starts. Error details are safe provider summaries, but should still be shown only where useful to the user.

Example status response:

```json
{
  "messageId": "msg_5db4ad9a4d8743859b15d5a06713c2f1",
  "channel": "EMAIL",
  "status": "MOCK_SENT",
  "priority": "P2",
  "createdAt": "2026-10-16T10:30:00Z",
  "updatedAt": "2026-10-16T10:30:02Z",
  "providerMessageId": "mock_msg_5db4ad9a4d8743859b15d5a06713c2f1",
  "providerAcceptedAt": null,
  "deliveredAt": null,
  "failureCode": null,
  "failureDetails": "Mock adapter: no external message was sent"
}
```

Recommended UI behavior:

1. Show a queued/pending state after `202`.
2. Poll the backend status endpoint with a modest interval and backoff, or have the backend relay callback updates to the frontend using the app’s existing real-time mechanism.
3. Stop routine polling when the status is `MOCK_SENT`, `DELIVERED`, `READ`, `FAILED`, `EXPIRED`, or `UNKNOWN_OUTCOME`.
4. Treat `PROVIDER_ACCEPTED` as accepted by the provider, not as delivered.
5. Treat `UNKNOWN_OUTCOME` as uncertain. Do not automatically resend with a new idempotency key because that can create a duplicate.

The Notification Microservice webhook is a server-to-server callback to the registered backend URL; it is not a browser webhook. The backend should validate it and then update or notify the frontend.

## Status values and scenarios

| Status | Meaning | Suggested client presentation |
|---|---|---|
| `QUEUED` | Accepted and awaiting worker delivery. | Pending |
| `PROCESSING` | A worker is handling the send. | Sending |
| `PROVIDER_ACCEPTED` | Provider accepted the send request. | Sent to provider; delivery pending |
| `DELIVERED` | Provider reported delivery. | Delivered |
| `READ` | Provider reported the message was read, where supported. | Read |
| `RETRY_PENDING` | A retryable error occurred and another attempt may run. | Retrying |
| `MOCK_SENT` | Local mock adapter accepted the job; no external message was sent. | Mocked (development only) |
| `FAILED` | The send failed permanently or could not be configured. | Failed; show safe error text if appropriate |
| `EXPIRED` | The message expired before delivery. | Expired |
| `UNKNOWN_OUTCOME` | The provider may have accepted the message, but the service could not confirm. | Outcome unknown; avoid blind resend |

Current implementation caveat: a retryable delivery that exhausts its attempts is placed in a RabbitMQ dead-letter queue while the database status can remain `RETRY_PENDING`. The frontend should not assume every long-lived `RETRY_PENDING` message will automatically become `FAILED`; operations/backend reconciliation is needed for that case.

## Idempotency and client retries

For a payment OTP intent, the backend can use a stable key such as:

```http
Idempotency-Key: payment-123-otp
```

- On a network timeout, retry the identical request with the same key.
- Keep the key at or below 200 characters.
- Same key and same request returns the original message ID and acceptance.
- Same key with changed request data returns HTTP `409 Conflict`.
- Use a new key for a new OTP or a separate notification event.
- Do not use one constant key for every message.

The key is scoped by the authenticated requester service and tenant in the current service implementation.

## Error responses

| HTTP status | Typical reason | Frontend/backend action |
|---:|---|---|
| `400` | Invalid JSON/fields, invalid recipient, missing text/template code, or expired `expiresAt`. | Fix the request; do not retry unchanged. |
| `401` | Missing or invalid service API key. | Backend configuration/authentication issue. Never ask the end user for this key. |
| `403` | Caller is not allowed to use the channel or message type. | Correct service policy/configuration. |
| `404` | Message is not found for this caller/tenant. | Check the ID and tenant-scoped backend mapping. |
| `409` | Idempotency key was reused with a different request. | Resolve whether this is a new intent; use a new key only for a genuinely new notification. |
| `202` | Message accepted for asynchronous processing. | Store the message ID and track status. |

Error body shape:

```json
{
  "error": "Human-readable error message"
}
```

## Status webhook contract

The Notification Microservice posts status updates to the callback URL configured for the requesting service. The backend callback receiver should verify the signature, deduplicate by `eventId`, persist the update, and then expose it to the frontend.

Example local mock event:

```json
{
  "eventId": "5d15c6d3-5957-4a23-a151-f5d9484a3d19",
  "eventType": "message.status_changed",
  "messageId": "msg_5db4ad9a4d8743859b15d5a06713c2f1",
  "serviceId": "local-service",
  "status": "MOCK_SENT",
  "channel": "EMAIL",
  "providerMessageId": "mock_msg_5db4ad9a4d8743859b15d5a06713c2f1",
  "occurredAt": "2026-10-16T10:30:02Z",
  "failure": null
}
```

Headers:

```http
Content-Type: application/json
X-Event-ID: 5d15c6d3-5957-4a23-a151-f5d9484a3d19
X-Notification-Timestamp: 2026-10-16T10:30:02Z
X-Notification-Signature: sha256=<hex HMAC-SHA256>
```

The signature is HMAC-SHA256 over the exact raw request body prefixed with `timestamp + "."`, using the configured service webhook secret. Verify the signature against raw bytes before parsing JSON. Do not log the secret or signature. The sample receiver is in `Notification_microservice/sample-webhook`.

Possible webhook statuses include `PROVIDER_ACCEPTED`, `RETRY_PENDING`, `FAILED`, `EXPIRED`, `DELIVERED`, `READ`, `UNKNOWN_OUTCOME`, and local-only `MOCK_SENT`.

## Channel and provider availability

| Channel | Local profile | Non-local/provider profile |
|---|---|---|
| `SMS` | Mock adapter; returns `MOCK_SENT`. | Twilio adapter; currently supports `TEXT`. |
| `WHATSAPP` | Mock adapter; returns `MOCK_SENT`. | WhatsApp Cloud API adapter; supports text and template requests. |
| `EMAIL` | Mock adapter; returns `MOCK_SENT`. | No email adapter is configured. Do not enable for real delivery yet. |

The requesting service must also have the channel and `messageType` in its allowlist. If a channel is not allowed, the API returns `403` before creating a message.

## Local end-to-end email mock

1. Start the sample callback receiver using the instructions in `Notification_microservice/sample-webhook/README.md`.
2. Start the notification Compose stack with the `local` profile and test registry configured to allow `EMAIL`.
3. Have the backend submit the email request above with its service API key and an idempotency key.
4. Expect `202` with a message ID, then `MOCK_SENT` from the status endpoint and a signed `MOCK_SENT` event at the backend callback URL.
5. The receiver prints the callback JSON. No email is sent to the address.
