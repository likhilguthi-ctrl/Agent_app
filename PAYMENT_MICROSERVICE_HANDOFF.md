# Payment Microservice & Agent-Led Subscriber Registration Developer Handoff

**Project:** Community OTT Agent Platform  
**Directory:** `D:\AVANFlix\OTT_Agent_App`  
**Date:** October 26, 2026  
**Status:** Implemented, Integrated, and Verified  

---

## 1. Architecture Overview & Flow A (Hosted Checkout Redirect)

The platform implements **Flow A: Hosted Checkout Redirect** as specified in `FRONTEND_INTEGRATION_PAYMENT.md`. This pattern keeps sensitive payment processing and third-party gateway SDKs off the agent app client, ensuring zero-trust credential isolation.

```mermaid
sequenceDiagram
    autonumber
    actor Agent as Agent (Browser UI)
    participant NextProxy as Next.js Proxy (/backend-api/)
    participant FastAPI as FastAPI Backend
    participant Postgres as PostgreSQL Database
    participant PaymentMS as Payment Microservice (8102 / DuckDNS)
    participant NotifMS as Notification Microservice
    actor Subscriber as Subscriber (End User)

    Agent->>NextProxy: POST /api/v1/agent/users (Subscriber form + plan or custom amount)
    NextProxy->>FastAPI: Forward with community_session & CSRF token
    FastAPI->>Postgres: Verify Agent, create User & TenantMembership
    FastAPI->>Postgres: Create SubscriberProfile (whatsapp_consent recorded)
    FastAPI->>Postgres: Create MemberSubscription (status: PENDING_PAYMENT)
    FastAPI->>PaymentMS: POST /api/v1/payments/init (Header: X-API-Key, Payload: amount, tenant, callback_url)
    PaymentMS-->>FastAPI: 200 OK (payment_id, order_id, payment_page_url)
    FastAPI->>Postgres: Create PaymentOrder (external_payment_id, payment_page_url, status: created)
    FastAPI-->>Agent: 200 OK (user_id, payment_page_url, payment_id)
    
    Agent->>PaymentMS: Redirect to payment_page_url (/checkout/{paymentId})
    Subscriber->>PaymentMS: Completes payment (UPI / Card / NetBanking)
    PaymentMS-->>Agent: Browser Redirect to /agent/payment/callback?payment_id=...&order_id=...&status=COMPLETED

    Agent->>NextProxy: POST /api/v1/agent/payments/confirm (payment_id, order_id)
    NextProxy->>FastAPI: Forward with session
    FastAPI->>PaymentMS: GET /api/v1/payments/{payment_id} (Server-side status check)
    PaymentMS-->>FastAPI: Status: COMPLETED
    FastAPI->>Postgres: Update PaymentOrder -> paid, MemberSubscription -> ACTIVE
    FastAPI->>NotifMS: Dispatch Welcome Message (SMS mandatory; WhatsApp ONLY if whatsapp_consent == true)
    FastAPI-->>Agent: Payment Confirmed & Subscription Activated
```

---

## 2. Security Boundaries & Zero-Trust Invariants

1. **Private Credential Isolation**:
   - `PAYMENT_SERVICE_API_KEY` (`X-API-Key`) and `PAYMENT_WEBHOOK_SECRET` reside solely in backend server environments (`backend/.env` / production vault).
   - Neither key is EVER exposed to browser clients, embedded in client JavaScript, or returned in public API payloads.
2. **Server-Side Settlement Verification**:
   - The frontend redirect handler (`/agent/payment/callback`) NEVER marks a subscription `ACTIVE` directly from query parameters (e.g. `?status=COMPLETED`).
   - Every activation requires a server-to-server confirmation call (`POST /api/v1/agent/payments/confirm`) that interrogates the Payment Microservice authority (`GET /api/v1/payments/{payment_id}`).
3. **Strict WhatsApp Consent Compliance**:
   - Welcome WhatsApp notifications are strictly conditional on `whatsapp_consent == True`. If the user does not provide explicit consent or unchecks the box, the WhatsApp message is skipped while SMS delivery proceeds.

---

## 3. Environment Configuration

### Backend (`backend/.env`)

```ini
# --- Payment Microservice ---
# Remote live endpoint: http://handorg.duckdns.org
# Local development endpoint: http://localhost:8102
# Testing dashboard: http://localhost:5055
PAYMENT_SERVICE_URL=http://localhost:8102
PAYMENT_SERVICE_API_KEY=your_payment_microservice_api_key_here
PAYMENT_SERVICE_TIMEOUT_SECONDS=10.0
PAYMENT_WEBHOOK_SECRET=your_hmac_webhook_secret_here

# --- Notification Microservice ---
NOTIFICATION_SERVICE_BASE_URL=http://handorg.duckdns.org/notifications
NOTIFICATION_SERVICE_API_KEY=your_notification_api_key_here
NOTIFICATION_SERVICE_DELIVERY_MODE=direct
```

### Frontend (`frontend/.env.local`)

```ini
# Next.js Server Proxy Target (FastAPI)
BACKEND_INTERNAL_URL=http://127.0.0.1:8000
```

> **Note**: Frontend does not require any `NEXT_PUBLIC_` payment service keys. All payment routing flows through `/backend-api/api/v1/agent/users` and `/backend-api/api/v1/agent/payments/confirm`.

---

## 4. PostgreSQL Database Schema Additions

Alembic Migration: `backend/migrations/versions/20261026_29_subscriber_whatsapp_consent_and_payment.py`  
Current Head: `20261026_29`

### Additions to `subscriber_profiles`
- `whatsapp_consent` (Boolean, default `False`, nullable=False): Records explicit permission for WhatsApp messaging.
- `whatsapp_consent_at` (DateTime with timezone, nullable=True): Audit timestamp when consent was granted.

### Additions to `payment_orders`
- `external_payment_id` (String(128), nullable=True, indexed): Stores `pay_...` ID from Payment Microservice.
- `payment_page_url` (String(1024), nullable=True): Stores hosted checkout page URL.
- `custom_amount` (Boolean, default `False`, nullable=False): Tracks whether the payment was for a standard plan or customized pricing.

---

## 5. API Contracts

### 5.1 Register Subscriber & Initiate Payment
- **Endpoint:** `POST /api/v1/agent/users` (proxied via `/backend-api/api/v1/agent/users`)
- **Headers:** `Cookie: community_session=...`, `X-CSRF-Token: ...`
- **Request Body:**
```json
{
  "full_name": "Ramesh Kumar",
  "mobile_number": "+919876543210",
  "whatsapp_number": "+919876543210",
  "whatsapp_consent": true,
  "email": "ramesh@example.com",
  "photo_url": "https://pub-...r2.dev/subscribers/photo.jpg",
  "plan_id": "00000000-0000-0000-0000-000000000001",
  "custom_amount": 350.00,
  "verification_token": "ver_..."
}
```
- **Response:**
```json
{
  "user_id": "c1f7a0b3-...",
  "full_name": "Ramesh Kumar",
  "mobile_number": "+91 98765 •••••",
  "whatsapp_number": "+91 98765 •••••",
  "whatsapp_consent": true,
  "payment_page_url": "http://localhost:8102/checkout/pay_123456",
  "payment_id": "pay_123456",
  "order_id": "order_789012",
  "amount": 350.0,
  "currency": "INR",
  "status": "pending_payment"
}
```

### 5.2 Confirm Payment & Activate Subscription
- **Endpoint:** `POST /api/v1/agent/payments/confirm` (proxied via `/backend-api/api/v1/agent/payments/confirm`)
- **Headers:** `Cookie: community_session=...`, `X-CSRF-Token: ...`
- **Request Body:**
```json
{
  "payment_id": "pay_123456",
  "order_id": "order_789012"
}
```
- **Response:**
```json
{
  "success": true,
  "status": "COMPLETED",
  "subscription_status": "ACTIVE",
  "order_id": "order_789012",
  "message": "Payment verified and subscription activated successfully."
}
```

### 5.3 Agent Collections Summary
- **Endpoint:** `GET /api/v1/agent/collections/summary`
- **Headers:** `Cookie: community_session=...`
- **Response:**
```json
{
  "total_subscribers": 14,
  "active_subscriptions": 12,
  "total_collected_amount": 4200.00,
  "currency": "INR"
}
```

---

## 6. Verification & Test Commands

To verify the complete integration across backend and frontend:

```bash
# 1. Backend Payment & Subscriber Workflow Tests (6 tests)
powershell -Command "Set-Location backend; & '.\.venv\Scripts\python.exe' -m pytest tests/test_agent_subscriber_payment_workflow.py -v"

# 2. Complete Backend Regression Suite (48 tests)
powershell -Command "Set-Location backend; & '.\.venv\Scripts\python.exe' -m pytest tests/test_agent_appointments_and_rbac.py tests/test_registration_workflow_upgrade.py tests/test_platform_communities.py tests/test_location_registration_phase1.py tests/test_auth_and_notification_workflow_fixes.py tests/test_agent_subscriber_payment_workflow.py -v"

# 3. Frontend Proxy Security & Route Tests (25 tests)
powershell -Command "node frontend/test/proxy-integration.test.mjs"

# 4. Next.js Production Build & TypeScript Verification (30 pages)
powershell -Command "Set-Location frontend; npm run build"
```

