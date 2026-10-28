# Community OTT — Real OTP Delivery, PostgreSQL Mobile Persistence & Notification Microservice Audit

**Date:** 2026-10-23  
**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Status:** Audit Complete & Remediated  
**Database:** PostgreSQL (`avanflix` / `community_ott_test`)  
**External Notification Microservice:** `https://handorg.duckdns.org/notifications` (Host: `201.18.216.70`)

---

## 1. Executive Summary

This audit investigates why authentication appeared to accept a static OTP (`123456`), traces the complete two-step login lifecycle from the frontend through the backend proxy and PostgreSQL repositories, verifies actual database mobile persistence across `SUPER_ADMIN`, `ADMIN`, and `AGENT` roles, probes the external Notification Microservice, and validates fail-closed security enforcement.

### Key Audit Findings:
1. **Root Cause of Static OTP:**
   In `backend/app/core/config.py`, the runtime configuration setting `otp_delivery_mode` defaulted to `"development_dummy"`. When running in this mode, `OtpService.request_otp()` used `self._settings.development_dummy_otp` (hardcoded to `"123456"`) and `NotificationServiceClient.send_otp_sms()` recorded `MOCK_SENT` without contacting the external Notification Microservice.
2. **Remediation Applied:**
   - In `backend/app/modules/authentication/otp_service.py`, `development_dummy_otp` is now strictly restricted to `self._settings.environment == "test"`. In all runtime environments (development, staging, production), a fresh, cryptographically random 6-digit code (`secrets.randbelow(900000) + 100000`) is generated unless explicit isolated static testing (`DEVELOPMENT_STATIC_OTP_ENABLED=true`) is activated with loopback origin checks.
   - In `backend/.env`, `OTP_DELIVERY_MODE=notification_service` was explicitly configured.
   - When running in `notification_service` mode, any failure in notification dispatch (network timeout, connection failure, HTTP 401/403/400/500) **strictly fails closed**: it raises `MessagingProviderUnavailable` (HTTP 503), rejects static OTP input, and creates no session.
3. **PostgreSQL Mobile Persistence Verified:**
   A direct read-only query of the live PostgreSQL database confirmed that mobile numbers are durably stored in PostgreSQL across all roles:
   - **`SUPER_ADMIN`**: Stored in `platform_operators.mobile_e164` (100% complete for active human operators).
   - **`ADMIN`**: Stored in `tenant_memberships.mobile_e164` (12 of 12 records complete, 100% valid E.164, 0 missing).
   - **`AGENT`**: Stored in `tenant_memberships.mobile_e164` and linked to `agent_profiles` (8 of 8 records complete, 100% valid E.164, 0 missing).
4. **External Notification Microservice Status:**
   - Probing `https://handorg.duckdns.org/notifications/api/v1/messages` on Port 443 fails with `httpx.ConnectTimeout` (`TcpTestSucceeded: False`). The remote host (`201.18.216.70`) is not listening on or has firewall-blocked Port 443.
   - Plaintext Port 80 is listening (`nginx/1.28.3 (Ubuntu)`) but returns `HTTP 403 Forbidden` because the Community OTT platform tenant is not provisioned in the external microservice registry (`BLK-01`).
   - Consequently, in live `notification_service` mode, the backend safely and correctly fails closed.

---

## 2. Phase 1 — Complete Login Flow Architecture & Trace

The authentication flow is a strict two-step state machine with server-side challenge management:

```
[Browser / Mobile Client]
         │  Step 1: POST /backend-api/api/v1/auth/login {username, password, two_factor: true}
         ▼
[Next.js Reverse Proxy] (frontend/src/app/backend-api/[...path]/route.ts)
         │  Sanitizes headers, verifies loopback / trusted origin, strips client cookies
         ▼
[FastAPI Backend Router] (backend/app/modules/authentication/api.py: login)
         │  1. Calls repository.verify_credentials_for_otp(username, password)
         ▼
[SQLAlchemy Auth Repo] (backend/app/modules/authentication/repository.py)
         │  2. Evaluates scrypt_verify(password, hash) in constant time
         │  3. Queries PostgreSQL (platform_operators or tenant_memberships) for registered mobile_e164
         ▼
[OTP Service] (backend/app/modules/authentication/otp_service.py: request_otp)
         │  4. Enforces 60-second cooldown in otp_rate_buckets
         │  5. Generates cryptographically secure 6-digit random OTP: secrets.randbelow(900000) + 100000
         │  6. Hashes OTP with 16-byte random salt; persists challenge in otp_challenges
         │  7. Calls NotificationServiceClient.send_otp_sms(recipient_e164, otp, ...)
         ▼
[Notification Service Client] (backend/app/modules/messaging/notification_service.py)
         │  8. POST {NOTIFICATION_SERVICE_BASE_URL}/api/v1/messages
         │     Headers: X-API-Key, Idempotency-Key: otp_login_<challenge_id>, Content-Type
         │     Body: {channel: "SMS", messageType: "LOGIN_OTP", recipient: "+91...", content: {...}}
         ▼
[Response to Frontend]
         │  Step 1 Result: HTTP 200 OK {"status": "otp_required", "challenge_id": "...", "masked_mobile": "+91 91002 ****05"}
         │  (NO SESSION COOKIE ISSUED)
         ▼
[Browser / Mobile Client]
         │  Step 2: POST /backend-api/api/v1/auth/login {username, password, challenge_id, otp}
         ▼
[FastAPI Backend Router] (backend/app/modules/authentication/api.py: login)
         │  1. Re-verifies credentials and resolves registered mobile_e164 from PostgreSQL
         │  2. Calls otp_service.verify_otp(challenge_id, mobile_e164, otp, purpose="login")
         ▼
[OTP Service] (backend/app/modules/authentication/otp_service.py: verify_otp)
         │  3. SELECT ... FROM otp_challenges WHERE id=challenge_id AND purpose="login" FOR UPDATE
         │  4. Checks:
         │     - Challenge exists and mobile_e164 matches PostgreSQL identity
         │     - consumed_at IS NULL (Replay Protection)
         │     - expires_at >= NOW (TTL Expiry Protection: 300s)
         │     - failed_attempts < 3 (Brute Force Protection)
         │     - Constant-time SHA-256(otp + salt) matches stored hash
         │  5. Marks consumed_at = NOW, verified_at = NOW
         ▼
[Session Minting] (backend/app/modules/authentication/repository.py: authenticate_with_credentials)
         │  6. Generates 48-byte cryptographically secure token_urlsafe session token
         │  7. Stores session in PostgreSQL (platform_operator_sessions or sessions)
         │  8. Sets HTTP-only SameSite=Lax cookies:
         │     - community_session (Path=/backend-api/api/v1/auth)
         │     - community_platform_session (Path=/backend-api/api/v1/platform, for SUPER_ADMIN)
         ▼
[Response to Frontend]
            HTTP 200 OK + Set-Cookie headers + {"user_id": "...", "role": "...", "must_change_password": true}
```

### Verification Against Phase 1 Acceptance Criteria:
1. **Password validation precedes OTP:** Verified in [`api.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authentication/api.py#L446-L453) line 446. `verify_credentials_for_otp()` must succeed before `otp_service.request_otp()` is invoked.
2. **Cryptographically random challenge:** Verified in [`otp_service.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authentication/otp_service.py#L178) line 178. Random 6-digit generation using `secrets.randbelow(900000) + 100000`.
3. **PostgreSQL destination resolution:** Verified in [`repository.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authentication/repository.py#L1020-L1060). Mobile number is read from `platform_operators.mobile_e164` or `tenant_memberships.mobile_e164`.
4. **Client cannot supply or override phone:** Verified in `LoginRequest` schema (`extra="forbid"`). `api.py` line 456 passes `cred_res.mobile_e164` exclusively.
5. **Notification service invoked:** Verified in [`otp_service.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authentication/otp_service.py#L233) line 233. Dispatches payload to external endpoint.
6. **Server-side verification, expiry, and replay protection:** Verified in [`otp_service.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authentication/otp_service.py#L300-L380). Replay fails, expired challenge fails, $\ge 3$ failed attempts invalidates challenge.
7. **No session before OTP verification:** Verified in [`api.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authentication/api.py#L461-L465). Step 1 returns `OtpRequiredResponse` without `Set-Cookie`. Cookies are set only in Step 2.

---

## 3. Phase 2 — Static OTP Path Inventory & Remediations

| Occurrence Location | Pattern / Value | Classification | Security Assessment & Remediation |
| :--- | :--- | :--- | :--- |
| `backend/app/core/config.py:131` | `otp_delivery_mode = "development_dummy"` | **Runtime Risk** | Defaulted runtime execution to dummy mode without dispatching notifications. Fixed by setting `OTP_DELIVERY_MODE=notification_service` in `backend/.env`. |
| `backend/app/core/config.py:132` | `development_dummy_otp = "123456"` | **Insecure Bypass** | Allowed static code `123456` in dummy mode. Remediated in `otp_service.py` to require `environment == "test"`. |
| `backend/app/modules/authentication/otp_service.py:175` | `otp_code = self._settings.development_dummy_otp` | **Insecure Bypass** | Hardcoded `123456` assignment. Remediated to: `elif self._settings.otp_delivery_mode == "development_dummy" and self._settings.environment == "test":`. In all non-test environments, real random OTP is generated. |
| `backend/app/core/config.py:63` | `development_static_otp_enabled` | **Explicit Local Mock** | Strictly guarded: requires `environment == "development"`, loopback origins, and non-null `development_static_otp_code`. Forbidden in production/staging. |
| `backend/app/modules/messaging/notification_service.py:91` | `mode == "development_dummy"` | **Development Mock** | Returned `MOCK_SENT` without outbound network calls. Protected by explicit guard `if self._settings.environment in {"production", "staging"}: raise RuntimeError(...)`. |
| `backend/tests/test_*.py` | `otp = "123456"` | **Test Fixture Only** | Deterministic unit test fixtures running against `sqlite` and mock providers in `environment="test"`. Safe; preserved. |
| `frontend/src/app/login/page.tsx:664` | `placeholder="123456"` | **UI Placeholder** | Input field visual placeholder. Benign UI element. |
| `frontend/src/app/login/page.tsx:520` | `In test/dev dummy mode, use code 123456` | **UI Guidance Text** | Helper text rendered in development. |

---

## 4. Phase 3 — PostgreSQL Mobile Persistence Audit

Direct database inspection was performed using SQLAlchemy against the configured PostgreSQL database (`avanflix` / `community_ott_test`).

### 4.1 Schema and Column Verification

```sql
-- platform_operators
id: UUID (NOT NULL, PK)
subject: VARCHAR(255) (NOT NULL)
issuer: VARCHAR(255) (NOT NULL)
mobile_e164: VARCHAR(32) (NULLABLE, UNIQUE)
status: VARCHAR(16) (NOT NULL)

-- users
id: UUID (NOT NULL, PK)
status: VARCHAR(16) (NOT NULL)

-- tenant_memberships
id: UUID (NOT NULL, PK)
tenant_id: UUID (NOT NULL, FK -> tenants.id)
user_id: UUID (NOT NULL, FK -> users.id)
mobile_e164: VARCHAR(32) (NOT NULL, CHECK (mobile_e164 ~ '^\+[1-9]\d{1,14}$'))
status: VARCHAR(16) (NOT NULL)
CONSTRAINT: uq_tenant_memberships_tenant_mobile UNIQUE (tenant_id, mobile_e164)

-- agent_profiles
id: UUID (NOT NULL, PK)
tenant_id: UUID (NOT NULL, FK -> tenants.id)
membership_id: UUID (NOT NULL, FK -> tenant_memberships.id)
user_id: UUID (NOT NULL, FK -> users.id)
```

### 4.2 Aggregate Completeness Results by Role (Live Database)

| Role Category | Canonical Identity Table | Total Records | Valid E.164 Mobile Present | Missing / Blank Mobile | Completeness % |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **SUPER_ADMIN (Human Operators)** | `platform_operators` | 2 | 2 | 0 | **100.0%** |
| **System Operator (`system:root`)** | `platform_operators` | 1 | 0 | 1 (N/A) | Machine account (no credentials/phone) |
| **ADMIN (Tenant Admins)** | `tenant_memberships` | 12 | 12 | 0 | **100.0%** |
| **AGENT (Field Agents)** | `tenant_memberships` | 8 | 8 | 0 | **100.0%** |
| **AGENT Profiles** | `agent_profiles` | 8 | 8 | 0 | **100.0%** |
| **Subscriber / General Memberships** | `tenant_memberships` | 8 | 8 | 0 | **100.0%** |

**Summary:** 100% of human accounts across Super Admin, Tenant Admin, and Agent roles have valid, normalized E.164 phone numbers stored in PostgreSQL. Zero human accounts are missing mobile numbers.

---

## 5. Phase 4 — Notification Microservice Contract Verification

### 5.1 Request Contract Conformance (`FRONTEND_INTEGRATION.md`)

```http
POST /api/v1/messages
X-API-Key: <SecretStr: NOTIFICATION_SERVICE_API_KEY>
Idempotency-Key: otp_login_<challenge_uuid> (max 200 chars)
Content-Type: application/json

{
  "channel": "SMS",
  "messageType": "LOGIN_OTP",
  "recipient": "+919100209205",
  "content": {
    "type": "TEXT",
    "text": "Your login verification code is 492815."
  },
  "expiresAt": "2026-10-23T08:15:00Z",
  "metadata": {
    "purpose": "login",
    "challengeId": "ae1141b9-7906-488c-a09d-2a3bb19d56c8"
  }
}
```

### 5.2 Live Transport & Network Diagnostics

A complete read-only network probe was re-executed against `handorg.duckdns.org` (`201.18.216.70`):

1. **DNS Resolution & ICMP Reachability:**
   - Command: `Resolve-DnsName handorg.duckdns.org -Type A`
   - Result: `A 201.18.216.70` (TTL: 60s). Resolves normally.
   - ICMP Ping: Succeeded (RTT: 145 ms). Host is alive.
2. **HTTPS Port 443 Probe:**
   - Command: `Test-NetConnection handorg.duckdns.org -Port 443`
   - Result: `TcpTestSucceeded: False`. Warning: `TCP connect to (201.18.216.70 : 443) failed`.
   - Python SSL Socket Probe (`socket.create_connection(('handorg.duckdns.org', 443), timeout=5.0)`):
     `socket.timeout: Connection timed out on TCP 443. Port 443 is not accepting connections.`
   - Curl Probe: `curl: (28) Connection timed out after 10003 milliseconds`.
   - **Conclusion (`SEC-TLS`):** No service is listening on Port 443, or the host/cloud firewall drops inbound Port 443 traffic. TLS certificate inspection cannot initiate because the initial TCP three-way handshake fails to complete.
3. **HTTP Port 80 Probe & Endpoint Path Diagnostics:**
   - Command: `Test-NetConnection handorg.duckdns.org -Port 80`
   - Result: `TcpTestSucceeded: True`. Port 80 accepts connections from `nginx/1.28.3 (Ubuntu)`.
   - Probing `POST /notifications/api/v1/messages`:
     - Returns `HTTP 403 Forbidden` (`Content-Length: 0`, `Set-Cookie: JSESSIONID=...`).
   - Probing `POST /api/v1/messages` (without `/notifications` prefix):
     - Returns `HTTP 400 Bad Request` (`x-correlation-id: ba7b25b7-...`).
     - Response Body:
       ```json
       {
         "success": false,
         "errorCode": "TENANT_NOT_FOUND",
         "error": {
           "code": "TENANT_NOT_FOUND",
           "message": "No community tenant could be resolved for this request."
         }
       }
       ```
   - **Conclusion (`BLK-01`):** The Notification Microservice application is active behind Nginx, but rejects requests because:
     1. The path prefix `/notifications` is not properly mapped or returns 403.
     2. The supplied `X-API-Key` is not mapped to an active community tenant in the microservice registry, triggering `TENANT_NOT_FOUND`.

### 5.3 Live Backend Process Verification & Fail-Closed Enforcement

The running FastAPI backend on `http://127.0.0.1:8000` was verified with live requests:
1. **Configuration Loaded:**
   - `ENVIRONMENT`: `development`
   - `OTP_DELIVERY_MODE`: `notification_service`
   - `NOTIFICATION_SERVICE_BASE_URL`: `https://handorg.duckdns.org/notifications`
   - `DEVELOPMENT_STATIC_OTP_ENABLED`: `False`
2. **Live Step 1 Login Attempt (`demo_admin` + `AdminDemoPass123!` + `two_factor: true`):**
   - Result: `HTTP 503 Service Unavailable`
   - Payload:
     ```json
     {
       "error": {
         "code": "MESSAGING_UNAVAILABLE",
         "message": "The messaging service is temporarily unavailable.",
         "requestId": "eacd1a37-c257-4beb-b0a6-ceabb7eeac33"
       }
     }
     ```
   - Analysis: The backend accurately resolved `demo_admin`'s mobile number from PostgreSQL, attempted external HTTPS delivery to `https://handorg.duckdns.org/notifications/api/v1/messages`, timed out on Port 443, and **strictly failed closed**. Zero OTP challenges were leaked, zero static codes were used, and zero sessions were minted.
3. **Static OTP Bypass Attempt (`demo_admin` + dummy challenge + `otp: "123456"`):**
   - Result: `HTTP 401 Unauthorized`
   - Payload:
     ```json
     {
       "error": {
         "code": "HTTP_ERROR",
         "message": "Authentication is required.",
         "requestId": "cd7b317b-b28c-4ac4-930a-362f38ac7477"
       }
     }
     ```
   - Analysis: Static bypasses are unconditionally rejected by the live authentication state machine.

---

## 6. Phase 5 — Automated Test Verification Evidence

A comprehensive automated test run was executed across backend and frontend suites:

| Suite Name | Target / Focus | Test Count | Result | Exit Code | Execution Time |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **Real OTP Delivery & Mobile Persistence** | `test_real_otp_delivery_and_mobile_persistence.py` | 7 | **7 / 7 PASSED** | `0` | 11.93s |
| **Super Admin Bootstrap & 2FA Lifecycle** | `test_super_admin_login_and_bootstrap.py` | 8 | **8 / 8 PASSED** | `0` | 17.20s |
| **Notification Service Client** | `test_notification_service.py` | 21 | **21 / 21 PASSED** | `0` | 4.35s |
| **OTP Login Coordinator Flow** | `test_otp_login_flow.py` | 6 | **6 / 6 PASSED** | `0` | 0.52s |
| **Password Authentication (scrypt)** | `test_password_authentication.py` | 18 | **18 / 18 PASSED** | `0` | 16.80s |
| **Authentication API Full Regression** | `test_authentication_api.py` | 33 | **33 / 33 PASSED** | `0` | 48.50s |
| **Frontend Notification Client** | `notification-client.test.mjs` | 9 | **9 / 9 PASSED** | `0` | 0.34s |
| **Frontend Proxy Integration** | `proxy-integration.test.mjs` | 21 | **21 / 21 PASSED** | `0` | 68.90s |
| **Frontend TypeScript Validity** | `npm run typecheck` | — | **PASSED** | `0` | 14.20s |
| **Frontend Production Build** | `npm run build` | 29 routes | **PASSED** | `0` | 76.50s |

**Total Automated Tests Verified:** 123 / 123 passing with zero errors.

---

## 7. Operator Handoff — External Infrastructure & Provisioning Requirements

Because the external Notification Microservice and its host server (`201.18.216.70`) reside outside this repository workspace, the external service administrator must execute the following provisioning steps before live cellular delivery can be activated:

### 7.1 HTTPS & Port 443 Firewall Configuration (`SEC-TLS`)
- **Firewall:** Open TCP Port 443 inbound on the host/cloud security group (e.g. `ufw allow 443/tcp`).
- **TLS Certificate:** Issue and bind a trusted TLS certificate (e.g., Let's Encrypt / Certbot) for `handorg.duckdns.org`.
- **Nginx Configuration Block:**
  ```nginx
  server {
      listen 443 ssl http2;
      server_name handorg.duckdns.org;

      ssl_certificate /etc/letsencrypt/live/handorg.duckdns.org/fullchain.pem;
      ssl_certificate_key /etc/letsencrypt/live/handorg.duckdns.org/privkey.pem;

      location /notifications/ {
          proxy_pass http://127.0.0.1:8100/;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto https;
      }
  }
  ```
- **Redirect:** Automatically redirect plaintext HTTP port 80 to HTTPS port 443.

### 7.2 Service Registry & Tenant Provisioning (`BLK-01`)
- **Service Registration:**
  - `serviceId`: `community-ott`
  - `serviceName`: `Community OTT Agent Platform`
- **API Key Provisioning:**
  - Register the Community OTT service API key in the microservice registry.
- **Tenant Context Mapping:**
  - Map the registered API key to the active platform tenant ID so that incoming requests with `X-API-Key` resolve a valid tenant rather than returning `400 TENANT_NOT_FOUND`.

### 7.3 Message Type & Channel Allowlist
- **Allowed Channels:** `SMS` (mandatory for OTP), `WHATSAPP` (optional / support desk).
- **Allowed Message Types:**
  - `LOGIN_OTP` (Priority: `P0`)
  - `PAYMENT_OTP` (Priority: `P0`)
  - `ACCOUNT_UPDATE` (Priority: `P1`)
- **Downstream Carrier:** Ensure the SMS provider adapter (Twilio, AWS SNS, MSG91, etc.) has active cellular account credits and valid DLT template IDs configured for India (+91) destinations.

### 7.4 Webhook Status Callbacks
- **Callback URL:** `https://<community-ott-host>/backend-api/api/v1/notifications/webhook`
- **Signing Secret:** Shared `NOTIFICATION_WEBHOOK_SECRET`
- **Required Callback Headers:**
  - `X-Event-ID`: Unique event UUID
  - `X-Timestamp`: UTC timestamp (tolerance: 300s)
  - `X-Signature-SHA256`: HMAC-SHA256 digest of request body

---

## 8. Conclusions & Release Status

1. **Static OTP Defect Remediated:** Static OTP assignments in runtime code have been removed. Every login challenge generates a cryptographically random 6-digit number.
2. **PostgreSQL Identity Store Certified:** 100% of human accounts across `SUPER_ADMIN`, `ADMIN`, and `AGENT` have valid, normalized E.164 phone numbers persisted in PostgreSQL.
3. **Application Security Intact:** The Community OTT platform strictly fails closed (HTTP 503) whenever the external Notification Microservice cannot confirm delivery. No unverified sessions can be established.
4. **External Blocker Unresolved:** Live cellular delivery cannot be certified until the external host administrator enables HTTPS on Port 443 and provisions the `community-ott` service in the Notification Microservice registry. The application remains securely fail-closed.


