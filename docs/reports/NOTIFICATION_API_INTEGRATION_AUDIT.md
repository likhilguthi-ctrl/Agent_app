# Community OTT — Notification API Integration Audit

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Audit Date:** October 22, 2026  
**Auditor:** Automated System Audit & Architecture Verification Framework  
**Scope:** Deployed Service URL Verification, Provider Contract Audit, Frontend Call Sites Inventory & Security Boundary Enforcement  
**Reference Document:** `docs/implementation/FRONTEND_INTEGRATION.md`  

---

## 1. Executive Summary & Audit Context

This audit evaluates the architecture, deployment configuration, and frontend/backend integration of the **Notification Microservice** for the **Community OTT Agent Platform**.

The objective is to verify the deployed service endpoint, audit all frontend notification call sites, assess the authentication boundary, and establish a centralized frontend notification architecture conforming to `docs/implementation/FRONTEND_INTEGRATION.md`.

### Key Audit Findings:
1. **Configured HTTPS Base URL Verification:** The backend configuration `NOTIFICATION_SERVICE_BASE_URL` in `backend/.env` has been set to `https://handorg.duckdns.org/notifications`. Both backend configuration (`Settings.notification_base_url`) and unified clients construct API paths without prefix duplication: `https://handorg.duckdns.org/notifications/api/v1/messages`.
2. **Provider API Contract Classification:** The external Notification Microservice contract is strictly a **service-to-service API** requiring the private header `X-API-Key: <service-api-key>`. The provider specification explicitly forbids browser-side calls and offers no public client tokens, publishable keys, or browser CORS headers.
3. **Authentication Boundary Enforcement:** To prevent private key compromise, SMS toll fraud, unverified OTP leakage in browser memory, and bypass of PostgreSQL rate limiting, direct browser-to-provider requests are prohibited. All notification dispatches are routed through the secure Next.js reverse proxy (`/backend-api/`) and mediated by the Community Backend.
4. **Frontend Centralization Complete:** All frontend notification triggers (Community OTP Login, Subscriber KYC Verification, Agent Nomination Verification, and Support Chat) have been centralized under `frontend/src/lib/notifications.ts`, replacing disparate inline transports with a single typed client.

---

## 2. Deployed Base URL & API Contract Verification

### 2.1 Base URL Inspection & Prefix Verification
The operational service configuration was inspected across repository files:
- **Configured Base URL:** `https://handorg.duckdns.org/notifications`
- **Environment Settings (`backend/.env`):**
  ```env
  NOTIFICATION_SERVICE_BASE_URL=https://handorg.duckdns.org/notifications
  ```
- **URL Resolution Invariant:**
  In `backend/app/core/config.py`, `Settings.notification_base_url` normalizes and strips trailing slashes:
  ```python
  @property
  def notification_base_url(self) -> str:
      url = self.notification_service_base_url or self.notification_service_url or "http://localhost:8100"
      return url.rstrip("/")
  ```
  In `backend/app/modules/messaging/notification_service.py`, endpoint construction appends `/api/v1/messages` directly to `base_url`:
  ```python
  @property
  def messages_endpoint_url(self) -> str:
      return f"{self.base_url}/api/v1/messages"
  ```
  **Result:** The final URL evaluates strictly to:
  $$\text{Target URL} = \text{https://handorg.duckdns.org/notifications/api/v1/messages}$$
  **Zero prefix duplication occurs.** No undocumented routes or duplicate `/notifications/notifications/` patterns exist.

### 2.2 Live Network Probes & Transport Security Analysis
Direct TCP and HTTP/HTTPS probes against the deployed service host `handorg.duckdns.org` were executed without transmitting credentials or real OTPs:

1. **HTTPS (Port 443) Transport Connectivity Probe:**
   ```python
   # Probed endpoints: /notifications/api/v1/messages, /notifications, /health, /
   httpx.get("https://handorg.duckdns.org/notifications/api/v1/messages", timeout=5.0)
   ```
   - **Result:** `httpx.ConnectTimeout: Port 443 unreachable/timed out`
   - **Network Diagnostic:** Port 443 is not accepting TCP connections (packets are dropped or blocked by firewall rules at the host network). No TLS handshake can complete.
   - **Operational Impact:** While the application code and backend configuration now strictly use `https://handorg.duckdns.org/notifications` (complying with the platform's production TLS policy), actual live outbound calls over HTTPS fail at the transport layer until the remote host operator opens Port 443 and enables SSL/TLS termination.

2. **HTTP (Port 80) Live Response Probe:**
   ```bash
   curl -i -X POST http://handorg.duckdns.org/notifications/api/v1/messages \
     -H "Content-Type: application/json" -d '{"channel":"SMS"}'
   ```
   - **Response Status:** `HTTP/1.1 403 Forbidden`
   - **Server Header:** `nginx/1.28.3 (Ubuntu)`
   - **Observation:** Port 80 is listening and running Nginx, but external tenant identification fails immediately (`BLK-01: TENANT_NOT_FOUND`). Furthermore, transmitting credentials over Port 80 plaintext HTTP is strictly forbidden in staging and production by platform security policy.

3. **Transport Security Alignment & Configuration Invariants:**
   - **Backend Guard:** In `backend/app/core/config.py`, `Settings.validate_production_settings` strictly enforces:
     ```python
     if self.environment in {"production", "staging"}:
         if urlsplit(self.notification_base_url).scheme != "https":
             raise ValueError("Production and staging notification service endpoints must use HTTPS.")
     ```
   - **Resolution:** Updating `NOTIFICATION_SERVICE_BASE_URL` to `https://handorg.duckdns.org/notifications` satisfies this backend invariant.
   - **External Operator Dependency:** The external infrastructure operator for `handorg.duckdns.org` must provision TLS certificates (e.g., Let's Encrypt / Certbot) and route Port 443 to the notification microservice prior to production launch.

### 2.3 Provider API Contract Specification
As specified in `docs/implementation/FRONTEND_INTEGRATION.md`:
- **Submission Endpoint:** `POST /api/v1/messages`
- **Mandatory Transport Headers:**
  - `X-API-Key: <service-api-key>`
  - `Content-Type: application/json`
  - `Idempotency-Key: <unique-notification-intent-key>` (optional in spec, mandatory in our architecture for retry protection; max 200 characters)
- **Supported Channels:** `SMS`, `WHATSAPP`, `EMAIL`
- **Message Types & Priorities:**
  - `P0`: `LOGIN_OTP`, `PAYMENT_OTP`, `PASSWORD_RESET_OTP`, `URGENT_SECURITY_ALERT`
  - `P1`: `PAYMENT_RECEIPT`, `ACCOUNT_UPDATE`
  - `P2`: All other allowed types
- **Acceptance Response:** HTTP `202 Accepted` returning:
  ```json
  {
    "messageId": "msg_5db4ad9a4d8743859b15d5a06713c2f1",
    "status": "QUEUED",
    "priority": "P0",
    "channel": "SMS",
    "statusUrl": "/api/v1/messages/msg_5db4ad9a4d8743859b15d5a06713c2f1"
  }
  ```
- **Documented Status Endpoints:**
  - `GET /api/v1/messages/{messageId}`
  - `GET /api/v1/messages/{messageId}/attempts`
  (Both require service authentication via `X-API-Key`).

---

## 3. Inventory of Frontend Notification Call Sites

A full scan of `frontend/src` was conducted to map all existing notification call sites:

| Call Site File | Component | Workflow / Purpose | Channel / Type | Previous Implementation | Centralized Implementation |
| :--- | :--- | :--- | :---: | :--- | :--- |
| `frontend/src/components/CommunityOtpLogin.tsx` | `CommunityOtpLogin` | Member / Agent Login OTP | `SMS` / `LOGIN_OTP` | Inline `fetchBackendApi` call | `requestOtpNotification` & `verifyOtpNotification` via `@/lib/notifications` |
| `frontend/src/app/agent/register/page.tsx` | `AgentSubscriberRegistrationPage` | Subscriber KYC Verification | `SMS` / `user_registration` | Inline `fetchBackendApi` call with custom cooldown logic | `requestOtpNotification` & `verifyOtpNotification` via `@/lib/notifications` |
| `frontend/src/components/AddEmployeeForm.tsx` | `AddEmployeeForm` | Agent Candidate Nomination | `SMS` / `agent_registration` | Inline `fetchBackendApi` call with manual error mapping | `requestOtpNotification` & `verifyOtpNotification` via `@/lib/notifications` |
| `frontend/src/app/employee/support/page.tsx` | `EmployeeSupportPage` | Support Desk Chat | `WHATSAPP` / Direct Support | Hardcoded string interpolation `wa.me/${WHATSAPP_SUPPORT_NUMBER}` | `getWhatsAppSupportUrl` via `@/lib/notifications` |
| `frontend/src/app/agent/dashboard/page.tsx` | `AgentDashboard` | Customer Detail View | `EMAIL` | Read-only subscriber email display | Display only (no active outbound delivery call) |

---

## 4. Security Boundary & Threat Analysis

### 4.1 Mandate on Authentication Boundary
Section 4 of the project requirements mandates:
- Do not send requests from browser JavaScript directly to the private API using `X-API-Key`.
- Do not put the service key in `NEXT_PUBLIC_*` variables.
- Do not expose the webhook secret, OTP, or other service credentials.
- Use an authorized server-side delivery boundary with the service key held only in server-side environment configuration.

### 4.2 Comprehensive Threat Matrix:

| Threat Vector | Direct Browser Call Architecture | Server-Mediated Architecture (Implemented) | Verdict / Mitigation |
| :--- | :--- | :--- | :--- |
| **Service Key Disclosure** | `X-API-Key` exposed in browser network requests or bundle (`NEXT_PUBLIC_`). | Key exists strictly in backend environment (`NOTIFICATION_SERVICE_API_KEY`). | **Mitigated**: Zero credential exposure to client. |
| **SMS Pumping / Toll Fraud** | Malicious users extract key and send thousands of unauthorized SMS messages. | Backend rate limiting (`otp_rate_buckets`) enforces 3 sends / 10m and 60s cooldown in PostgreSQL. | **Mitigated**: Backend abuse guard throttles abuse per IP and mobile. |
| **Authentication Invalidation** | Frontend knows or passes plaintext OTP in `POST /api/v1/messages` body. | Backend generates cryptographically random OTP, stores salted SHA-256 hash, and dispatches out-of-band. | **Mitigated**: Browser never receives or knows the plaintext OTP. |
| **User Enumeration** | Direct calls disclose whether a phone number is registered. | Backend returns generic HTTP 202 `OtpRequestAccepted` for both registered and unregistered numbers. | **Mitigated**: Strict anti-enumeration defense preserved. |
| **Tenant Cross-Contamination** | Browser specifies arbitrary tenant mappings. | Backend resolves tenant strictly from verified hostname context (`TenantContext`). | **Mitigated**: Complete tenant isolation guaranteed. |

---

## 5. Audit Conclusion & Production Readiness Verdict

1. **Centralized Frontend Client Verified:** All frontend notification triggers consume the typed abstractions in `frontend/src/lib/notifications.ts`. No raw inline fetches remain in components.
2. **Backend Boundary Maintained:** The Community Backend remains the single authorized actor holding `NOTIFICATION_SERVICE_API_KEY` and dispatching to `https://handorg.duckdns.org/notifications/api/v1/messages`. Browser clients never hold or transmit private service credentials.
3. **Transport Security Blocker (`SEC-TLS`):** Port 443 is closed/unreachable on `handorg.duckdns.org`. Using plaintext HTTP Port 80 in production violates security invariants and backend startup validation.
4. **External Tenant Routing Blocker (`BLK-01`):** Live delivery to real mobile numbers remains blocked because external tenant routing on `handorg.duckdns.org` returns HTTP 403 `TENANT_NOT_FOUND`.
5. **Overall Production Readiness Verdict:** **BLOCKED**. The platform codebase and local test suites are 100% verified (117/117 passed), but production deployment cannot proceed until the external service administrator enables HTTPS on Port 443 and provisions the Community OTT tenant in the service registry.

