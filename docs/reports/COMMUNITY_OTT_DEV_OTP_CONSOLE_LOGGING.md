# Community OTT — Development OTP Console Logging Implementation

**Platform Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Generated Date:** 2026-10-23  
**Audience:** Platform Engineering, Security Architecture, Local Developers  

---

## 1. Overview & Objective

To facilitate frictionless local end-to-end testing and authentication verification when external messaging providers or notification microservices are offline or unreachable, a secure, development-only mechanism has been implemented to print the generated Community OTT login OTP directly to the FastAPI backend terminal.

---

## 2. Security Boundaries & Invariants

| Security Boundary | Policy & Implementation |
|---|---|
| **Default Disabled** | `OTP_LOG_TO_CONSOLE` is `false` by default in `Settings`. |
| **Strict Environment Gate** | Permitted **only** when `ENVIRONMENT=development`. Pydantic model validation (`validate_production_settings`) raises a hard `ValidationError` if `OTP_LOG_TO_CONSOLE=true` in `production`, `staging`, or `test`. |
| **Defense in Depth** | `Settings.can_log_otp_to_console` property enforces `self.environment == "development" and self.otp_log_to_console`. |
| **No Static OTPs** | The logged OTP is the actual, dynamically generated 6-digit cryptographic random code (`secrets.randbelow(900000) + 100000`). |
| **No Credential / OTP Leakage** | The plain OTP is **never** included in HTTP responses, browser bundles, general structured request logs, or application audit sinks. |
| **Non-Disruptive** | Durably persisted in PostgreSQL before printing. Console output is wrapped in a fail-safe try/except block; standard output errors can never abort OTP persistence or notification dispatch. |
| **Independent Dispatch** | Normal notification dispatch (`NotificationServiceClient.send_otp`) continues to execute unchanged unless explicitly bypassed with `OTP_SKIP_NOTIFICATION_DISPATCH=true`. |
| **Notification Dispatch Bypass** | `OTP_SKIP_NOTIFICATION_DISPATCH` allows developers to bypass outbound HTTP calls during local testing. Disabled by default, rejected in production/staging/test, logs `DEVELOPMENT ONLY: notification dispatch skipped`, and records `SKIPPED_LOCAL` without claiming delivery. |

---

## 3. Configuration

### Backend `.env` (Development Setup)

```dotenv
# Environment must be development
ENVIRONMENT=development

# Dedicated development-only console output (disabled by default)
OTP_LOG_TO_CONSOLE=true

# Development-only bypass of outbound notification microservice requests (disabled by default)
OTP_SKIP_NOTIFICATION_DISPATCH=true

# Standard OTP policy
OTP_CHALLENGE_TTL_SECONDS=300
OTP_SEND_COOLDOWN_SECONDS=60
```

### Staging and Production Rejection

Any attempt to run with `OTP_LOG_TO_CONSOLE=true` or `OTP_SKIP_NOTIFICATION_DISPATCH=true` when `ENVIRONMENT` is `production` or `staging` halts application startup immediately:
```text
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, OTP_LOG_TO_CONSOLE is not permitted in production or staging.
```
or
```text
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, OTP_SKIP_NOTIFICATION_DISPATCH is not permitted in production or staging.
```

---

## 4. Terminal Output Format

When enabled in development, after the random OTP challenge is salted, hashed, and persisted to PostgreSQL, the following notice is written directly to backend `sys.stdout`:

```text
======================================================================
[DEVELOPMENT ONLY] Community OTT Login OTP
----------------------------------------------------------------------
  Challenge ID : 5a415a78-b19b-4654-8e42-ec32e36611ef
  Purpose      : login
  Login OTP    : 481923
  Expires In   : 300s
======================================================================
```

---

## 5. Files Changed

1. **`backend/app/core/config.py`**:
   - Added `otp_log_to_console: bool = False` field.
   - Added `otp_skip_notification_dispatch: bool = False` field.
   - Enforced strict rejection in `production`, `staging`, and non-`development` environments inside `validate_production_settings`.
   - Added `can_log_otp_to_console` and `can_skip_notification_dispatch` properties.
2. **`backend/app/modules/authentication/otp_service.py`**:
   - Updated `_print_dev_otp_to_terminal(challenge_id, purpose, otp_code, expires_in_seconds, dispatch_skipped)` to accept optional dispatch skip status.
   - Added check for `otp_skip_notification_dispatch` in step `4`: skips outbound HTTP calls entirely, logs `DEVELOPMENT ONLY: notification dispatch skipped`, and records `SKIPPED_LOCAL` status in challenge metadata.
   - Preserves 100% of cryptographic random OTP generation, PostgreSQL persistence, rate limits, and verification flow.
3. **`backend/.env.example`**:
   - Documented `OTP_LOG_TO_CONSOLE=false` and `OTP_SKIP_NOTIFICATION_DISPATCH=false` with security instructions.
4. **`backend/.env`**:
   - Enabled `OTP_LOG_TO_CONSOLE=true` and `OTP_SKIP_NOTIFICATION_DISPATCH=true` for local development testing.
5. **`backend/tests/test_otp_console_logging.py`**:
   - Test suite covering console logging functionality.
6. **`backend/tests/test_otp_skip_notification_dispatch.py`**:
   - Dedicated 10-test suite verifying defaults, dev allowance, production/staging/test rejection, outbound HTTP suppression, terminal output with `Dispatch: SKIPPED (development mode)`, PostgreSQL persistence with `SKIPPED_LOCAL`, rate limiting, replay protection, and end-to-end FastAPI login completion.

---

## 6. Verification & Test Evidence

### Backend Pytest Suites

* **Dedicated Skip Notification Dispatch Suite:**
  ```bash
  pytest backend/tests/test_otp_skip_notification_dispatch.py -v
  ```
  **Result:** **10 passed in 4.37s**
  - `test_otp_skip_notification_dispatch_disabled_by_default` (PASS)
  - `test_otp_skip_notification_dispatch_allowed_in_development_when_explicitly_enabled` (PASS)
  - `test_otp_skip_notification_dispatch_rejected_in_production` (PASS)
  - `test_otp_skip_notification_dispatch_rejected_in_staging` (PASS)
  - `test_otp_skip_notification_dispatch_rejected_in_test_environment` (PASS)
  - `test_can_skip_notification_dispatch_defense_in_depth` (PASS)
  - `test_skip_dispatch_enabled_skips_http_request_and_logs` (PASS)
  - `test_normal_dispatch_when_switch_disabled` (PASS)
  - `test_otp_rate_limiting_and_replay_protection_enforced` (PASS)
  - `test_fastapi_login_endpoint_with_skip_notification_dispatch` (PASS)

* **Dedicated Console Logging Suite:**
  ```bash
  pytest backend/tests/test_otp_console_logging.py -v
  ```
  **Result:** **10 passed in 5.20s**

* **Combined Authentication & Messaging Suite:**
  ```bash
  pytest backend/tests/test_otp_skip_notification_dispatch.py backend/tests/test_otp_console_logging.py backend/tests/test_notification_service.py backend/tests/test_notification_webhook.py backend/tests/test_real_otp_delivery_and_mobile_persistence.py backend/tests/test_otp_login_flow.py backend/tests/test_super_admin_login_and_bootstrap.py
  ```
  **Result:** **89 passed**

* **Authentication API Suite:**
  ```bash
  pytest backend/tests/test_authentication_api.py
  ```
  **Result:** **33 passed**

### Frontend Proxy Suite

* **Next.js Reverse Proxy Integration:**
  ```bash
  npm run test:proxy
  ```
  **Result:** **22 passed**

