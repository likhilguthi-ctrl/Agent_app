# Community OTT — Notification Status Webhook Implementation Report

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Specification:** `docs/implementation/FRONTEND_INTEGRATION.md`  
**External Notification Service Base URL:** `https://handorg.duckdns.org/notifications`  
**Report Date:** October 22, 2026  
**Status:** Verification Passed (14/14 Webhook Tests, 102/102 Backend Tests, 30/30 Frontend Tests)  

---

## 1. Executive Summary

This report documents the end-to-end implementation and verification of the Notification Status Webhook subsystem for the Community OTT Agent Platform.

The external Community OTT Notification Microservice transmits asynchronous delivery receipts and lifecycle state transitions for SMS, WhatsApp, and Email notifications. A production-grade webhook receiver, persistent event log, and authenticated status inquiry API have been engineered directly within the FastAPI backend and integrated through the Next.js same-origin reverse proxy and frontend notification client.

All cryptographic integrity checks, replay protections, terminal state enforcement, PostgreSQL deduplication, and cross-tenant boundary protections were validated with zero regressions across the existing Phase 3, Phase 4, and Phase 5 suites.

---

## 2. Architecture & Endpoint Routing

### 2.1 Backend Webhook Ingestion Route
* **Endpoint:** `POST /api/v1/webhooks/notifications/status`
* **Router Prefix:** Mounted at `backend/app/api/v1/router.py` via `notification_webhooks_router`.
* **Access Control:** Publicly accessible endpoint protected exclusively by HMAC-SHA256 signature verification over the exact incoming raw request body and timestamp header. No tenant session cookie or browser credentials required.
* **Registered Callback URL for Notification Microservice:**
  * Direct Backend: `https://<backend-domain>/api/v1/webhooks/notifications/status`
  * Frontend Ingress: `https://<frontend-domain>/backend-api/api/v1/webhooks/notifications/status`

### 2.2 Authenticated Delivery Status Query Route
* **Endpoint:** `GET /api/v1/notifications/status/{message_id}`
* **Router Prefix:** Mounted at `backend/app/api/v1/router.py` via `notifications_router`.
* **Access Control:** Requires valid authenticated community session cookie (`community_session`) or Bearer token, plus capability `PermissionCode.NOTIFICATION_READ`.
* **Tenant Isolation:**
  * Enforces strict multi-tenant boundary: a tenant user or Community Admin can only query messages issued within their active tenant (`msg.tenant_id == caller.tenant_id`). Cross-tenant access returns HTTP 404 (Not Found) to prevent resource enumeration.
  * Super Admin / Platform Operator (`caller.role == "SUPER_ADMIN"`) possesses global visibility across all tenants.

### 2.3 Next.js Same-Origin Reverse Proxy Routing
* **File:** `frontend/src/app/backend-api/[...path]/route.ts`
* Added explicit route recognition and security proxy forwarding for:
  * `api/v1/webhooks/notifications/status` (POST: passes headers `X-Notification-Timestamp`, `X-Notification-Signature`, `X-Event-ID`)
  * `api/v1/notifications/status/[messageId]` (GET: passes `community_session` and forwards status JSON)

### 2.4 Frontend Client Status Methods
* **File:** `frontend/src/lib/notifications.ts`
* Added client helper methods:
  * `getNotificationStatus(messageId: string)`: Queries `/backend-api/api/v1/notifications/status/${messageId}`.
  * `pollNotificationStatus(messageId: string, options)`: Polls status using exponential backoff (initial delay, max delay, timeout, backoff multiplier) and exits immediately upon reaching a terminal state (`DELIVERED`, `READ`, `FAILED`, `EXPIRED`).

---

## 3. Cryptographic Verification & Replay Protection

### 3.1 HMAC-SHA256 Signature Verification
* **Algorithm:** HMAC-SHA256.
* **Signed String:** `f"{timestamp_header}.{raw_body_bytes}"`.
* **Header Format:** `X-Notification-Signature: sha256=<hex_digest>`.
* **Timestamp Header:** `X-Notification-Timestamp: <ISO-8601 UTC timestamp>` (e.g. `2026-10-22T10:00:00Z`).
* **Comparison:** Constant-time comparison using `hmac.compare_digest` to prevent timing attacks.
* **Fail-Closed Policy:**
  * If `NOTIFICATION_WEBHOOK_SECRET` is unset or empty in production or staging, the webhook receiver fails closed with HTTP 500 (`INTERNAL_ERROR`), logging `notification_webhook_secret_missing`.
  * If headers are missing or malformed, returns HTTP 400 (`MISSING_SIGNATURE`).
  * If signature does not match, returns HTTP 401 (`INVALID_SIGNATURE`).

### 3.2 Replay Protection
* The timestamp is parsed and converted to UTC.
* The system evaluates `abs(now_utc - timestamp_utc).total_seconds()`.
* If the difference exceeds `NOTIFICATION_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` (default: 300 seconds / 5 minutes), the request is rejected with HTTP 400 (`TIMESTAMP_OUT_OF_BOUNDS`).

---

## 4. Persistence Schema & Migration Head

### 4.1 Alembic Migration
* **Migration File:** `backend/migrations/versions/20261022_21_notification_status_webhooks.py`
* **Revision:** `20261022_21`
* **Down Revision:** `20261021_20` (Single linear head preserved)
* **Status:** Verified head in Alembic repository.

### 4.2 Database Tables Created

#### 1. `notification_messages`
Authoritative record of notification messages dispatched by the platform:
* `id`: UUID (Primary Key)
* `tenant_id`: UUID (Foreign Key -> `tenants.id`, ON DELETE RESTRICT, Nullable for platform/system notifications)
* `message_id`: VARCHAR(128), UNIQUE, NOT NULL
* `channel`: VARCHAR(32), NOT NULL (`SMS`, `WHATSAPP`, `EMAIL`)
* `message_type`: VARCHAR(64), NOT NULL (`LOGIN_OTP`, `PAYMENT_OTP`, `PASSWORD_RESET_OTP`, `ACCOUNT_UPDATE`, etc.)
* `recipient`: VARCHAR(64), NOT NULL (E.164 normalized mobile or email)
* `status`: VARCHAR(32), NOT NULL (`QUEUED`, `PROCESSING`, `PROVIDER_ACCEPTED`, `DELIVERED`, `READ`, `RETRY_PENDING`, `MOCK_SENT`, `FAILED`, `EXPIRED`, `UNKNOWN_OUTCOME`)
* `priority`: VARCHAR(8), NOT NULL (default `P0`)
* `provider_message_id`: VARCHAR(128), Nullable
* `purpose`: VARCHAR(64), Nullable (`login`, `agent_registration`, `admin_registration`, `user_registration`)
* `challenge_id`: UUID, Nullable (FK reference to `otp_challenges.id`)
* `failure_reason`: VARCHAR(256), Nullable
* `metadata_json`: JSON, Nullable
* `occurred_at`: TIMESTAMPTZ, Nullable
* `created_at`: TIMESTAMPTZ, NOT NULL (default `now()`)
* `updated_at`: TIMESTAMPTZ, NOT NULL (default `now()`)
* **Indexes:**
  * `ix_notification_messages_message_id` on (`message_id`)
  * `ix_notification_messages_tenant_status` on (`tenant_id`, `status`)
  * `ix_notification_messages_challenge_id` on (`challenge_id`)

#### 2. `notification_webhook_events`
Audit log of raw incoming webhook event receipts:
* `id`: UUID (Primary Key)
* `event_id`: VARCHAR(128), UNIQUE, NOT NULL (deduplication key)
* `event_type`: VARCHAR(64), NOT NULL (`message.status_changed`)
* `service_id`: VARCHAR(64), NOT NULL (`community-ott`)
* `message_id`: VARCHAR(128), NOT NULL
* `tenant_id`: UUID, Nullable (FK -> `tenants.id`)
* `status`: VARCHAR(32), NOT NULL
* `channel`: VARCHAR(32), NOT NULL
* `provider_message_id`: VARCHAR(128), Nullable
* `payload`: JSON, NOT NULL (raw JSON payload)
* `processing_status`: VARCHAR(32), NOT NULL (`processed`, `duplicate`, `quarantined`, `error`)
* `error_message`: VARCHAR(512), Nullable
* `occurred_at`: TIMESTAMPTZ, NOT NULL
* `created_at`: TIMESTAMPTZ, NOT NULL (default `now()`)
* `processed_at`: TIMESTAMPTZ, NOT NULL (default `now()`)
* **Indexes:**
  * `ix_notification_webhook_events_event_id` on (`event_id`)
  * `ix_notification_webhook_events_message_id` on (`message_id`)
  * `ix_notification_webhook_events_tenant_id` on (`tenant_id`)

---

## 5. Deduplication & State Machine Rules

### 5.1 Deduplication
1. Incoming `eventId` is checked against `notification_webhook_events.event_id`.
2. If already processed, the service returns HTTP 200 with `status: "duplicate"` without modifying notification state.
3. Database unique constraint on `event_id` acts as secondary safety: concurrent duplicate requests that race past the query check are caught by `IntegrityError`, rolled back cleanly, and return `status: "duplicate"`.

### 5.2 Terminal State & Out-of-Order Protection
* **Terminal States:** `DELIVERED`, `FAILED`, `EXPIRED`, `UNKNOWN_OUTCOME`.
* **State Progression Protection:**
  * Once a message reaches `DELIVERED`, only transition to `READ` is permitted. Any attempt by a late `PROCESSING`, `QUEUED`, or `PROVIDER_ACCEPTED` event to overwrite `DELIVERED` is silently ignored, preserving `DELIVERED` status.
  * Once a message reaches `FAILED`, `EXPIRED`, or `UNKNOWN_OUTCOME`, no subsequent status update is allowed.
  * Intermediate statuses like `PROVIDER_ACCEPTED` update `provider_message_id` but never mark the message as `DELIVERED`.
* **Timestamp Monotonicity:**
  * If an incoming event carries an `occurredAt` timestamp older than the stored `notification_messages.occurred_at`, the status transition is discarded, preventing out-of-order webhook delivery from regressing state.
  * Offsets are normalized to UTC across both naive and aware representations to ensure robust comparison across all database dialects.

### 5.3 Quarantined Unknown Messages
* If a webhook arrives for a `messageId` not found in `notification_messages` or `otp_challenges`:
  * The event is persisted in `notification_webhook_events` with `processing_status: "quarantined"` and `tenant_id: None`.
  * The webhook returns HTTP 200 with `status: "quarantined"`, preventing cross-tenant data corruption while logging the anomaly for administrator review.

---

## 6. Verification & Automated Test Results

### 6.1 Webhook Test Suite (`test_notification_webhook.py`)
All 14 comprehensive test scenarios passed:
```
============================= test session starts =============================
platform win32 -- Python 3.12.3, pytest-8.4.2
collected 14 items

backend/tests/test_notification_webhook.py::test_scenario_01_valid_signed_webhook_accepted[asyncio] PASSED [  7%]
backend/tests/test_notification_webhook.py::test_scenario_02_missing_signature_rejected[asyncio] PASSED [ 14%]
backend/tests/test_notification_webhook.py::test_scenario_03_invalid_signature_rejected[asyncio] PASSED [ 21%]
backend/tests/test_notification_webhook.py::test_scenario_04_missing_webhook_secret_fails_closed[asyncio] PASSED [ 28%]
backend/tests/test_notification_webhook.py::test_scenario_05_modified_body_with_original_signature_rejected[asyncio] PASSED [ 35%]
backend/tests/test_notification_webhook.py::test_scenario_06_stale_timestamp_rejected[asyncio] PASSED [ 42%]
backend/tests/test_notification_webhook.py::test_scenario_07_invalid_payload_and_unsupported_status_rejected[asyncio] PASSED [ 50%]
backend/tests/test_notification_webhook.py::test_scenario_08_valid_duplicate_event_does_not_duplicate_persistence[asyncio] PASSED [ 57%]
backend/tests/test_notification_webhook.py::test_scenario_09_concurrent_duplicate_event_handling[asyncio] PASSED [ 64%]
backend/tests/test_notification_webhook.py::test_scenario_10_unknown_message_id_cannot_update_another_tenant_data[asyncio] PASSED [ 71%]
backend/tests/test_notification_webhook.py::test_scenario_11_valid_delivery_status_updates_correct_notification[asyncio] PASSED [ 78%]
backend/tests/test_notification_webhook.py::test_scenario_12_provider_accepted_status_is_not_treated_as_delivered[asyncio] PASSED [ 85%]
backend/tests/test_notification_webhook.py::test_scenario_13_out_of_order_events_cannot_regress_terminal_states[asyncio] PASSED [ 92%]
backend/tests/test_notification_webhook.py::test_scenario_14_frontend_status_endpoint_authorization[asyncio] PASSED [100%]

======================= 14 passed in 33.79s =======================
```

### 6.2 Backend Regression Suites
1. **Notification Service Client (`test_notification_service.py`):**
   * Result: **21 passed in 5.95s** (100% pass)
2. **OTP Login & Messaging (`test_otp_login_flow.py`, `test_messaging.py`):**
   * Result: **34 passed in 1.96s** (100% pass)
3. **Authentication & Session Security (`test_authentication_api.py`):**
   * Result: **33 passed in 55.10s** (100% pass)

### 6.3 Frontend Verification Suites
1. **Notification Client Tests (`npm run test:notifications --prefix frontend`):**
   * Result: **9 passed, 0 failed in 404ms**
2. **Proxy Integration Tests (`npm run test:proxy --prefix frontend`):**
   * Result: **21 passed, 0 failed in 125s**
3. **TypeScript Typecheck (`npm run typecheck --prefix frontend`):**
   * Result: **0 errors (Exit Code 0)**
4. **Next.js Production Build (`npm run build --prefix frontend`):**
   * Result: **Compiled successfully, static optimization 29/29 routes passed (Exit Code 0)**

---

## 7. Files Created & Modified

### Created Files
| Path | Purpose |
|---|---|
| `backend/app/modules/messaging/webhook_service.py` | Core webhook verification, replay protection, deduplication, and status updater |
| `backend/migrations/versions/20261022_21_notification_status_webhooks.py` | Alembic migration for `notification_messages` & `notification_webhook_events` |
| `backend/tests/test_notification_webhook.py` | 14 comprehensive test scenarios for webhook processing and status retrieval |
| `docs/reports/NOTIFICATION_WEBHOOK_IMPLEMENTATION_REPORT.md` | This architectural and verification report |

### Modified Files
| Path | Modification |
|---|---|
| `backend/app/core/config.py` | Added webhook secret, timestamp tolerance, and service ID configuration with staging/prod validation |
| `backend/.env.example` | Added sample `NOTIFICATION_WEBHOOK_SECRET` and configuration keys |
| `backend/.env` | Added local development configuration entries |
| `backend/app/modules/messaging/models.py` | Added SQLAlchemy declarative models for notification tracking |
| `backend/migrations/env.py` | Registered models in Alembic metadata |
| `backend/app/modules/messaging/api.py` | Implemented `POST /api/v1/webhooks/notifications/status` and `GET /api/v1/notifications/status/{message_id}` |
| `backend/app/api/v1/router.py` | Mounted webhook and notification status routers |
| `backend/app/modules/authentication/otp_service.py` | Persisted `NotificationMessage` records on OTP dispatch |
| `frontend/src/app/backend-api/[...path]/route.ts` | Configured proxy route handlers for status retrieval and webhook ingestion |
| `frontend/src/lib/notifications.ts` | Added `getNotificationStatus` and `pollNotificationStatus` methods |
| `backend/tests/test_notification_service.py` | Updated test fixture for production webhook secret requirement |

---

## 8. Deployment & Operational Checklist

To complete live end-to-end webhook delivery in staging or production:

1. **Environment Configuration:**
   * Set `NOTIFICATION_WEBHOOK_SECRET` in production `.env` (min 32 random alphanumeric characters).
   * Ensure `NOTIFICATION_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` is set (default 300).
   * Ensure `NOTIFICATION_SERVICE_ID=community-ott` matches the provider's registered service ID.
2. **Provider Callback Registration:**
   * Register the webhook callback URL with the Notification Microservice team:
     * Public URL: `https://<your-domain>/api/v1/webhooks/notifications/status`
     * Shared Secret: Match `NOTIFICATION_WEBHOOK_SECRET`.
     * Events: Subscribe to `message.status_changed`.
3. **Database Migration:**
   * Run `alembic upgrade head` to apply revision `20261022_21`.
4. **External Infrastructure Status (Remaining Blockers):**
   * As identified in Stage 5.0 audit, `https://handorg.duckdns.org/notifications` currently has an external Port 443 SSL/HTTPS connection timeout (`BLK-01`).
   * Once DuckDNS port 443 routing is unblocked by the upstream infrastructure team, live webhook callbacks will be verified over the wire. All internal application code, tests, and database models are fully complete and production-ready.

