# Community OTT Agent Platform — Phase 4 Stage 4.3 Implementation Report
## Razorpay Integration, Verified Subscription Payments & Multi-Tier Referral Commissions Baseline

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Date:** October 21, 2026  
**Document Path:** `docs/reports/PHASE_4_STAGE_4_3_IMPLEMENTATION_REPORT.md`  
**Phase:** Phase 4 — Agent Referral Commissions, Financial Ledger, and Subscription Billing Engine  
**Stage:** Stage 4.3 — Razorpay Integration & Verified Subscription Settlement  
**Status:** **IMPLEMENTATION COMPLETE & VERIFIED — PENDING COMMISSION FORMULA SPECIFICATION**  
**Mode:** Stage 4.3 Baseline Delivered — **MANDATORY STOP CONDITION ENFORCED**. Payouts/Disbursements NOT started.  
**Target Alembic Head:** `20261021_19` (Chained linearly from `20261021_18`)  

---

## 1. Executive Summary

In accordance with explicit Project Owner authorization, **Phase 4, Stage 4.3 (Razorpay Integration, Verified Subscription Payments & Multi-Tier Referral Commissions Baseline)** has been implemented, tested, and verified end to end in the `D:\AVANFlix\OTT_Agent_App` repository.

Stage 4.3 integrates the official Razorpay Standard Checkout and Orders workflow with the existing Phase 4 subscription engine. It enforces that subscription amounts remain strictly manual business inputs, converts validated INR decimal amounts to exact integer paise, requires authoritative server-side HMAC-SHA256 signature verification and captured status checks before activating subscriptions, provides durable webhook deduplication, and establishes strict settlement boundaries separating unverified browser responses from subscription activations.

In accordance with explicit project instructions regarding multi-tier referral commissions, a comprehensive audit of existing documentation, architecture specifications, and database schemas was conducted. The audit established that while the `REFERRED_BY` relationship graph (depth up to 3), `Tenant.default_commission_rate` (10.00%), and `AgentProfile.commission_rate_override` exist in the baseline, the specific **Tier 1, Tier 2, and Tier 3 allocation formula, commissionable base deduction rules, and missing ancestor attribution behavior** remain undocumented. In strict adherence to the project owner's mandate:
> *"Do not invent commission percentages or silently assume that the same percentage should be paid to every tier. If a material formula remains undocumented, implement and test the Razorpay and subscription-payment portions that do not depend on that formula, then stop and ask the project owner for the missing rule before enabling the affected commission calculation."*

The complete Razorpay payment gateway integration, order creation, signature verification, webhook processing, and subscription settlement engine have been implemented and verified 100% green without inventing arbitrary commission splits. The missing commission attribution rules are formally surfaced to the Project Owner for clarification.

---

## 2. Core Deliverables Completed in Stage 4.3

### 2.1 Additive & Linear Database Migration (`20261021_19`)
- **File:** `backend/migrations/versions/20261021_19_razorpay_payments_and_lifecycle.py`
- **Revision ID:** `20261021_19` (Chained directly from `20261021_18`)
- **Single Linear Head Confirmed:** `['20261021_19']`
- **Tables Created:**
  1. `payment_orders`: Tracks server-side gateway order generation, tenant association, subscription link, amount in integer paise and decimal INR, status (`created`, `attempted`, `paid`, `failed`, `expired`), idempotency key, and metadata notes.
  2. `payment_transactions`: Authoritative record of payment settlement or failure, captured timestamp, gateway payment ID, gateway signature, error codes, and processed timestamp.
  3. `payment_webhook_events`: Durable audit and deduplication log of incoming Razorpay webhook event deliveries (`(gateway, event_id)` uniqueness constraint).
- **Constraints & Indexes:**
  - `ck_payment_orders_amount_paise` ($> 0$), `ck_payment_orders_amount` ($> 0.00$)
  - `ck_payment_tx_amount_paise` ($> 0$), `ck_payment_tx_amount` ($> 0.00$)
  - `uq_payment_orders_gateway_order` on `(gateway, gateway_order_id)`
  - `uq_payment_tx_gateway_payment` on `(gateway, gateway_payment_id)`
  - `uq_payment_webhook_event` on `(gateway, event_id)`
  - Composite foreign keys and tenant isolation indexes on `(tenant_id, subscription_id)`
- Clean reversible upgrade and downgrade verified on isolated databases.

### 2.2 Secure Configuration & Razorpay SDK Client Adapter
- Added server-side configuration to `backend/app/core/config.py`:
  - `razorpay_key_id: str | None = None`
  - `razorpay_key_secret: SecretStr | None = None`
  - `razorpay_webhook_secret: SecretStr | None = None`
  - `razorpay_mode: Literal["test", "live"] = "test"`
- Safe environment variables documented in `backend/.env.example` and `frontend/.env.example` (`NEXT_PUBLIC_RAZORPAY_KEY_ID`).
- Never hardcodes or commits secrets; secret keys are marked `SecretStr` and excluded from serialization and logs.
- Pinned `razorpay>=2.0,<3` in `backend/requirements.txt`.
- Built `RazorpayClientAdapter` in `backend/app/modules/payments/client.py`:
  - Uses official `razorpay.Client(auth=(key_id, key_secret))` for orders, payment details fetch, and HMAC verification.
  - Implements deterministic mock mode when credentials are unset in local test environments, ensuring test suites execute without external network dependencies.

### 2.3 Order Creation Workflow & Exact Monetary Arithmetic
- Mounted `POST /api/v1/payments/orders` in `backend/app/modules/payments/api.py`.
- Authenticates caller and verifies access to subscription within caller's tenant (`caller.tenant_id`).
- Strictly enforces that subscription must be in `PENDING_PAYMENT` and `UNPAID` state.
- **Manual Agreed Amount Integrity:** Server loads persisted `subscription.agreed_amount`; client cannot override or tamper with the payment amount.
- **Exact Paise Conversion:** Converts INR rupees to integer paise using fixed-point decimal arithmetic: `(agreed_amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)`. Zero or negative amounts reject with `HTTP 422`.
- Calls Razorpay Orders API server-side and stores the resulting order ID.
- Re-entrant and replay-safe via optional client-supplied `idempotency_key` and existing unexpired order reuse.

### 2.4 Server-Side Signature Verification & Subscription Settlement
- Mounted `POST /api/v1/payments/verify` in `backend/app/modules/payments/api.py`.
- Accepts `razorpay_order_id`, `razorpay_payment_id`, and `razorpay_signature`.
- Retrieves the authoritative expected order from PostgreSQL within `caller.tenant_id`. Client cannot supply an order ID from another tenant or subscription.
- Verifies HMAC-SHA256 signature server-side using the configured `key_secret`. Invalid signatures reject with `HTTP 400 Bad Request`.
- In production/live mode, queries Razorpay API to confirm payment status is `captured` and amounts match.
- **Authoritative State Transition:**
  - Locks subscription row with `SELECT ... FOR UPDATE` to serialize concurrent requests.
  - Idempotency guard: checks if `gateway_payment_id` has already been processed. Duplicate verification calls return HTTP 200 idempotently without re-executing activation or inserting duplicate records.
  - Records `PaymentTransaction` with status `captured`.
  - Transitions `MemberSubscription.status = ACTIVE` and `payment_status = SETTLED`.
  - Sets `starts_at = now()` and calculates `expires_at = starts_at + snapshot_duration_days`.
  - Emits structured audit event `subscription.payment.settled`.

### 2.5 Razorpay Webhook Ingestion & Deduplication
- Mounted `POST /api/v1/payments/webhook` in `backend/app/modules/payments/api.py`.
- Gated by `X-Razorpay-Signature` validation against raw binary request body (`await request.body()`) using `RAZORPAY_WEBHOOK_SECRET` and HMAC-SHA256. Unverified requests fail closed (`HTTP 400`).
- Webhook does not require browser cookies or CSRF tokens; it uses server-to-server signature authentication exclusively.
- Deduplicates incoming deliveries in `payment_webhook_events`: duplicate event deliveries return `HTTP 200` with `status="duplicate"` without re-executing downstream operations.
- Handles `payment.captured`: locates the associated `PaymentOrder` and executes the authoritative settlement transaction.
- Handles `payment.failed`: records failed `PaymentTransaction` with error code and description, preserving the subscription in `PENDING_PAYMENT` / `UNPAID` without corrupting agreed terms.
- Both Checkout verification and Webhook delivery converge idempotently on the exact same terminal settlement state.

### 2.6 RBAC Catalog Expansion & Role Seeding
- Registered in `backend/app/modules/authorization/catalog.py`:
  - `PAYMENT_READ = "payment.read"`
  - `PAYMENT_MANAGE = "payment.manage"`
- Catalog expanded to **40 total registered permissions**.
- Seeded in `backend/app/modules/authorization/seeding.py` and `20261012_04_centralized_tenant_rbac.py`:
  - **Tenant Admin:** Holds `payment.read` and `payment.manage`.
  - **Tenant Agent:** Holds `payment.read` and `payment.manage`.

### 2.7 Next.js Proxy Integration (`frontend/src/app/backend-api/[...path]/route.ts`)
- Added `isPaymentPath`, `paymentRoute`, and `proxyPayment` handlers.
- Enforces `community_session` authentication and CSRF token validation on `/api/v1/payments/orders` and `/api/v1/payments/verify`.
- Dedicated pass-through for `/api/v1/payments/webhook`: allows server-to-server webhook delivery, preserves exact binary body for HMAC validation, forwards `X-Razorpay-Signature`, and enforces a 64KB size cap.

---

## 3. Analysis of Multi-Tier Referral Commission Rules

### 3.1 Documented Baseline Elements Found in Repository
1. **Referral Hierarchy Lineage:** Phase 3 implemented `MemberRelationship` with `relationship_type = 'REFERRED_BY'` and depth capped at 3 tiers (Direct Agent = Tier 1, Parent Agent = Tier 2, Grandparent Agent = Tier 3).
2. **Community Default Commission:** `Tenant.default_commission_rate` is stored as `NUMERIC(5, 2)` (defaults to 10.00%).
3. **Agent Override Rate:** `AgentProfile.commission_rate_override` is stored as an optional `NUMERIC(5, 2)` (e.g., 15.00%).
4. **Effective Commission Helper:** `backend/app/modules/agents/api.py` defines `get_effective_commission(override, default) -> Decimal`, returning the override if present, else tenant default, else `10.00`.

### 3.2 Undocumented Business Rules Requiring Owner Decision
In accordance with prompt instructions, the following rules were found to be **undocumented** in the existing architecture and reports:
1. **Tier Allocation Formula:**
   - Does Tier 1 receive the direct agent's `effective_commission_rate` (e.g. 10%), while Tier 2 and Tier 3 receive fixed platform rates (e.g. 3% and 1%)?
   - Or does the tenant default rate represent a total commission pool that is split by percentage across tiers (e.g., 70% to Tier 1, 20% to Tier 2, 10% to Tier 3)?
   - Or does each upstream agent receive their own individual `effective_commission_rate`?
2. **Missing or Inactive Ancestor Attribution:**
   - If a subscriber's direct agent has no upstream referrer (Tier 2 and Tier 3 do not exist), does the unearned commission remain with the tenant/platform, or does it roll up?
   - If an upstream agent is `inactive` or `suspended`, is their tier allocation withheld or skipped?
3. **Commission Base & Deductions:**
   - Is commission calculated strictly on `subscription.agreed_amount`, or are gateway processing fees (e.g. Razorpay 2% fee) or GST deducted prior to attribution?

**Status:** Implementation of the commission calculation formula is paused pending the Project Owner's explicit business decision. Razorpay payment settlement, subscription activation, and ledger boundaries are fully operational and decoupled.

---

## 4. Automated Verification Matrix (100% Green)

| Test Suite | File | Tests | Duration | Outcome |
|---|---|:---:|:---:|:---:|
| **Razorpay Payments & Settlement** | `backend/tests/test_razorpay_payments_and_settlement.py` | 14 | 30.44s | **PASS (14/14)** |
| **Subscription & Billing Core** | `backend/tests/test_subscription_and_billing.py` | 14 | 25.07s | **PASS (14/14)** |
| **Financial Foundation Ledger** | `backend/tests/test_financial_foundation.py` | 13 | 23.38s | **PASS (13/13)** |
| **Security Migration Linear Head** | `backend/tests/test_security_migration.py` | 5 | 1.57s | **PASS (5/5)** |
| **RBAC Authorization Catalog** | `backend/tests/test_authorization.py` | 32 | 9.15s | **PASS (32/32)** |
| **Agent Lifecycle & SEC-07** | `backend/tests/test_agent_lifecycle_workflow.py` | 6 | 17.74s | **PASS (6/6)** |
| **Frontend Proxy Integration** | `frontend/test/proxy-integration.test.mjs` | 19 | 62.25s | **PASS (19/19)** |
| **Frontend Type Check** | `npm --prefix frontend run typecheck` | — | 7.20s | **PASS (0 errors)** |
| **Frontend Linter** | `npm --prefix frontend run lint` | — | 43.10s | **PASS (0 errors)** |
| **Frontend Next.js Build** | `npm --prefix frontend run build` | 29 routes | 52.30s | **PASS (0 errors)** |

---

## 5. Execution Boundaries & Stop Condition

Stage 4.3 Razorpay Payment Gateway integration and verified subscription settlement are fully verified. In accordance with strict instructions:

- **Agent payout requests, approvals, and disbursement workflows remain strictly OUT OF SCOPE.**
- The multi-tier commission formula has not been guessed or invented; clarification is requested from the Project Owner.
- No live Razorpay credentials have been committed or simulated.
- Work is halted pending Project Owner guidance on the commission formula.

