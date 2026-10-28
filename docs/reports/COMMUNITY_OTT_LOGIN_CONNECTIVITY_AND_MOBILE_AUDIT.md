# Community OTT — Login Connectivity, Backend Proxy & Mobile Identity Persistence Audit

**Date:** 2026-10-23  
**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Status:** Remediated & Fully Verified  
**Live Environment:** Next.js Frontend (Port 3000) & FastAPI Backend (Port 8000)  
**Database:** PostgreSQL (`avanflix` / `community_ott_test`)

---

## 1. Executive Summary & Root Cause Analysis

### 1.1 The Incident
During login testing, the frontend displayed:
```
Authentication failed. Please verify your username and password.
```
While inspection of the network traffic revealed:
```json
HTTP/1.1 503 Service Unavailable
{"error": "BACKEND_PROXY_UNAVAILABLE"}
```
Although frontend mock unit tests previously reported green test runs, live end-to-end login via the Next.js reverse proxy was failing before authentication could complete. Furthermore, the persistence and resolution of mobile numbers across `SUPER_ADMIN`, `ADMIN`, and `AGENT` roles in PostgreSQL required formal architectural verification.

### 1.2 Root Cause Analysis

Through step-by-step diagnostic tracing across `frontend/src/app/backend-api/[...path]/route.ts`, `frontend/src/app/login/page.tsx`, and the FastAPI backend `backend/app/modules/authentication/api.py`, three interrelated defects were identified:

1. **Premature Cookie Enforcement on Multi-Step 2FA Login in Proxy (`route.ts`):**
   - **Mechanism:** In the legacy single-step password login, `POST /api/v1/auth/login` always established an authenticated session and returned `Set-Cookie` headers. To prevent phantom logins or misconfigured upstream responses, `proxyCommunityAuth` in `route.ts` had a safeguard:
     ```typescript
     else if (successfulAuth && routePath.endsWith("/login")) {
       return unavailableResponse();
     }
     ```
   - **Defect:** With the introduction of multi-step Two-Factor Authentication (Stage 1), Step 1 (`two_factor: true`) verifies the operator's credentials and returns `HTTP 200 OK` with payload:
     ```json
     {"status": "otp_required", "challenge_id": "...", "masked_mobile": "+91 91002 ****05", "cooldown_seconds": 60}
     ```
     At this stage, no session cookie is minted yet (cookies are only set in Step 2 upon OTP verification). Because `incomingCookies.length === 0`, line 2501 was triggered, converting the legitimate `200 OK` response into `503 Service Unavailable` with `BACKEND_PROXY_UNAVAILABLE`.

2. **Loopback Host/Origin Mismatch in Development Proxy Guard (`route.ts`):**
   - **Mechanism:** `isLocalDevelopmentRequest` compared `parsedOrigin.host.toLowerCase() === requestHost.toLowerCase()`.
   - **Defect:** When a browser accessed the application via `http://localhost:3000` while an automated fetch or origin header arrived as `http://127.0.0.1:3000` (or vice-versa), string comparison failed, rejecting development loopback requests as untrusted cross-origin requests.

3. **Generic Error Masking in Frontend UI (`frontend/src/app/login/page.tsx`):**
   - **Mechanism:** `handleCredentialsSubmit` in `page.tsx` checked `if (res.status === 401)` and `else if (res.status === 429)`. Any other non-OK status (including `502`, `503`, and `504`) fell through to:
     ```typescript
     setErrorMessage("Authentication failed. Please verify your username and password.");
     ```
   - **Impact:** Upstream proxy outages (`503 BACKEND_PROXY_UNAVAILABLE`) or network timeouts were falsely presented to users and operators as "invalid username and password", concealing the operational fault.

---

## 2. Remediations Applied

### 2.1 Backend Proxy Route (`frontend/src/app/backend-api/[...path]/route.ts`)
- **Step 1 Passthrough:** Removed the blanket `else if (successfulAuth && routePath.endsWith("/login")) { return unavailableResponse(); }` requirement, permitting legitimate Step 1 `status: "otp_required"` responses without session cookies to pass through to the frontend.
- **Enhanced Loopback Matching:** Updated `isLocalDevelopmentRequest` to verify loopback equivalence across `localhost` and `127.0.0.1` on matching ports:
  ```typescript
  const bothLoopback = (
    (parsedOrigin.hostname === "localhost" || parsedOrigin.hostname === "127.0.0.1") &&
    (hostName === "localhost" || hostName === "127.0.0.1")
  );
  if (bothLoopback && (parsedOrigin.port || "80") === (requestPort || "80")) {
    return true;
  }
  ```
- **Preserved Proxy Security Boundaries:**
  - Upstream target remains strictly hardcoded to `BACKEND_API_BASE_URL` (default `http://127.0.0.1:8000`).
  - No client-controlled host headers or URLs can redirect upstream forwarding.
  - OIDC session cookies and community session cookies remain strictly separated.
  - Route allowlist remains enforced; arbitrary endpoints outside the allowlist return `404 Not Found`.

### 2.2 Frontend Error Mapping (`frontend/src/app/login/page.tsx`)
- Updated `handleCredentialsSubmit`, `handleVerifyOtp`, `handlePasswordChange`, `handleResendOtp`, and `handleSelectContext` to explicitly differentiate server/proxy unavailability (`502`, `503`, `504`):
  ```typescript
  } else if (res.status === 502 || res.status === 503 || res.status === 504) {
    setErrorMessage("Authentication service is temporarily unavailable. Please try again later.");
  }
  ```
- Users now receive accurate operational guidance during transient outages, while credential errors (`401`) continue to indicate invalid input.

---

## 3. PostgreSQL Mobile Identity & Persistence Architecture Audit

A thorough audit of the database schema, models, and authentication repository was conducted to verify where canonical identities and mobile numbers reside across all user roles.

### 3.1 Role-by-Role Schema Breakdown

| Role | Canonical Identity Table | Mobile Column & Constraints | Credential Storage Table | Role Association |
| :--- | :--- | :--- | :--- | :--- |
| **SUPER_ADMIN** | `platform_operators` | `mobile_e164` (`VARCHAR(32)`, `UNIQUE`, `NOT NULL`) | `account_credentials` (`account_type='platform_operator'`, `operator_id=platform_operators.id`) | Inherent in `platform_operators` record |
| **ADMIN** | `users` & `tenant_memberships` | `tenant_memberships.mobile_e164` (`VARCHAR(32)`, `NOT NULL`, regex check `^\+[1-9]\d{1,14}$`, unique per tenant `uq_tenant_memberships_tenant_mobile`) | `account_credentials` (`account_type='tenant_user'`, `user_id=users.id`) | `user_role_assignments` linked to `roles.code = 'admin'` |
| **AGENT** | `users`, `tenant_memberships`, & `agent_profiles` | `tenant_memberships.mobile_e164` (`VARCHAR(32)`, `NOT NULL`) | `account_credentials` (`account_type='tenant_user'`, `user_id=users.id`) | `user_role_assignments` linked to `roles.code = 'agent'` |

### 3.2 Persistence Guarantees
- **Durable Storage in PostgreSQL:** Mobile numbers and operator records are stored exclusively in PostgreSQL relational tables (`platform_operators` and `tenant_memberships`). No MongoDB, in-memory caches, or client storage is used.
- **Persistence Across Restart:** Tables are governed by Alembic migrations (`20261016_01_core_identity_schema.py` through `20261023_22_account_credentials_must_change_password.py`). Database restarts retain all records, constraints, and audit trails.
- **E.164 Normalization:** Mobile numbers are strictly normalized to standard E.164 format (e.g., `+919100209205`) on write, preventing formatting discrepancies from causing delivery or lookup failures.

### 3.3 Server-Side Resolution in Authentication Flow
In `backend/app/modules/authentication/repository.py`:
- In `verify_credentials_for_otp()`, after the user's password is confirmed against `account_credentials.password_hash` via `scrypt`:
  1. The server queries `PlatformOperator` (if platform operator) or `TenantMembership` (if tenant user) directly in PostgreSQL using the authenticated entity's primary key.
  2. The registered `mobile_e164` is extracted from the database record.
  3. **Strict Security Invariant:** Any mobile number supplied by the client in the login request is **completely ignored**. The OTP is dispatched exclusively to the number on file in PostgreSQL.
  4. The returned response contains only the masked mobile number (e.g. `+91 91002 ****05`) to prevent data leakage and enumeration attacks.

---

## 4. Verification Evidence

### 4.1 Live Proxy End-to-End Verification (Ports 3000 -> 8000)

Using the live running Next.js application on `http://127.0.0.1:3000` connected to FastAPI on `http://127.0.0.1:8000`:

#### Test 1: Two-Factor Step 1 — Credential Verification & OTP Dispatch
- **Endpoint:** `POST http://127.0.0.1:3000/backend-api/api/v1/auth/login`
- **Request:**
  ```json
  {"username": "super_admin", "password": "SuperAdmin@123", "two_factor": true}
  ```
- **Response:** `HTTP 200 OK`
- **Body:**
  ```json
  {
    "status": "otp_required",
    "challenge_id": "ae1141b9-7906-488c-a09d-2a3bb19d56c8",
    "masked_mobile": "+91 91002 ****05",
    "cooldown_seconds": 60
  }
  ```
- **Outcome:** Passed. Challenge created, registered PostgreSQL mobile resolved, proxy passed response with 200 OK without requiring Set-Cookie.

#### Test 2: Two-Factor Step 2 — OTP Verification & Session Issuance
- **Endpoint:** `POST http://127.0.0.1:3000/backend-api/api/v1/auth/login`
- **Request:**
  ```json
  {
    "username": "super_admin",
    "password": "SuperAdmin@123",
    "challenge_id": "ae1141b9-7906-488c-a09d-2a3bb19d56c8",
    "otp": "123456"
  }
  ```
- **Response:** `HTTP 200 OK`
- **Set-Cookie Headers Issued:**
  - `community_session=Q6xLAymd...; Path=/backend-api/api/v1/auth; HttpOnly; SameSite=lax; Max-Age=43200`
  - `community_platform_session=Q6xLAymd...; Path=/backend-api/api/v1/platform; HttpOnly; SameSite=lax; Max-Age=3600`
- **Body:**
  ```json
  {
    "user_id": "e2742846-b78e-4995-a077-bb06ffe90ad9",
    "username": "super_admin",
    "role": "SUPER_ADMIN",
    "status": "active",
    "csrf_token": "72af87ffd95de8dacb0e4056701b4ead...",
    "must_change_password": true,
    "context": {"type": "platform", "role": "SUPER_ADMIN"}
  }
  ```
- **Outcome:** Passed. Both session cookies established and properly scoped.

#### Test 3: Authenticated Session Resolution via Proxy
- **Endpoint:** `GET http://127.0.0.1:3000/backend-api/api/v1/auth/me`
- **Headers:** `Cookie: community_session=...; community_platform_session=...`
- **Response:** `HTTP 200 OK`
- **Body:**
  ```json
  {
    "user_id": "e2742846-b78e-4995-a077-bb06ffe90ad9",
    "username": "super_admin",
    "role": "SUPER_ADMIN",
    "status": "active",
    "must_change_password": true,
    "context": {"type": "platform", "role": "SUPER_ADMIN"}
  }
  ```
- **Outcome:** Passed. Authenticated operator identity resolved cleanly via reverse proxy.

#### Test 4: Invalid Credentials Rejection
- **Endpoint:** `POST http://127.0.0.1:3000/backend-api/api/v1/auth/login`
- **Payload:** `{"username": "super_admin", "password": "WrongPassword123!", "two_factor": true}`
- **Response:** `HTTP 401 Unauthorized`
- **Body:**
  ```json
  {"error": {"code": "AUTHENTICATION_FAILED", "message": "Login could not be completed."}}
  ```
- **Outcome:** Passed. Rejected without exposing enumeration details.

#### Test 5: Invalid OTP Verification Rejection
- **Endpoint:** `POST http://127.0.0.1:3000/backend-api/api/v1/auth/login`
- **Payload:** `{"username": "super_admin", "password": "SuperAdmin@123", "challenge_id": "00000000-0000-0000-0000-000000000000", "otp": "999999"}`
- **Response:** `HTTP 401 Unauthorized`
- **Outcome:** Passed. Single-use and unknown challenges rejected.

---

### 4.2 Automated Test Suite Results

All automated test suites were executed in the environment and achieved 100% pass rates:

| Test Suite | Command | Result | Duration |
| :--- | :--- | :--- | :--- |
| **Backend Auth & Storage (pytest)** | `python -m pytest backend/tests/test_super_admin_login_and_bootstrap.py backend/tests/test_cloudflare_r2_storage.py backend/tests/test_registration_photos.py backend/tests/test_authentication_api.py backend/tests/test_authentication_providers.py backend/tests/test_otp_login_flow.py backend/tests/test_password_authentication.py` | **87 / 87 PASSED** (0 failures) | 82.36s |
| **Frontend Proxy Integration (node)** | `node --test frontend/test/proxy-integration.test.mjs` | **21 / 21 PASSED** (0 failures) | 68.90s |
| **Frontend Notification Client (node)** | `node --test frontend/test/notification-client.test.mjs` | **9 / 9 PASSED** (0 failures) | 0.34s |
| **Frontend Mobile OTP Auth (node)** | `node --test frontend/test/mobile-otp-auth.test.mjs` | **4 / 4 PASSED** (0 failures) | 1.57s |
| **Frontend TypeScript Validation** | `npm --prefix frontend run typecheck` | **PASSED** (exit code 0) | 14.2s |
| **Frontend Production Build** | `npm --prefix frontend run build` | **PASSED** (exit code 0, 29/29 pages) | 76.5s |

**Total Automated Tests Verified:** 121 / 121 passing.

---

## 5. Security & Operational Summary

1. **Backend Proxy Reliability:** The Next.js API proxy (`/backend-api/[...path]`) correctly proxies both multi-step OTP authentication flows and direct session issuance without false `503 BACKEND_PROXY_UNAVAILABLE` errors.
2. **PostgreSQL as Canonical Identity Store:** All user identities, credentials, mobile numbers, and roles reside durably in PostgreSQL with referential integrity constraints, E.164 normalization, and unique indexes.
3. **Defense-in-Depth OTP Security:**
   - Client-provided mobile numbers during login are completely ignored.
   - OTP codes are hashed in `otp_challenges`, single-use, and time-bounded.
   - Failed attempts decrement attempts remaining; 3 failed attempts invalidate the challenge.
   - Anti-enumeration timing defense protects against username harvesting.
   - Initial super admin login strictly enforces temporary password replacement (`must_change_password`) before platform access.
4. **Cloudflare R2 Ready:** Object storage service implements content-type validation, magic byte signature inspection, presigned expiring URLs, non-blocking asynchronous threadpool transfers, and strict tenant isolation.

