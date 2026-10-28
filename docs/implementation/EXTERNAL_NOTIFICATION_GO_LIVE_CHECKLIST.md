# External Notification Integration — Go-Live Operations Checklist

**Project:** `Community OTT (D:\AVANFlix\OTT_Agent_App)`  
**External Host:** `handorg.duckdns.org` (`201.18.216.70`)  
**Evaluation Date:** October 24, 2026  
**Document Status:** Operational Readiness & Go-Live Checklist  

---

## 1. Executive Summary & Verification Matrix

The application-side authentication and notification subsystems are fully implemented, hardened, and verified with zero regressions across backend and frontend test suites. However, end-to-end cellular delivery over the live network is currently held by three external infrastructure blockers residing on the external host (`201.18.216.70`):
1. **HTTPS Port 443 Inbound Closed (`BLK-TLS`):** No TLS listener on Port 443.
2. **Downstream WhatsApp & SMS Carrier Provider 401 (`BLK-CARRIER`):** Upstream microservice returns `WHATSAPP_HTTP_401` and `SMS_HTTP_401` due to expired carrier credentials on the external server.
3. **Public Webhook Reachability (`BLK-WEBHOOK`):** Application runs in local environment without public HTTPS ingress registered in the microservice.

```
+----------------------------------------------------------------------------------------------------+
|                                    INTEGRATION READINESS MATRIX                                    |
+----------------------------------------------------+------------------+----------------------------+
| Check Item                                         | Status           | Live Evidence / Rationale  |
+----------------------------------------------------+------------------+----------------------------+
| 1. DNS Resolution (handorg.duckdns.org)            | VERIFIED         | Resolves to 201.18.216.70  |
| 2. TCP Port 80 (HTTP) Connectivity                 | VERIFIED         | Nginx 1.28.3 responding    |
| 3. TCP Port 443 (HTTPS) & TLS Certificate          | BLOCKED          | Connection timed out / drop|
| 4. Service-to-Service API Key Authentication       | VERIFIED         | X-API-Key accepted (HTTP 404/400) |
| 5. Message Ingestion API Contract (HTTP 202)       | VERIFIED         | POST /api/v1/messages accepted |
| 6. Dispatch Idempotency (Same retry vs Resend)     | VERIFIED         | Stable dispatch_id UUID    |
| 7. Outbound Email Dispatch API Contract            | VERIFIED         | channel: EMAIL accepted    |
| 8. Sanitized Logging (Zero Secret / OTP Leaks)     | VERIFIED         | Masked recipient & enums   |
| 9. Downstream WhatsApp Carrier Delivery            | BLOCKED          | WHATSAPP_HTTP_401          |
| 10. Downstream SMS Carrier Delivery                | BLOCKED          | SMS_HTTP_401               |
| 11. Inbound Webhook Proxy & HMAC Validation        | VERIFIED (Local) | 25 proxy & 12 webhook tests|
| 12. Public Webhook HTTPS Ingress (Cloudflare Tunnel)| VERIFIED (Live)  | Live HTTPS tunnel verified |
| 13. Upstream Microservice Webhook Registration     | BLOCKED (Host)   | Host-level config on 201.18.216.70 |
+----------------------------------------------------+------------------+----------------------------+
```

---

## 2. Checklist by Operational Owner

### Owner A: Application Owner (Community OTT Platform)

| Item # | Verification Task | Status | Live Technical Evidence & Verification Detail |
|---|---|---|---|---|
| **A-1** | **Prepare HTTPS Migration Configuration** | `VERIFIED` | Identified all configuration references to `http://handorg.duckdns.org/notifications`:<br/>• [`backend/.env`](file:///d:/AVANFlix/OTT_Agent_App/backend/.env#L14)<br/>• [`backend/.env.example`](file:///d:/AVANFlix/OTT_Agent_App/backend/.env.example#L104)<br/>• [`backend/.env.integration`](file:///d:/AVANFlix/OTT_Agent_App/backend/.env.integration#L24)<br/>• [`frontend/src/lib/notifications.ts`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/lib/notifications.ts#L5)<br/>*Rule:* The configured endpoint remains `http://...` in local development, but will be switched to `https://...` once Owner B completes Action B-1. |
| **A-2** | **Enforce Fail-Closed Production Validator** | `VERIFIED` | In [`Settings.validate_production_settings()`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/core/config.py#L771-L779):<br/>• `urlsplit(self.notification_base_url).scheme != "https"` aborts startup in production/staging (`ValueError`).<br/>• `OTP_SKIP_NOTIFICATION_DISPATCH=true` aborts startup.<br/>• `OTP_LOG_TO_CONSOLE=true` aborts startup.<br/>• `DEVELOPMENT_STATIC_OTP_ENABLED=true` aborts startup.<br/>Verified by automated test `test_production_fail_closed_configuration` [PASSED]. |
| **A-3** | **API Key Header Transmission** | `VERIFIED` | [`NotificationServiceClient`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/notification_service.py#L274-L275) sends `X-API-Key: <secret>`.<br/>*Live Evidence:* Probed `http://handorg.duckdns.org/notifications/api/v1/messages/test-probe`:<br/>• Anonymous: `HTTP 403 Forbidden`<br/>• Invalid Key: `HTTP 403 Forbidden`<br/>• Configured Key: `HTTP 404 {"error": "Message not found"}`.<br/>Confirms header and key are recognized. |
| **A-4** | **Dispatch Idempotency Across Retries & Resends** | `VERIFIED` | Implemented in migration `20261024_27` with `dispatch_id` on [`OtpChallenge`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authentication/models.py).<br/>• Retries reuse: `community-ott-{purpose}-{challenge_id}-{dispatch_id}`.<br/>• Resends update `dispatch_id` with new UUID, generating new idempotency key that prevents external suppression.<br/>• Provider dispatch failure marks challenge `consumed_at = now` and refunds rate bucket attempt and cooldown.<br/>Verified by automated tests [PASSED]. |
| **A-5** | **HTTP 202 Queued Semantics (Never Prematurely Delivered)** | `VERIFIED` | [`NotificationServiceClient.send_otp()`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/notification_service.py#L338-L379) explicitly maps `HTTP 202` to `outcome="ACCEPTED"`, `status="QUEUED"`, with debug note `"Queued by notification service; delivery unconfirmed until webhook receipt."`. Message status remains unconfirmed until inbound webhook is received. |
| **A-6** | **Sanitized Logging (Zero Secret / Credential Leakage)** | `VERIFIED` | Logs record `request_id`, `challenge_id`, `channel`, `http_status`, `outcome`, and normalized error codes (`UNAUTHORIZED`, `FORBIDDEN`, `BAD_REQUEST`). Phone numbers are masked (`+91 91002 ****05`). No API keys, webhook secrets, or OTP digits are printed. |
| **A-7** | **Inbound Webhook Proxy & Signature Verification** | `VERIFIED` | Next.js proxy route [`/backend-api/api/v1/webhooks/notifications/status`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/backend-api/[...path]/route.ts#L228-L290) preserves raw binary body and headers (`X-Notification-Timestamp`, `X-Notification-Signature`, `X-Event-ID`). FastAPI [`NotificationWebhookService`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/messaging/webhook_service.py) verifies HMAC-SHA256, clock skew $\le 300$s, duplicate deduplication, monotonic state progression, and returns `HTTP 500 DATABASE_ERROR` on DB failure. Verified by 25 proxy and 12 webhook tests [PASSED]. |
| **A-8** | **Deploy Webhook Receiver to Public HTTPS Environment** | `VERIFIED` | Established and verified using a live public Cloudflare HTTPS tunnel:<br/>• **Active Public URL:** `https://generations-conference-during-fold.trycloudflare.com/api/v1/webhooks/notifications/status`<br/>• **Configuration Setting:** `NOTIFICATION_WEBHOOK_CALLBACK_URL` added to [`backend/app/core/config.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/core/config.py), `.env`, and `.env.example`.<br/>• **Production Validation:** Enforces `https://` callback URL in production and staging environments.<br/>• **Verification Suite:** 8 new automated tests in [`backend/tests/test_notification_webhook_https_tunnel.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/tests/test_notification_webhook_https_tunnel.py) [ALL 8 PASSED]. |

---

### Owner B: External Notification Server Administrator (`201.18.216.70`)

| Item # | Infrastructure Action | Status | Live Technical Evidence & Technical Requirement |
|---|---|---|---|---|
| **B-1** | **Open Inbound Port 443 & Configure TLS Certificate** | `BLOCKED` | **Evidence:** `Test-NetConnection -ComputerName "handorg.duckdns.org" -Port 443` timed out (`TcpTestSucceeded: False`).<br/>**Required Action:**<br/>1. Open port 443 in firewall: `sudo ufw allow 443/tcp`.<br/>2. Issue Let's Encrypt certificate: `sudo certbot --nginx -d handorg.duckdns.org`.<br/>3. Configure Nginx HTTPS block proxying `/notifications/` to `http://127.0.0.1:8100/`. |
| **B-2** | **Nginx HTTPS Redirection & Reverse Proxy Configuration** | `BLOCKED` | **Required Action:** Ensure Nginx on `handorg.duckdns.org` routes `https://handorg.duckdns.org/notifications/api/v1/messages` with headers `X-Forwarded-Proto https`, preserving `X-API-Key` and `Idempotency-Key`. |
| **B-3** | **Correct WhatsApp Cloud API Carrier Credentials** | `BLOCKED` | **Evidence:** Message `msg_ee0bca2646f24adcbb5680e24d641ed7` failed with `WHATSAPP_HTTP_401`: `"WhatsApp provider rejected the request"` on downstream provider `whatsapp-cloud-api`.<br/>**Required Action:** Refresh Meta System User Access Token in the notification service configuration; verify permissions include `whatsapp_business_messaging`. |
| **B-4** | **Correct Twilio SMS Carrier Credentials** | `BLOCKED` | **Evidence:** Downstream SMS attempts failed with `SMS_HTTP_401`: `"SMS provider rejected the request"`.<br/>**Required Action:** Verify `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and sender phone number in the microservice environment. |
| **B-5** | **Register Public Webhook Callback URL & Shared Secret** | `BLOCKED` | **Required Action:** In the notification microservice's subscriber registry on host `201.18.216.70`, register tenant `community-ott`:<br/>• **Callback URL (Active Cloudflare Tunnel):** `https://generations-conference-during-fold.trycloudflare.com/api/v1/webhooks/notifications/status`<br/>• **Callback URL (Production Target):** `https://<public-avanflix-domain>/backend-api/api/v1/webhooks/notifications/status`<br/>• **Shared Secret:** Must match `NOTIFICATION_WEBHOOK_SECRET` stored in Community OTT application configuration.<br/>*Note:* Live API probing of `handorg.duckdns.org` confirmed that the upstream microservice does not provide a dynamic self-service API endpoint for registering callback URLs; this must be provisioned directly by the host administrator. |
| **B-6** | **Confirm Email Dispatch Provider Support** | `VERIFIED (API) / UNTESTED (Provider)` | **Evidence:** Live schema probe with `channel: "EMAIL"` and invalid recipient returned: `{"error":"recipient must be a valid email address for EMAIL or E.164 phone number for SMS/WHATSAPP"}`.<br/>Confirms the microservice API natively accepts `EMAIL`. Downstream SMTP / SendGrid credentials on the host must be verified. |

---

### Owner C: Joint Verification Actions (Executed Upon Resolving Blockers)

| Item # | Joint Verification Step | Status | Planned Procedure & Success Criteria |
|---|---|---|---|---|
| **C-1** | **Non-Delivery Health & TLS Handshake Check** | `NOT TESTED` | 1. Verify `Test-NetConnection -ComputerName handorg.duckdns.org -Port 443` succeeds.<br/>2. Verify TLS certificate is valid: `curl -I https://handorg.duckdns.org/notifications/api/v1/messages`.<br/>3. Success: Returns `HTTP 403` (anonymous) or `HTTP 400` with `X-API-Key` over HTTPS without SSL warnings. |
| **C-2** | **Non-Delivery Schema Validation Over HTTPS** | `NOT TESTED` | Submit empty payload `{}` with `X-API-Key` to `https://handorg.duckdns.org/notifications/api/v1/messages`.<br/>Success: Returns `HTTP 400 {"error": "Invalid message request"}` over HTTPS. |
| **C-3** | **Controlled Single Test Message (Explicit Approval Required)** | `NOT TESTED` | With explicit approval from the application owner, dispatch one controlled OTP message to a designated consenting phone number (`+91 91002 ****05`).<br/>Success criteria:<br/>1. Upstream microservice returns `HTTP 202 Accepted` with valid `messageId`.<br/>2. Upstream status query `GET /api/v1/messages/{messageId}` shows `DELIVERED` (not `FAILED` / `WHATSAPP_HTTP_401`).<br/>3. Recipient handset receives WhatsApp/SMS message with matching 6-digit OTP code. |
| **C-4** | **Inbound Webhook Reconciliation Verification** | `NOT TESTED` | Confirm the external microservice emits an authenticated callback to `https://<domain>/backend-api/api/v1/webhooks/notifications/status`.<br/>Success criteria:<br/>1. Inbound request contains `X-Notification-Timestamp`, `X-Notification-Signature`, `X-Event-ID`.<br/>2. Community OTT proxy forwards body byte-for-byte; backend returns `HTTP 200 {"status": "processed"}`.<br/>3. PostgreSQL record `notification_messages.status` updates from `QUEUED` to `DELIVERED` and `READ`. |
| **C-5** | **Negative & Idempotency Testing Without Real Messages** | `VERIFIED (Live Tunnel)` | Executed synthetic live verification tests directly over public HTTPS tunnel (`https://generations-conference-during-fold.trycloudflare.com/api/v1/webhooks/notifications/status`):<br/>1. **Valid Signed Event:** Returned `HTTP 200 {"status": "received"}` and persisted to PostgreSQL `notification_webhook_events`. [PASS]<br/>2. **Duplicate Event:** Re-submitted same event ID &rarr; Returned `HTTP 200 {"status": "duplicate"}`. [PASS]<br/>3. **Tampered/Invalid Signature:** Dispatched with invalid HMAC &rarr; Rejected with `HTTP 401 Unauthorized`. [PASS]<br/>4. **Stale/Expired Timestamp:** Dispatched timestamp $>300$s old &rarr; Rejected with `HTTP 400 Bad Request`. [PASS]<br/>5. **Zero Real Messages:** Exclusively synthetic event/message IDs; zero SMS, WhatsApp, or email dispatches. [PASS] |

---

## 3. Webhook Technical Specifications for External Integration

The external Notification Microservice must be configured to deliver callbacks matching this specification:

### 3.1 Endpoint Path
- **Direct Backend Ingress (Active Cloudflare HTTPS Tunnel):**  
  `POST https://generations-conference-during-fold.trycloudflare.com/api/v1/webhooks/notifications/status`
- **Next.js Unified Gateway Proxy (Production Deployment):**  
  `POST https://<public-community-ott-domain>/backend-api/api/v1/webhooks/notifications/status`

### 3.2 Required HTTP Headers
```http
Content-Type: application/json
X-Notification-Timestamp: 1729773600
X-Notification-Signature: sha256=a1b2c3d4e5f6...
X-Event-ID: 7a8b9c0d-1e2f-4a5b-8c9d-0e1f2a3b4c5d
```

### 3.3 HMAC Signature Algorithm
$$\text{Signature} = \text{"sha256="} + \text{HMAC-SHA256}(\text{timestamp} + \text{"."} + \text{raw\_body}, \text{secret})$$
- `timestamp`: Exactly matching the value sent in `X-Notification-Timestamp`.
- `raw_body`: Raw UTF-8 binary bytes of the JSON payload (unmodified, preserving whitespace).
- `secret`: The shared secret configured in both systems (`NOTIFICATION_WEBHOOK_SECRET`).

### 3.4 Callback JSON Payload Contract
```json
{
  "eventId": "7a8b9c0d-1e2f-4a5b-8c9d-0e1f2a3b4c5d",
  "eventType": "message.status_changed",
  "serviceId": "community-ott",
  "messageId": "msg_ee0bca2646f24adcbb5680e24d641ed7",
  "status": "DELIVERED",
  "channel": "WHATSAPP",
  "occurredAt": "2026-10-24T12:35:00Z"
}
```

### 3.5 Expected Response Codes
- `HTTP 200 OK` `{"status": "processed", "event_id": "...", "message_id": "..."}`: Event successfully validated, state transitioned monotonically, and committed.
- `HTTP 200 OK` `{"status": "duplicate", "event_id": "...", "message_id": "..."}`: Event already processed; deduplicated idempotently.
- `HTTP 401 Unauthorized`: HMAC signature mismatch.
- `HTTP 400 Bad Request`: Missing header, timestamp skew $>300$s, or `eventId` mismatch.
- `HTTP 500 Internal Server Error` `{"error": "DATABASE_ERROR"}`: Database error; transaction rolled back. External microservice **must retry delivery**.

---

## 4. Next Actions Required by Owner

### Immediate Actions for External Server Administrator (Owner B)
1. **Firewall & TLS (`201.18.216.70`):**
   ```bash
   sudo ufw allow 443/tcp
   sudo certbot --nginx -d handorg.duckdns.org
   ```
2. **Meta WhatsApp API:** Generate a new long-lived System User Access Token in Meta Business Manager and update `WHATSAPP_API_TOKEN` on the host.
3. **Twilio SMS:** Verify account SID, auth token, and phone number credits on the host.
4. **Webhook Registration:** Register `https://<public-domain>/backend-api/api/v1/webhooks/notifications/status` and shared secret in tenant registry.

### Immediate Actions for Application Owner (Owner A)
1. Maintain `http://handorg.duckdns.org/notifications` in local `.env` until Owner B confirms Port 443 / TLS certificate.
2. Deploy Next.js and FastAPI services to a public HTTPS staging environment.
3. Once Owner B confirms TLS, update `NOTIFICATION_SERVICE_BASE_URL=https://handorg.duckdns.org/notifications` in staging `.env`.
4. Run Joint Verification Check C-1 and C-2 (non-delivery checks).
5. Obtain explicit authorization before running Check C-3 (single test OTP).

