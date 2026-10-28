# Community OTT Agent Platform — Phase 4 Stage 4.3 Security and Regression Audit
## Razorpay Payment Gateway, Server-Side Verification, Webhook Deduplication, and Multi-Tier Referral Commission Attribution Audit

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Date:** October 21, 2026  
**Document Path:** `docs/reports/PHASE_4_STAGE_4_3_SECURITY_AND_REGRESSION_AUDIT.md`  
**Phase:** Phase 4 — Agent Referral Commissions, Financial Ledger, and Subscription Billing Engine  
**Stage:** Stage 4.3 — Razorpay Integration & Multi-Tier Referral Commissions  
**Audit Mode:** Independent Read-Only Security & Regression Acceptance Audit  
**Auditor:** Independent Security Acceptance Agent  
**Baseline References:**
- `docs/reports/PHASE_4_SCOPE_AND_ACCEPTANCE_PLAN.md`
- `docs/reports/PHASE_4_STAGE_4_3_IMPLEMENTATION_REPORT.md`
- `docs/reports/PHASE_4_STAGE_4_2_SECURITY_AND_REGRESSION_AUDIT.md` (Stage 4.2 Verdict: `ACCEPTED`)
- `docs/reports/PHASE_4_STAGE_4_1_SECURITY_AND_REGRESSION_AUDIT.md` (Stage 4.1 Verdict: `ACCEPTED`)
- `docs/reports/PHASE_3_FINAL_ACCEPTANCE_AUDIT.md` (Phase 3 Verdict: `ACCEPTED`, SEC-07 closed)
- `docs/reports/PHASE_3_SEC07_REMEDIATION_REPORT.md`
- `docs/reports/PHASE_2_FINAL_SECURITY_ACCEPTANCE_AUDIT.md`

---

## 1. Executive Summary & Audit Verdict

### Stage 4.3 Final Audit Verdict: **`ACCEPTED` (Full Stage 4.3 Scope Complete & Verified)**

An independent, evidence-based security and regression audit was conducted on the Stage 4.3 Razorpay Payment Gateway integration, verified subscription settlement, and multi-tier referral commission attribution implementation in `D:\AVANFlix\OTT_Agent_App`.

The audit evaluated server-side credential isolation, manual agreed amount non-tampering, exact integer paise conversion arithmetic, server-side HMAC-SHA256 signature verification, webhook raw binary verification and idempotency deduplication, authoritative subscription lifecycle transitions, double-entry financial ledger integration, multi-tier referral attribution across the `REFERRED_BY` hierarchy (depth up to 3), missing/inactive ancestor retention invariants, Next.js proxy perimeter defenses, multi-tenant boundary isolation, RBAC catalog expansion, and zero regression against historical security invariants (specifically SEC-01 through SEC-07).

### Summary of Audit Findings

1. **Secret Key Confinement (PAY-01):**
   - Razorpay secret keys (`razorpay_key_secret`, `razorpay_webhook_secret`) are strictly typed as `SecretStr` in `backend/app/core/config.py`. They are loaded exclusively from server-side environment variables, never hardcoded, never serialized to API responses, and never exposed to the frontend browser bundle. Only the public `razorpay_key_id` (`NEXT_PUBLIC_RAZORPAY_KEY_ID`) is shared with client-side checkout scripts.
2. **Manual Agreed Amount Non-Tampering (PAY-02):**
   - Order creation (`POST /api/v1/payments/orders`) retrieves the subscription's manual `agreed_amount` directly from PostgreSQL within the caller's verified `tenant_id`. Clients cannot pass or manipulate the monetary amount in order creation or settlement payloads. Subscriptions in any status other than `PENDING_PAYMENT` and `UNPAID` are rejected with `HTTP 400 Bad Request`.
3. **Exact Integer Paise Conversion Arithmetic (PAY-03):**
   - Subscriptions store INR currency in decimal format (`agreed_amount NUMERIC(12, 2)`). The payment service converts this to exact integer paise ($1.00 \to 100 \text{ paise}$) using fixed-point decimal arithmetic: `(agreed_amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)`. Zero or negative amounts fail closed with `HTTP 422 Unprocessable Content`. Database check constraints (`ck_payment_orders_amount_paise > 0`, `ck_payment_tx_amount_paise > 0`) enforce this at the persistence layer.
4. **Authoritative Server-Side HMAC-SHA256 Signature Verification (PAY-04):**
   - Verification endpoint (`POST /api/v1/payments/verify`) mandates `razorpay_order_id`, `razorpay_payment_id`, and `razorpay_signature`. The server recomputes the HMAC-SHA256 signature using the secret key and strictly rejects mismatched signatures with `HTTP 400 Bad Request` (`INVALID_PAYMENT_SIGNATURE`). Client-side claims of payment completion without valid cryptographic signatures cannot activate any subscription.
5. **Gateway Payment Fetch & Capture Verification (PAY-05):**
   - In production mode, the server queries the Razorpay API to verify that the payment is in `captured` status and that the amount captured matches the persisted order amount before executing settlement.
6. **Authoritative Subscription Settlement & Row Locking (PAY-06):**
   - Subscriptions transition from `PENDING_PAYMENT` / `UNPAID` to `ACTIVE` / `SETTLED` exclusively inside a row-locked transaction (`SELECT ... FOR UPDATE`). Idempotency is enforced at both the database level (`uq_payment_tx_gateway_payment`) and application level. Multiple checkout verification or webhook calls for the same payment return `HTTP 200` idempotently without creating duplicate ledger records or extending durations.
7. **Webhook Raw Binary Body HMAC Verification (PAY-07):**
   - Webhook endpoint (`POST /api/v1/payments/webhook`) validates `X-Razorpay-Signature` against the exact binary bytes of the incoming request body (`await request.body()`) using `RAZORPAY_WEBHOOK_SECRET`. Requests with missing or invalid signatures fail closed with `HTTP 400 Bad Request`. Webhook delivery does not depend on browser cookies or CSRF tokens.
8. **Durable Webhook Ingestion Deduplication (PAY-08):**
   - Incoming webhook events are recorded in `payment_webhook_events` with a unique constraint on `(gateway, event_id)`. Replays or duplicate webhook deliveries return `HTTP 200` with `status="duplicate"` immediately, preventing double processing.
9. **Financial Ledger Separation & Pre-Settlement Isolation (PAY-09):**
   - Generating a payment order does not create or post entries to the Stage 4.1 double-entry ledger (`commission_journals` and `commission_ledger_entries`). Failed payment transactions are logged with error metadata but produce zero ledger movements ($\Delta \text{Journals} = 0$).
10. **Multi-Tenant Boundary Isolation (PAY-10):**
    - `PaymentOrder` and `PaymentTransaction` records mandate `tenant_id`. All checkout and verification operations enforce that the requested subscription belongs to `caller.tenant_id`. Cross-tenant references return `HTTP 404 Not Found`.
11. **Next.js API Proxy Perimeter Security (PAY-11):**
    - The Next.js API proxy routes `/api/v1/payments/orders` and `/api/v1/payments/verify` through session verification (`community_session`), CSRF validation, and a 32KB payload size cap. The `/api/v1/payments/webhook` endpoint bypasses browser cookie/CSRF checks, passes through the raw binary body to preserve HMAC verification, and enforces a strict 64KB payload size cap.
12. **Zero Security Regressions (PAY-12 / SEC-01 to SEC-07):**
    - The critical SEC-07 vulnerability remains closed: `PATCH /api/v1/agents/{agent_id}` returns `HTTP 403 Forbidden` if `status` modification is attempted. Super Admin lifecycle approval remains the sole valid mechanism for agent state transitions. Presigned R2 photo URLs, AES-256-GCM encryption, and appointment booking continue to operate without regression.
13. **Multi-Tier Lineage Attribution Accuracy (COMM-01):**
    - Upon verified payment capture, `CommissionAttributionService` traverses `MemberRelationship` (`REFERRED_BY`) up to 3 tiers:
      - Tier 1: Agent override rate if set, else tenant default (10.00%).
      - Tier 2: Fixed 3.00%.
      - Tier 3: Fixed 1.00%.
    - Verified in `backend/tests/test_referral_commission_attribution.py`: 100% accurate attribution across 1, 2, and 3 tiers.
14. **Missing & Inactive Ancestor Retention Invariant (COMM-02):**
    - When an upstream ancestor is missing or inactive, their unearned commission is strictly retained by the tenant/platform and is **never rolled up** to direct agents or other ancestors. Inactive agents receive 0 ledger postings.
15. **Gross Agreed Amount Base Invariant (COMM-03):**
    - Referral commissions are calculated directly on the gross subscription `agreed_amount` without gateway fee deductions.
16. **Double-Entry Financial Ledger Posting Invariant (COMM-04):**
    - Multi-tier commission attribution posts balanced journals to `commission_journals` (debit `COMMUNITY_EXPENSE`, credit `AGENT_COMMISSION`). Re-running attribution is strictly idempotent ($\Delta \text{Journals} = 0$, balance unchanged). Direct signups without referrers produce zero ledger entries.

---

## 2. Environment Baseline & Git Working Tree

- **Working Directory:** `D:\AVANFlix\OTT_Agent_App`
- **Current Alembic Revision:** `20261021_19` (`20261021_19_razorpay_payments_and_lifecycle.py`), chaining linearly from `20261021_18`.
- **Alembic Heads:** Single linear head `['20261021_19']` verified.
- **Python Runtime:** Python 3.12.3 in `backend/.venv`
- **Node.js Runtime:** Next.js 14.2.35 in `frontend/`
- **RBAC Catalog:** 40 permissions registered and seeded.

---

## 3. Stage 4.3 Security & Regression Invariant Matrix (PAY-01 to COMM-04)

| Invariant ID | Severity | Category | Description | Implementation File & Lines | Audit Result |
|---|---|---|---|---|---|
| **PAY-01** | **P0 (Critical)** | Credential Security | Secret Key Confinement: Secrets typed as `SecretStr`, server-only, never sent to client or logged. | `backend/app/core/config.py#L42-L55`<br>`backend/app/modules/payments/client.py#L30-L50` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-02** | **P0 (Critical)** | Business Logic | Manual Agreed Amount Non-Tampering: Order amount pulled from DB `agreed_amount`; client cannot supply price. | `backend/app/modules/payments/service.py#L80-L105`<br>`backend/app/modules/payments/schemas.py#L15-L25` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-03** | **P0 (Critical)** | Monetary Integrity | Exact Integer Paise Conversion: $1.00 \to 100\text{ paise}$, fixed-point `ROUND_HALF_UP`, non-positive rejected. | `backend/app/modules/payments/service.py#L98-L108`<br>`backend/migrations/versions/20261021_19_razorpay_payments_and_lifecycle.py#L42-L65` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-04** | **P0 (Critical)** | Cryptographic Verification | Server-Side HMAC-SHA256 Verification: Recomputed with secret key; invalid signature rejects with 400. | `backend/app/modules/payments/client.py#L82-L105`<br>`backend/app/modules/payments/service.py#L145-L165` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-05** | **P0 (Critical)** | Payment Verification | Gateway Capture Status Check: Payment must be `captured` and amount matched before settlement. | `backend/app/modules/payments/client.py#L65-L80`<br>`backend/app/modules/payments/service.py#L155-L170` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-06** | **P0 (Critical)** | Lifecycle State Machine | Authoritative Subscription Transition: `PENDING_PAYMENT` $\to$ `ACTIVE`, row locking, idempotent replay. | `backend/app/modules/payments/service.py#L175-L230`<br>`backend/app/modules/subscriptions/models.py#L80-L95` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-07** | **P0 (Critical)** | Cryptographic Verification | Webhook Raw Binary HMAC: Validates `X-Razorpay-Signature` against `request.body()`; fails closed. | `backend/app/modules/payments/api.py#L60-L85`<br>`backend/app/modules/payments/service.py#L235-L260` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-08** | **P1 (High)** | Data Integrity | Webhook Deduplication: `(gateway, event_id)` uniqueness; replayed events return 200 without side effects. | `backend/app/modules/payments/models.py#L85-L105`<br>`backend/app/modules/payments/service.py#L250-L270` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-09** | **P0 (Critical)** | Financial Separation | Zero Pre-Settlement Ledger Postings: Order creation and failed transactions post zero journals. | `backend/app/modules/payments/service.py#L110-L135`<br>`backend/tests/test_razorpay_payments_and_settlement.py#L190-L225` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-10** | **P0 (Critical)** | Multi-Tenancy | Tenant Isolation: Orders and transactions enforce `tenant_id`; cross-tenant access returns HTTP 404. | `backend/app/modules/payments/service.py#L82-L95`<br>`backend/app/modules/payments/api.py#L35-L55` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-11** | **P1 (High)** | Proxy Perimeter | Next.js Proxy Integration: Session/CSRF on orders/verify; raw body pass-through & 64KB cap on webhook. | `frontend/src/app/backend-api/[...path]/route.ts#L1400-L1495` | **`VERIFIED_AND_ACCEPTED`** |
| **PAY-12** | **P0 (Critical)** | Regression Baseline | Zero Security Regressions: SEC-01 through SEC-07 intact (`PATCH /api/v1/agents/{id}` returns 403). | `backend/tests/test_agent_lifecycle_workflow.py#L380-L425` | **`VERIFIED_AND_ACCEPTED`** |
| **COMM-01** | **P0 (Critical)** | Financial Foundation | Multi-Tier Hierarchy Lineage Attribution: Tier 1 (effective rate), Tier 2 (3%), Tier 3 (1%) accurately attributed. | `backend/app/modules/commissions/service.py#L615-L750`<br>`backend/tests/test_referral_commission_attribution.py` | **`VERIFIED_AND_ACCEPTED`** |
| **COMM-02** | **P0 (Critical)** | Business Logic | Inactive & Missing Ancestor Retention: Unearned commissions retained by tenant; roll-up strictly prohibited. | `backend/app/modules/commissions/service.py#L650-L720`<br>`backend/tests/test_referral_commission_attribution.py#L470-L680` | **`VERIFIED_AND_ACCEPTED`** |
| **COMM-03** | **P1 (High)** | Monetary Integrity | Gross Base Calculation: Commissions calculated on gross subscription agreed amount without deductions. | `backend/app/modules/commissions/service.py#L620-L640` | **`VERIFIED_AND_ACCEPTED`** |
| **COMM-04** | **P0 (Critical)** | Ledger Integrity | Double-Entry Posting: Debit `COMMUNITY_EXPENSE`, credit `AGENT_COMMISSION`; balanced and replay-safe. | `backend/app/modules/commissions/service.py#L740-L800`<br>`backend/tests/test_referral_commission_attribution.py#L800-L920` | **`VERIFIED_AND_ACCEPTED`** |

---

## 4. Automated Verification Matrix (100% Green)

The following verification suite was executed live against the current working directory:

| Test Suite | Test File | Test Count | Execution Time | Result |
|---|---|:---:|:---:|:---:|
| **Referral Commission Attribution** | `backend/tests/test_referral_commission_attribution.py` | 7 | 6.17s | **PASS (7/7)** |
| **Razorpay Payments & Settlement** | `backend/tests/test_razorpay_payments_and_settlement.py` | 14 | 15.03s | **PASS (14/14)** |
| **Subscription & Package Billing** | `backend/tests/test_subscription_and_billing.py` | 14 | 24.12s | **PASS (14/14)** |
| **Financial Foundation Ledger** | `backend/tests/test_financial_foundation.py` | 13 | 21.05s | **PASS (13/13)** |
| **Additive Security Migration** | `backend/tests/test_security_migration.py` | 5 | 1.62s | **PASS (5/5)** |
| **RBAC Authorization Catalog** | `backend/tests/test_authorization.py` | 32 | 8.84s | **PASS (32/32)** |
| **Agent Lifecycle & SEC-07** | `backend/tests/test_agent_lifecycle_workflow.py` | 6 | 16.90s | **PASS (6/6)** |
| **Frontend Proxy Integration** | `frontend/test/proxy-integration.test.mjs` | 19 | 29.56s | **PASS (19/19)** |
| **Frontend TypeScript Typecheck** | `npm --prefix frontend run typecheck` | — | 6.00s | **PASS (0 errors)** |
| **Frontend Next.js ESLint** | `npm --prefix frontend run lint` | — | 6.00s | **PASS (0 errors)** |
| **Frontend Production Build** | `npm --prefix frontend run build` | 29 routes | 44.00s | **PASS (0 errors)** |

**Total Automated Tests:** **110 / 110 passed (100% Green)**.

---

## 5. Audit Verdict & Next Stage Authorization

### Audit Conclusion
Stage 4.3 (Razorpay Integration, Verified Subscription Payments & Multi-Tier Referral Commissions) satisfies all architecture, security, cryptographic, financial ledger, and regression requirements.
The verdict is **`ACCEPTED`**.

### Transition to Stage 4.4
The platform is now fully primed for **Stage 4.4: Agent Payout Governance & Workflow** (Payout requests, community admin review, platform operator disbursement, anti-tampering, and self-approval prevention).

