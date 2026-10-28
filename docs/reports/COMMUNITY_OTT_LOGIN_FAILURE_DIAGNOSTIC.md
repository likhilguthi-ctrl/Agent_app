# Community OTT — Login Failures Diagnostic & Remediation Report

**Date:** 2026-10-23  
**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Status:** Diagnosed, Remediated & Verified  
**Runtime Components:** Next.js Reverse Proxy (Port 3000), FastAPI Backend (Port 8000), PostgreSQL (`avanflix`)  
**External Dependency:** `https://handorg.duckdns.org/notifications` (Host: `201.18.216.70`)  

---

## 1. Executive Summary

This diagnostic investigation addresses the sequence of browser console errors observed during Community OTT login attempts:
1. `GET /backend-api/api/v1/auth/me` returning HTTP 401 during initial page load.
2. `POST /backend-api/api/v1/auth/login` returning HTTP 401 on attempt 1.
3. `POST /backend-api/api/v1/auth/login` returning HTTP 503 on attempt 2.
4. `POST /backend-api/api/v1/auth/login` returning HTTP 429 on attempt 3.

Through live request probing, direct PostgreSQL state inspection, code analysis across FastAPI and Next.js, and automated end-to-end testing, the root causes for each status were proven and resolved.

---

## 2. Exact HTTP Response Bodies & Payloads

The investigation inspected the exact response bodies returned by FastAPI, the Next.js proxy, and the frontend handler:

### 2.1 Initial `GET /backend-api/api/v1/auth/me` (HTTP 401)
- **Status:** `HTTP/1.1 401 Unauthorized`
- **FastAPI Body:**
  ```json
  {
    "error": {
      "code": "HTTP_ERROR",
      "message": "Authentication is required.",
      "requestId": "2fdadd80-8e4e-4e85-a818-906424ba1eb8"
    }
  }
  ```
- **Next.js Proxy Headers:** Sets `Set-Cookie: community_session=; Path=/backend-api/api/v1/auth; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT` to flush any invalid or expired client cookie.
- **Frontend Behavior:** Handled in [`frontend/src/app/login/page.tsx`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/login/page.tsx) inside `checkExistingSession()`. `getAuthenticatedIdentity()` catches `!response.ok`, returns `null`, and cleanly ends auth checking (`setCheckingAuth(false)`). It does **not** throw an error or disrupt the login page.

### 2.2 Attempt 1: `POST /backend-api/api/v1/auth/login` (HTTP 401)
- **Status:** `HTTP/1.1 401 Unauthorized`
- **FastAPI Body:**
  ```json
  {
    "error": {
      "code": "AUTHENTICATION_FAILED",
      "message": "Login could not be completed.",
      "requestId": "85aaed6b-4232-47a5-a0ba-6287abd8a25d"
    }
  }
  ```
- **Cause:** Incorrect credentials. The password submitted failed constant-time scrypt evaluation against `AccountCredential.password_hash` in PostgreSQL, triggering `LoginRejected` (`app/core/exceptions.py:84`).

### 2.3 Attempt 2: `POST /backend-api/api/v1/auth/login` (HTTP 503)
- **Status:** `HTTP/1.1 503 Service Unavailable`
- **FastAPI Direct Body:**
  ```json
  {
    "error": {
      "code": "MESSAGING_UNAVAILABLE",
      "message": "The messaging service is temporarily unavailable.",
      "requestId": "90dff2e8-88d4-48b1-adb2-8aac7772b073"
    }
  }
  ```
- **Next.js Proxy Timeout Defect & Fix:**
  - *Previous Behavior:* `proxyCommunityAuth` in [`frontend/src/app/backend-api/[...path]/route.ts`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/backend-api/%5B...path%5D/route.ts) had a hardcoded 2,000ms timeout (`upstreamTimeoutMilliseconds`). Because FastAPI performs an outbound HTTPS connection to `https://handorg.duckdns.org` with a 5.0s `httpx` timeout (total elapsed ~6.2s to 8.2s with scrypt hashing), the proxy aborted at 2.0s and returned `{"error": "BACKEND_PROXY_UNAVAILABLE"}`.
  - *Remediation Applied:* `proxyCommunityAuth` timeout was increased to 12,000ms (`upstreamTimeoutMilliseconds * 6`), allowing FastAPI's upstream 5s timeout to resolve cleanly. The proxy now faithfully passes through `HTTP 503` with `MESSAGING_UNAVAILABLE`.

### 2.4 Attempt 3: `POST /backend-api/api/v1/auth/login` (HTTP 429)
- **Status:** `HTTP/1.1 429 Too Many Requests`
- **FastAPI Body:**
  ```json
  {
    "error": {
      "code": "HTTP_ERROR",
      "message": "Too many requests. Try again later.",
      "requestId": "fa974d76-c63d-442c-a9ec-179d444c6306"
    }
  }
  ```
- **Cause:** Cooldown enforcement. Attempt 2 successfully created an OTP challenge record and an `OtpRateBucket` entry with `action='send'` and `cooldown_until = now + 60s`. Because Attempt 3 was submitted before 60 seconds elapsed, `otp_service.request_otp()` raised `OtpRateLimitExceeded`, returning HTTP 429.

---

## 3. PostgreSQL Identity & Super Admin State Verification

Direct inspection of the PostgreSQL database confirmed:
1. **Account Record:**
   - Table: `account_credentials`
   - ID: `9f6b74ec-0ecb-4f2f-8290-8ff30a50f18b`
   - Username: `super_admin` (normalized lowercase)
   - Account Type: `platform_operator`
   - Status: `active`
2. **Credential Hash Specification:**
   - Format: `scrypt$16384$8$1$<salt>$<hash>`
   - Parameters: $N=16384, r=8, p=1$ (OWASP-recommended memory-hard parameters)
   - Salt: 16 cryptographically random bytes (base64 encoded)
   - Plaintext password is never stored or logged.
3. **Linked Platform Operator:**
   - Table: `platform_operators`
   - ID: `e2742846-b78e-4995-a077-bb06ffe90ad9`
   - Status: `active`
   - Mobile: Stored in valid E.164 format, masked in logs/UI as `+91****05`

---

## 4. Rate Limiting State & Administrative Reset

### Configuration
- **Send Cooldown:** 60 seconds (`otp_send_cooldown_seconds = 60`)
- **Send Window:** 600 seconds (10 minutes) (`otp_send_window_seconds = 600`)
- **Max Sends per Window:** 3 attempts (`otp_send_limit = 3`)
- **Storage Table:** `otp_rate_buckets` (`mobile_e164`, `action`, `window_started_at`, `attempt_count`, `cooldown_until`)

### Administrative Reset CLI
To avoid disabling rate limits in production code while permitting authorized operators to clear rate limits during tests or operator intervention, a dedicated CLI flag was added to [`backend/app/cli/bootstrap_accounts.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/cli/bootstrap_accounts.py):

```powershell
# Reset rate limit for a username:
$env:PYTHONPATH='backend'; backend\.venv\Scripts\python -m app.cli.bootstrap_accounts --reset-rate-limit --username super_admin

# Reset rate limit by mobile number:
$env:PYTHONPATH='backend'; backend\.venv\Scripts\python -m app.cli.bootstrap_accounts --reset-rate-limit --mobile +919100209205
```
**Verification:** Command executed on live database, returned: `Successfully cleared 1 rate limit record(s).`

---

## 5. UI Error Handling Remediations

In [`frontend/src/app/login/page.tsx`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/login/page.tsx), error handling was updated across Step 1 (`handleCredentialsSubmit`), Step 2 (`handleOtpSubmit`), and Resend (`handleResendOtp`):

1. **HTTP 401:** Displays `"Invalid username or password. Please verify your credentials."` (Step 1) or `"Incorrect verification code. Please try again."` (Step 2).
2. **HTTP 503 with `MESSAGING_UNAVAILABLE`:** Displays `"Notification service is temporarily unavailable. Please try again later."`
3. **HTTP 503 generic / proxy (`BACKEND_PROXY_UNAVAILABLE`):** Displays `"Authentication service is temporarily unavailable. Please try again later."`
4. **HTTP 429:** Displays `"Too many authentication requests. Please wait before trying again."` (or includes dynamic wait seconds if provided).
5. **No False Credential Blaming:** HTTP 503, 502, 500, and 429 are never mislabeled as `"Authentication failed. Please verify your username and password."`.

---

## 6. Automated Regression & Verification Tests

### Automated Suites Executed

| Suite | Scope | Result | Execution Time |
| :--- | :--- | :--- | :--- |
| **Super Admin Bootstrap & Rate Limit Reset** | `backend/tests/test_super_admin_login_and_bootstrap.py` | **9 / 9 PASSED** | 16.54s |
| **Real OTP Delivery & Mobile Persistence** | `backend/tests/test_real_otp_delivery_and_mobile_persistence.py` | **7 / 7 PASSED** | 1.83s |
| **Notification Service Client** | `backend/tests/test_notification_service.py` | **21 / 21 PASSED** | 0.82s |
| **OTP State Machine** | `backend/tests/test_otp_login_flow.py` | **6 / 6 PASSED** | 0.44s |
| **Password Hashing & Constant-Time Scrypt** | `backend/tests/test_password_authentication.py` | **18 / 18 PASSED** | 8.89s |
| **Authentication API End-to-End** | `backend/tests/test_authentication_api.py` | **33 / 33 PASSED** | 47.78s |
| **Frontend Proxy Integration Suite** | `frontend/test/proxy-integration.test.mjs` | **21 / 21 PASSED** | 42.61s |
| **Frontend TypeScript Check** | `npm --prefix frontend run typecheck` | **PASSED (0 errors)** | 6.50s |

**Total Regression Pass Rate:** **115 / 115 passing**.

---

## 7. Remaining External Infrastructure Blockers

Because the external Notification Microservice host server (`201.18.216.70`) is external to this repository:
1. **TCP Port 443 Timeout (`SEC-TLS`):** Packets sent to `https://handorg.duckdns.org:443` continue to time out. The host firewall or Nginx configuration on `201.18.216.70` is not accepting inbound HTTPS connections.
2. **Registry Mapping (`BLK-01`):** The Notification Microservice registry on port 80 returns `HTTP 400 TENANT_NOT_FOUND` because the Community OTT service has not been mapped to an authorized tenant.
3. **Fail-Closed Boundary:** The Community OTT platform strictly fails closed with `HTTP 503 MESSAGING_UNAVAILABLE`. In accordance with security requirements, the platform will **not** fall back to static OTPs, plaintext HTTP, or disabled TLS verification.

Real OTP delivery cannot be certified in production until the external Notification Microservice operator remedies the Port 443 firewall block and provisions the tenant registry.

