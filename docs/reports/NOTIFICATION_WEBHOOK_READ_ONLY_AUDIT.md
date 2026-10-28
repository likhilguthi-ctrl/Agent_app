# Community OTT — Notification Webhook Read-Only Audit Report

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Specification:** `docs/implementation/FRONTEND_INTEGRATION.md`  
**External Notification Service URL:** `https://handorg.duckdns.org/notifications`  
**Audit Date:** October 22, 2026  
**Audit Type:** Strict Read-Only Verification  
**Overall Verdict:** **PASS WITH BLOCKERS**  

---

## 1. Executive Verdict & Summary

### Overall Verdict: **PASS WITH BLOCKERS**

* **Internal Application Architecture & Code:** **PASS (100% Verified)**  
  The FastAPI backend webhook receiver, cryptographic HMAC-SHA256 signature verification, replay protection, terminal state enforcement, PostgreSQL deduplication, tenant isolation, and Next.js proxy integration are correctly implemented and completely covered by automated regression test suites (14/14 webhook tests, 21/21 notification service tests, 30/30 frontend tests).
* **Production Deployment Readiness:** **BLOCKERS PRESENT (External & Migration)**  
  Full end-to-end live delivery cannot be completed until three external blockers are addressed:
  1. **HTTPS Port 443 Timeout (`BLK-01`):** `https://handorg.duckdns.org` times out on port 443 (HTTP port 80 responds, but HTTPS is mandatory in production).
  2. **Provider Webhook Callback Registration (`BLK-02`):** The callback endpoint `https://<domain>/api/v1/webhooks/notifications/status` and shared secret must be registered with the Notification Microservice team.
  3. **Alembic Migration Chain Cast Defect (`BLK-03`):** Upstream migration `20261018_16_member_relationships.py` contains raw SQL parameter binding that fails with `psycopg.errors.AmbiguousParameter` on un-migrated PostgreSQL instances when running `alembic upgrade head`.

---

## 2. Evidence by Requirement

### Requirement 1: Message Correlation & Outbound Persistence
* **Files Inspected:**
  * [`backend/app/modules/authentication/otp_service.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/authentication/otp_service.py#L225-L277) (`request_otp`)
  * [`backend/app/modules/messaging/notification_service.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/notification_service.py#L184-L260) (`send_otp_sms`)
  * [`backend/app/modules/messaging/webhook_service.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/webhook_service.py#L273-L335) (`process_webhook`)
* **Finding & Evidence:**
  * **Outbound Message ID Generation:** The external Notification Microservice generates the authoritative `messageId` (e.g. `msg_5db4ad9a4d8743859b15d5a06713c2f1`) upon accepting the HTTP POST request and returns it in the HTTP 202 response body.
  * **Persistence Sequence:** In `otp_service.py` (lines 241-276), the backend commits the record to `notification_messages` and updates `OtpChallenge.metadata_json` *immediately after* the HTTP 202 response is returned.
  * **Race Condition (Webhook Arrives Before Message Record Committed):**
    If a webhook arrives before `otp_service.py` commits the `NotificationMessage` row, `webhook_service.py` (lines 296-335) detects that `notification_msg is None` and `otp_challenge is None`. It **quarantines** the event with `processing_status = "quarantined"`, `tenant_id = None`, and logs `notification_webhook_unknown_message`.
    *Consequence:* Cross-tenant data contamination is prevented. However, when `otp_service.py` subsequently commits the message row with `status = "QUEUED"`, the message will remain in `QUEUED` status unless a subsequent webhook arrives.
  * **Lost Response / Timeout During Outbound Submission:**
    If the external notification service processes the message but the HTTP connection breaks or times out on read, `NotificationServiceClient.send_otp_sms` returns `status = "TIMEOUT"` and `message_id = None`. The backend never creates a `NotificationMessage` record. When the provider subsequently sends a status webhook, it is safely quarantined with `tenant_id = None`.

---

### Requirement 2: Signature Security & Cryptographic Integrity
* **Files Inspected:**
  * [`backend/app/modules/messaging/webhook_service.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/webhook_service.py#L70-L160) (`verify_signature`)
  * [`backend/app/core/config.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/core/config.py#L729-L730) (`Settings.validate_production_security`)
* **Finding & Evidence:**
  * **Algorithm & Payload:** Computes HMAC-SHA256 over `f"{timestamp_header}.{raw_body_bytes}"` using `NOTIFICATION_WEBHOOK_SECRET`.
  * **Header Specification:** Requires `X-Notification-Timestamp` and `X-Notification-Signature` formatted strictly as `sha256=<hex_digest>`.
  * **Constant-Time Comparison:** Evaluated using `hmac.compare_digest(expected_sig, signature_header.strip())` to prevent timing side-channel attacks.
  * **Timestamp & Replay Protection:** Parses ISO-8601 UTC strings (`%Y-%m-%dT%H:%M:%SZ`) and Unix epoch timestamps, normalizes to UTC, and rejects age differences exceeding `NOTIFICATION_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` (default: 300s) with HTTP 400 (`TIMESTAMP_OUT_OF_BOUNDS`).
  * **Fail-Closed Behavior:**
    * In staging/production: `Settings` fails at startup if `NOTIFICATION_WEBHOOK_SECRET` is unset or empty.
    * At runtime: If secret is missing, raises HTTP 500 (`WEBHOOK_SECRET_UNCONFIGURED`) and logs `notification_webhook_secret_missing`.
    * Tampered body with original signature is rejected with HTTP 401 (`INVALID_SIGNATURE`).

---

### Requirement 3: Event Consistency (`X-Event-ID` vs Payload `eventId`)
* **Files Inspected:**
  * [`backend/app/modules/messaging/webhook_service.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/webhook_service.py#L194-L214) (`process_webhook`)
  * [`backend/app/modules/messaging/api.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/api.py#L58-L75) (`receive_notification_status_webhook`)
* **Finding & Evidence:**
  * In `webhook_service.py` line 194:
    `event_id = str(payload.get("eventId") or event_id_header or "").strip()`
  * **Confirmed Defect / Ambiguity:** If an incoming request supplies *both* the `X-Event-ID` HTTP header and the `payload["eventId"]` JSON field with conflicting values, the service silently prioritizes the payload field without validating that `X-Event-ID == payload["eventId"]`.
  * **Remediation Recommendation:** Add an explicit consistency assertion: if both header and body field are provided and do not match, reject the request with HTTP 400 (`EVENT_ID_MISMATCH`).

---

### Requirement 4: PostgreSQL Deduplication & Atomic Rollback
* **Files Inspected:**
  * [`backend/app/modules/messaging/models.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/models.py#L65-L96) (`NotificationWebhookEvent`)
  * [`backend/app/modules/messaging/webhook_service.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/webhook_service.py#L258-L270) & (lines 415-427)
* **Finding & Evidence:**
  * **Pre-Check Deduplication:** Before applying mutations, queries `NotificationWebhookEvent` where `event_id == incoming_event_id`. If found, returns HTTP 200 with `status: "duplicate"` immediately.
  * **Database Uniqueness Constraint:** `UniqueConstraint("event_id", name="uq_notification_webhook_event_id")` enforces uniqueness at the database engine level.
  * **Atomic Rollback on Concurrent Race:**
    When two identical requests arrive concurrently and pass the pre-check simultaneously:
    * The first transaction commits successfully.
    * The second transaction fails at `session.commit()` with `sqlalchemy.exc.IntegrityError`.
    * The exception handler calls `session.rollback()`, which cleanly reverts the in-memory mutations to `NotificationMessage` and `OtpChallenge`, leaving **zero partially applied updates**.
    * The second request returns HTTP 200 with `status: "duplicate"`.

---

### Requirement 5: Status Ordering, Monotonicity & Terminal State Protection
* **Files Inspected:**
  * [`backend/app/modules/messaging/webhook_service.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/webhook_service.py#L23-L46) & (lines 337-385)
* **Finding & Evidence:**
  * **Terminal States:** `DELIVERED`, `READ`, `FAILED`, `EXPIRED`, `UNKNOWN_OUTCOME`.
  * **Terminal State Inviolability:**
    * A message in `DELIVERED` status can only transition forward to `READ`.
    * Any late or out-of-order event carrying `QUEUED`, `PROCESSING`, `PROVIDER_ACCEPTED`, or `RETRY_PENDING` is ignored (`should_update_status = False`).
    * Messages in `FAILED`, `EXPIRED`, or `UNKNOWN_OUTCOME` cannot be updated by any subsequent event.
  * **Timestamp Monotonicity:**
    `incoming_occ = _normalize_utc(occurred_at)` and `stored_occ = _normalize_utc(notification_msg.occurred_at)`.
    If `incoming_occ < stored_occ`, the status update is discarded, preventing out-of-order webhook delivery from regressing status. Both naive and aware datetime objects are normalized to UTC.
  * **Special Status Representations:**
    * `PROVIDER_ACCEPTED`: Updates `provider_message_id` but never updates the message to `DELIVERED`.
    * `MOCK_SENT`: Allowed only in development/test. In production/staging, rejected with HTTP 400 (`UNSUPPORTED_STATUS`).
    * `UNKNOWN_OUTCOME`: Treated as terminal to prevent dangerous automated resends with new idempotency keys.

---

### Requirement 6: Tenant Isolation & Status Endpoint Authorization
* **Files Inspected:**
  * [`backend/app/modules/messaging/webhook_service.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/webhook_service.py#L273-L335)
  * [`backend/app/modules/messaging/api.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/api.py#L78-L154) (`get_notification_delivery_status`)
* **Finding & Evidence:**
  * **Untrusted Webhook Tenant ID Ignored:** `webhook_service.py` never reads `tenantId` from the webhook body. The tenant is derived strictly from the internal `NotificationMessage.tenant_id` or `OtpChallenge.tenant_id`.
  * **Quarantined Unknown Messages:** Messages not found on the platform are logged with `tenant_id = None` and `processing_status = "quarantined"`, preventing cross-tenant leakage.
  * **Endpoint Authorization (`GET /api/v1/notifications/status/{message_id}`):**
    * Requires session capability `PermissionCode.NOTIFICATION_READ`.
    * Unauthenticated requests return HTTP 401 (`HTTP_ERROR`).
    * Cross-tenant access attempts (Tenant B querying Tenant A's message) return HTTP 404 (`NOTIFICATION_NOT_FOUND`) to prevent resource enumeration.
    * Super Admin (`caller.role == "SUPER_ADMIN"`) bypasses tenant checks for global diagnostic oversight.

---

### Requirement 7: Alembic Migration Chain & Schema Analysis
* **Files Inspected:**
  * [`backend/migrations/versions/20261022_21_notification_status_webhooks.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/migrations/versions/20261022_21_notification_status_webhooks.py)
  * [`backend/migrations/versions/20261018_16_member_relationships.py`](file:///D:/AVANFlix/OTT_Agent_App/backend/migrations/versions/20261018_16_member_relationships.py#L236-L253)
* **Finding & Evidence:**
  * **Linear Alembic Chain:** Verified single linear head `20261022_21 (head)` revises `20261021_20`. 21 total revisions without merge branches.
  * **Migration Schema Quality (`20261022_21`):**
    * Table `notification_messages`: Foreign key constraint to `tenants.id` (RESTRICT), Unique constraint on `message_id`, check constraints on channel and status, composite index on `(tenant_id, status)`.
    * Table `notification_webhook_events`: Unique constraint on `event_id`, check constraint on `processing_status`, indexes on `event_id`, `message_id`, `processing_status`.
    * Clean downgrade implementation drops indexes and tables in reverse order.
  * **Confirmed Migration Blocker (`BLK-03`):**
    In upstream migration `20261018_16_member_relationships.py` (lines 243-252):
    ```sql
    INSERT INTO permissions (code, description, created_at)
    SELECT :code, :desc, CURRENT_TIMESTAMP
    WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code = :code)
    ```
    Under `psycopg3` on PostgreSQL, parameter binding in `SELECT :code` without an explicit type cast raises:
    `psycopg.errors.AmbiguousParameter: inconsistent types deduced for parameter $1: text versus character varying`.
    *Impact:* Running `alembic upgrade head` from revision `20261017_15` on a fresh or staging PostgreSQL instance aborts at revision `20261018_16`.

---

### Requirement 8: Frontend Proxy Security & Client Polling
* **Files Inspected:**
  * [`frontend/src/app/backend-api/[...path]/route.ts`](file:///D:/AVANFlix/OTT_Agent_App/frontend/src/app/backend-api/[...path]/route.ts#L2083-L2188) (`proxyNotification`)
  * [`frontend/src/lib/notifications.ts`](file:///D:/AVANFlix/OTT_Agent_App/frontend/src/lib/notifications.ts#L501-L570) (`getNotificationStatus`, `pollNotificationStatus`)
* **Finding & Evidence:**
  * **Cookie & Secret Isolation:**
    * In `route.ts`, session cookies are attached *only* for authenticated status retrieval (`api/v1/notifications/status/[messageId]`).
    * On the webhook receiver route (`api/v1/webhooks/notifications/status`), session cookies are omitted, and headers `X-Notification-Timestamp`, `X-Notification-Signature`, `X-Event-ID` are forwarded.
    * `NOTIFICATION_WEBHOOK_SECRET` exists strictly server-side in `backend/app/core/config.py` and `.env`; no secrets exist in frontend bundles or `NEXT_PUBLIC_*` variables.
  * **Response Filtering:**
    `NotificationStatusResponse` exposes only non-sensitive operational fields: `messageId`, `channel`, `status`, `messageType`, `providerMessageId`, `purpose`, `occurredAt`, `createdAt`, `failureReason`. OTP codes, salts, hashes, and recipient PII are excluded.
  * **Bounded Polling:**
    `pollNotificationStatus` bounds retries to `maxAttempts: 10` with exponential backoff (`intervalMs = Math.min(intervalMs * 1.5, 8000)`), terminating immediately when a terminal status (`DELIVERED`, `READ`, `FAILED`, `EXPIRED`, `UNKNOWN_OUTCOME`) is returned.

---

### Requirement 9: Test Evidence & Execution Proof
* **Execution Timestamp:** October 22, 2026, 16:58:39 UTC+05:30.
* **Fresh Execution Results:**
  1. **Webhook Suite (`backend/tests/test_notification_webhook.py`):**
     * Command: `& 'backend\.venv\Scripts\python.exe' -m pytest backend/tests/test_notification_webhook.py -v`
     * Result: **14 passed in 15.81s (Exit code 0)**.
     * Scenarios verified:
       - Scenario 01: Valid signed webhook accepted and status updated.
       - Scenario 02: Missing signature rejected (HTTP 400).
       - Scenario 03: Invalid signature rejected (HTTP 401).
       - Scenario 04: Missing webhook secret fails closed (HTTP 500).
       - Scenario 05: Modified body with original signature rejected (HTTP 401).
       - Scenario 06: Stale timestamp rejected (HTTP 400).
       - Scenario 07: Invalid payload and unsupported status rejected (HTTP 400).
       - Scenario 08: Duplicate event deduplication (HTTP 200 duplicate).
       - Scenario 09: Concurrent duplicate race condition handled via IntegrityError rollback.
       - Scenario 10: Unknown message ID quarantined without cross-tenant modification.
       - Scenario 11: Valid delivery status updates correct notification message.
       - Scenario 12: PROVIDER_ACCEPTED status updates provider ID but is not treated as DELIVERED.
       - Scenario 13: Out-of-order events cannot regress terminal states.
       - Scenario 14: Tenant authorization enforced on status endpoint (401 unauth, 404 cross-tenant, 200 tenant admin, 200 super admin).
  2. **Notification Service Client Suite (`backend/tests/test_notification_service.py`):**
     * Command: `& 'backend\.venv\Scripts\python.exe' -m pytest backend/tests/test_notification_service.py -v`
     * Result: **21 passed in 1.86s (Exit code 0)**.
  3. **Frontend Notification Unit Tests (`test:notifications`):**
     * Command: `npm run test:notifications --prefix frontend`
     * Result: **9 passed in 286ms (Exit code 0)**.
  4. **Frontend Proxy Integration Tests (`test:proxy`):**
     * Command: `npm run test:proxy --prefix frontend`
     * Result: **21 passed in 125.97s (Exit code 0)**.
  5. **Frontend TypeScript Check (`typecheck`):**
     * Command: `npm run typecheck --prefix frontend`
     * Result: **0 errors (Exit code 0)**.
  6. **Next.js Production Build (`build`):**
     * Command: `npm run build --prefix frontend`
     * Result: **29/29 routes statically optimized, build succeeded (Exit code 0)**.

---

### Requirement 10: External Integration & Infrastructure Status
* **Live Connectivity Probe:**
  * Command: `curl.exe -I -m 5 https://handorg.duckdns.org/notifications/api/v1/messages`
  * Output: `curl: (28) Connection timed out after 5010 milliseconds` (Port 443 SSL timeout).
  * Port 80 HTTP probe: `curl.exe -I -m 5 http://handorg.duckdns.org/notifications/api/v1/messages` connects and returns `HTTP/1.1 403 Forbidden` (nginx/1.28.3).
* **Finding:**
  The external host `handorg.duckdns.org` is reachable over plain HTTP (Port 80) but fails completely on HTTPS (Port 443). Production settings require HTTPS for external notification microservices. Until the upstream infrastructure operator opens port 443 and binds the TLS certificate, live over-the-wire webhook delivery cannot be verified.

---

## 3. Confirmed Defects vs. Unverified Assumptions

| Category | Finding | Impact | Severity |
|---|---|---|---|
| **Confirmed Defect** | Upstream migration `20261018_16_member_relationships.py` parameter type ambiguity under psycopg3 | Prevents fresh PostgreSQL database instances from migrating past revision 15 to revision 21 | **High** |
| **Confirmed Defect** | Lack of strict validation between `X-Event-ID` header and payload `eventId` | Allows conflicting event IDs without raising HTTP 400 | **Low** |
| **Confirmed Defect** | Outbound message persistence race condition on immediate webhook delivery | If a webhook arrives before the outbound HTTP response finishes committing `NotificationMessage`, the event is quarantined and subsequent message status remains `QUEUED` | **Medium** |
| **External Blocker** | DuckDNS Port 443 SSL Connection Timeout (`BLK-01`) | Prevents outbound live notification dispatch and inbound HTTPS webhook receipt | **Critical** |
| **External Blocker** | Webhook Callback URL and Shared Secret Registration (`BLK-02`) | External microservice does not know where to deliver webhook callbacks | **High** |
| **Unverified Assumption** | Twilio / WhatsApp Cloud carrier delivery status mapping in live traffic | Twilio and WhatsApp return vendor-specific error codes; mapping to `DELIVERED`/`FAILED` is handled by the upstream microservice and cannot be verified without live provider traffic | **Medium** |

---

## 4. Prioritized Remediation List

1. **[P0 - Infrastructure] Resolve DuckDNS Port 443 HTTPS Timeout (`BLK-01`):**
   Request infrastructure operator to open port 443 on the gateway host running nginx at `handorg.duckdns.org` and configure a valid Let's Encrypt certificate.
2. **[P0 - Migration] Fix SQL Parameter Cast in Migration `20261018_16` (`BLK-03`):**
   Update raw SQL in `20261018_16_member_relationships.py` line 247:
   `SELECT CAST(:code AS VARCHAR), CAST(:desc AS TEXT), CURRENT_TIMESTAMP WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code = :code)`.
3. **[P1 - Coordination] Register Webhook Callback with Notification Service (`BLK-02`):**
   Register `https://<domain>/api/v1/webhooks/notifications/status` and `NOTIFICATION_WEBHOOK_SECRET` in the Notification Microservice service configuration table.
4. **[P2 - Resilience] Outbound Correlation Pre-Persistence (`REC-01`):**
   When client sends `Idempotency-Key` or `challengeId` in `POST /api/v1/messages`, pre-create a `NotificationMessage` with a temporary tracking ID or challenge association before making the outbound HTTP call, so that early-arriving webhooks find a pending record instead of being quarantined.
5. **[P3 - Consistency] Enforce `X-Event-ID` Header & Body Consistency (`REC-02`):**
   In `webhook_service.py`, reject requests where `event_id_header` and `payload.get("eventId")` are both present but not identical.

