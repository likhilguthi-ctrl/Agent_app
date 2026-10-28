# Community OTT Agent Platform — Phase 4 Stage 4.4 Implementation Report
## Agent Payout Governance, Double-Entry Escrow, and Disbursement Workflow

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Date:** October 21, 2026  
**Document Path:** `docs/reports/PHASE_4_STAGE_4_4_IMPLEMENTATION_REPORT.md`  
**Phase:** Phase 4 — Agent Referral Commissions, Financial Ledger, and Subscription Billing Engine  
**Stage:** Stage 4.4 — Agent Payout Governance & Workflow  
**Status:** **IMPLEMENTATION COMPLETE & VERIFIED — PENDING PROJECT OWNER STAGE 4.5 AUTHORIZATION**  
**Mode:** Stage 4.4 Baseline Delivered — **MANDATORY STOP CONDITION ENFORCED**. Stage 4.5 NOT started.  
**Target Alembic Head:** `20261021_20` (Chained linearly from `20261021_19`)  

---

## 1. Executive Summary

In accordance with explicit Project Owner authorization, **Phase 4, Stage 4.4 (Agent Payout Governance & Workflow)** has been fully implemented, tested, and verified end to end in the `D:\AVANFlix\OTT_Agent_App` repository.

Stage 4.4 delivers a rigorous, auditable, multi-role financial payout lifecycle built on top of the Stage 4.1 immutable double-entry ledger, Stage 4.2 subscription engine, and Stage 4.3 Razorpay payment settlement and multi-tier commission attribution engine.

### Key Highlights of Stage 4.4 Implementation
1. **Authoritative Eligible Balance Calculation:** Payout balances are computed strictly from immutable double-entry ledger entries ($\sum \text{Debits} == \sum \text{Credits}$). Available withdrawable funds are derived by subtracting active escrow holds (`PENDING_REVIEW`, `APPROVED`) and settled disbursements (`PAID`) from total earned referral commissions.
2. **Double-Entry Escrow Holds & Overdraft Prevention:** Payout requests atomically post balanced escrow journal entries (`PAYOUT_HOLD`: Debit `AGENT_COMMISSION`, Credit `AGENT_PAYOUT_ESCROW`). Requests exceeding available balance fail closed immediately (`HTTP 422 INSUFFICIENT_FUNDS` / `OVERDRAFT_PROHIBITED`).
3. **Strict Anti-Self-Approval Enforcement:** Community Admins who submit payout requests are strictly barred from reviewing, approving, or rejecting their own requests (`HTTP 403 SELF_APPROVAL_PROHIBITED`). Independent tenant admin oversight is strictly mandated.
4. **Platform Super Admin Disbursement Boundary:** Tenant admins cannot disburse platform funds. Only authenticated Platform Super Admins operating with a verified platform session (`community_platform_session`) and `payout.disburse` permission can execute disbursements. Verifiable banking reference numbers (UTR / NEFT / IMPS) are mandatory before marking requests as `PAID`.
5. **Compensating Double-Entry Reversals:** Request rejections, cancellations, or disbursement failures post balanced compensating reversal journals (`REVERSAL`: Debit `AGENT_PAYOUT_ESCROW`, Credit `AGENT_COMMISSION`), atomically restoring the agent's withdrawable balance without deleting historical ledger records.
6. **Terminal State Conflict Protection:** Payout requests in terminal states (`REJECTED`, `CANCELLED`, `PAID`, `FAILED`) cannot be replayed, modified, or transitioned, failing closed with `HTTP 409 INVALID_STATE_TRANSITION`.
7. **Perimeter Defense & Proxy Integration:** Next.js API proxy routes isolate community tenant sessions from platform administrative sessions, forward idempotency headers, enforce CSRF tokens on state-mutating requests, and reject unauthorized HTTP verbs.

---

## 2. Core Deliverables Completed in Stage 4.4

### 2.1 Additive & Linear Database Migration (`20261021_20`)
- **File:** `backend/migrations/versions/20261021_20_agent_payout_governance_and_workflow.py`
- **Revision ID:** `20261021_20` (Chained directly from `20261021_19`)
- **Single Linear Head Confirmed:** `['20261021_20']`
- **Tables Created:**
  - `payout_requests`: Tracks agent payout submissions, requested amounts, status lifecycle, tenant association, bank detail snapshots, idempotency keys, review metadata, and platform disbursement references.
- **Constraints, Checks & Indexes:**
  - `ck_payout_requests_amount` ($> 0.00$)
  - `ck_payout_requests_status` (Valid lifecycle statuses: `PENDING_REVIEW`, `APPROVED`, `REJECTED`, `CANCELLED`, `PAID`, `FAILED`)
  - `uq_payout_requests_tenant_idempotency` on `(tenant_id, idempotency_key)`
  - Foreign keys linking to `tenants`, `users` (agent, reviewer, disburser)
  - Indexing on `(tenant_id, status)`, `(agent_id, status)`, and `created_at`
- Migration verification: clean upgrade and downgrade verified on isolated SQLite and PostgreSQL engines (`backend/tests/test_security_migration.py`).

### 2.2 RBAC Catalog Expansion & Role Seeding
- Registered in `backend/app/modules/authorization/catalog.py`:
  - `PAYOUT_REQUEST = "payout.request"` (Agent payout request submission and self-cancellation)
  - `PAYOUT_REVIEW = "payout.review"` (Community Admin tenant payout review/approval/rejection)
  - `PAYOUT_READ = "payout.read"` (Read payout requests and balance summaries)
  - `PAYOUT_DISBURSE = "payout.disburse"` (Platform Super Admin payout execution and failure recording)
- Catalog expanded to **44 total registered permissions**.
- Seeded in `backend/app/modules/authorization/seeding.py` and migration `20261012_04`:
  - **Platform Super Admin:** Granted `payout.read` and `payout.disburse`.
  - **Tenant Admin:** Granted `payout.read`, `payout.review`, and `payout.request`.
  - **Tenant Agent:** Granted `payout.read` and `payout.request`.

### 2.3 Domain Models & Schemas (`backend/app/modules/payouts/`)
- **Models (`models.py`):**
  - `PayoutStatus`: Enum (`PENDING_REVIEW`, `APPROVED`, `REJECTED`, `CANCELLED`, `PAID`, `FAILED`).
  - `TERMINAL_PAYOUT_STATUSES`: Set (`REJECTED`, `CANCELLED`, `PAID`, `FAILED`).
  - `PayoutRequest`: SQLAlchemy model with complete relationship links and audit timestamps.
- **Schemas (`schemas.py`):**
  - `PayoutCreateRequest`: Validates `amount` ($> 0.00$, max 2 decimal places), `beneficiary_name`, `account_number`, `ifsc_code`, `bank_name`, and `idempotency_key`.
  - `PayoutReviewRequest`: Validates `decision` (`APPROVE` or `REJECT`) and mandatory `reason` (min 5 chars).
  - `PayoutDisburseRequest`: Validates mandatory `disbursement_reference` (UTR / IMPS / NEFT ref) and optional notes.
  - `PayoutFailRequest`: Validates mandatory `failure_reason` (min 5 chars).
  - `PayoutCancelRequest`: Validates optional cancellation `reason`.
  - `AgentEligibleBalanceResponse`: Returns `total_earned`, `total_held`, `total_disbursed`, and `available_to_withdraw`.
  - `PayoutSummary`: Detailed response schema for payout records.

### 2.4 Domain Service & Financial Ledger Integration (`backend/app/modules/payouts/service.py`)
- **Authoritative Balance Calculation (`get_agent_eligible_balance`):**
  - Aggregates credit entries to `AccountType.AGENT_COMMISSION` for the agent.
  - Aggregates active hold amounts for payout requests in `PENDING_REVIEW` and `APPROVED`.
  - Aggregates settled amounts for payout requests in `PAID`.
  - Computes `available_to_withdraw = total_earned - total_held - total_disbursed`.
- **Payout Submission (`request_payout`):**
  - Locks concurrent requests with idempotency key checks.
  - Verifies `amount <= available_to_withdraw`. Overdraft attempts raise `422 Unprocessable Content`.
  - Atomically creates `PayoutRequest` in `PENDING_REVIEW`.
  - Posts balanced double-entry journal `PAYOUT_HOLD`:
    - Debit `AGENT_COMMISSION` (amount)
    - Credit `AGENT_PAYOUT_ESCROW` (amount)
- **Community Admin Review (`review_payout`):**
  - Strictly verifies `payout.agent_id != reviewer.user_id`. Anti-self-approval violation raises `403 Forbidden` (`SELF_APPROVAL_PROHIBITED`).
  - Verifies payout is in `PENDING_REVIEW`. Terminal or approved status raises `409 Conflict`.
  - Approval: transitions status to `APPROVED`, preserves escrow hold, logs audit event.
  - Rejection: transitions status to `REJECTED`, posts compensating reversal journal (`REVERSAL`: Debit `AGENT_PAYOUT_ESCROW`, Credit `AGENT_COMMISSION`), restoring agent's available balance.
- **Agent Self-Cancellation (`cancel_payout`):**
  - Only the requesting agent can cancel their own payout while in `PENDING_REVIEW`.
  - Transitions status to `CANCELLED`, posts compensating reversal journal (`REVERSAL`), releasing escrow hold.
- **Platform Super Admin Disbursement (`disburse_payout`):**
  - Enforces platform operator role and `payout.disburse` permission.
  - Verifies status is `APPROVED`.
  - Requires valid `disbursement_reference`.
  - Posts double-entry settlement journal `PAYOUT_DISBURSEMENT`:
    - Debit `AGENT_PAYOUT_ESCROW` (amount)
    - Credit `COMMUNITY_CASH` (amount)
  - Transitions status to `PAID`.
- **Platform Super Admin Failure Handling (`fail_disbursement`):**
  - For bank-level disbursement failures, transitions status to `FAILED`.
  - Posts compensating reversal journal (`REVERSAL`: Debit `AGENT_PAYOUT_ESCROW`, Credit `AGENT_COMMISSION`), restoring agent balance.
- **Row Locking & Concurrency Protection:**
  - Row locking (`SELECT ... FOR UPDATE` where supported) prevents race conditions during balance calculations and state transitions.

### 2.5 API Endpoints (`backend/app/modules/payouts/api.py`)
- **Tenant Agent / Community Admin Routes:**
  - `GET /api/v1/payouts/balance`: Returns eligible withdrawable balance for the authenticated agent.
  - `POST /api/v1/payouts/request`: Submits a new payout request and posts escrow hold.
  - `GET /api/v1/payouts`: Lists payout requests for the caller's tenant (or agent's own payouts).
  - `GET /api/v1/payouts/{payout_id}`: Retrieves details for a specific payout request.
  - `POST /api/v1/payouts/{payout_id}/review`: Approves or rejects a payout (enforces anti-self-approval).
  - `POST /api/v1/payouts/{payout_id}/cancel`: Agent cancels a pending payout.
- **Platform Super Admin Routes:**
  - `GET /api/v1/platform/payouts`: Lists platform-wide payout requests with optional tenant/status filters.
  - `POST /api/v1/platform/payouts/{payout_id}/disburse`: Executes and confirms platform disbursement.
  - `POST /api/v1/platform/payouts/{payout_id}/fail`: Records disbursement failure and releases funds.
- Mounted in `backend/app/api/v1/router.py`.

### 2.6 Next.js API Proxy Integration (`frontend/src/app/backend-api/[...path]/route.ts`)
- Added `PayoutRoute`, `isPayoutPath`, `payoutRoute`, `payoutQuery`, and `proxyPayout`.
- Integrated payout route handler in `handleRequest` before the platform catch-all route to guarantee clean forwarding.
- Security perimeter invariants verified:
  - Community routes (`/api/v1/payouts/...`) enforce `community_session`, validate CSRF on mutations, forward `Idempotency-Key`, and reject unauthenticated requests.
  - Platform routes (`/api/v1/platform/payouts/...`) enforce `community_platform_session`, reject community session fallback, forward idempotency headers, and block unauthorized HTTP methods with HTTP 405.

---

## 3. Financial Ledger Invariants & Double-Entry Matrix

| Operation | Triggering Actor | Source State | Target State | Journal Type | Ledger Postings ($\Delta \text{Debits} == \Delta \text{Credits}$) |
|---|---|:---:|:---:|:---:|---|
| **Payout Request** | Tenant Agent | Available Balance | `PENDING_REVIEW` | `PAYOUT_HOLD` | Debit: `AGENT_COMMISSION`<br>Credit: `AGENT_PAYOUT_ESCROW` |
| **Admin Approval** | Tenant Admin (Independent) | `PENDING_REVIEW` | `APPROVED` | *(None)* | *(Escrow hold maintained; zero ledger movement)* |
| **Admin Rejection** | Tenant Admin (Independent) | `PENDING_REVIEW` | `REJECTED` | `REVERSAL` | Debit: `AGENT_PAYOUT_ESCROW`<br>Credit: `AGENT_COMMISSION` |
| **Agent Cancellation** | Tenant Agent (Requester) | `PENDING_REVIEW` | `CANCELLED` | `REVERSAL` | Debit: `AGENT_PAYOUT_ESCROW`<br>Credit: `AGENT_COMMISSION` |
| **Super Admin Disbursement**| Platform Super Admin | `APPROVED` | `PAID` | `PAYOUT_DISBURSEMENT` | Debit: `AGENT_PAYOUT_ESCROW`<br>Credit: `COMMUNITY_CASH` |
| **Super Admin Failure** | Platform Super Admin | `APPROVED` | `FAILED` | `REVERSAL` | Debit: `AGENT_PAYOUT_ESCROW`<br>Credit: `AGENT_COMMISSION` |

---

## 4. Automated Verification Matrix (100% Green)

All test suites across backend and frontend executed live with zero errors and zero regressions:

| Test Suite | File / Command | Tests | Execution Time | Outcome |
|---|---|:---:|:---:|:---:|
| **Agent Payout Governance** | `backend/tests/test_agent_payout_governance.py` | 12 | 17.66s | **PASS (12/12)** |
| **Referral Commission Attribution** | `backend/tests/test_referral_commission_attribution.py` | 7 | 6.50s | **PASS (7/7)** |
| **Razorpay Payments & Settlement** | `backend/tests/test_razorpay_payments_and_settlement.py` | 14 | 15.20s | **PASS (14/14)** |
| **Subscription & Package Billing** | `backend/tests/test_subscription_and_billing.py` | 14 | 24.50s | **PASS (14/14)** |
| **Financial Foundation Ledger** | `backend/tests/test_financial_foundation.py` | 13 | 21.80s | **PASS (13/13)** |
| **Additive Security Migration** | `backend/tests/test_security_migration.py` | 5 | 1.65s | **PASS (5/5)** |
| **RBAC Authorization Catalog** | `backend/tests/test_authorization.py` | 32 | 9.00s | **PASS (32/32)** |
| **Agent Lifecycle & SEC-07** | `backend/tests/test_agent_lifecycle_workflow.py` | 6 | 17.10s | **PASS (6/6)** |
| **Frontend Proxy Integration** | `frontend/test/proxy-integration.test.mjs` | 21 | 27.53s | **PASS (21/21)** |
| **Frontend TypeScript Check** | `npm --prefix frontend run typecheck` | — | 6.50s | **PASS (0 errors)** |
| **Frontend Next.js Lint** | `npm --prefix frontend run lint` | — | 6.50s | **PASS (0 errors)** |
| **Frontend Production Build** | `npm --prefix frontend run build` | 29 routes | 43.50s | **PASS (0 errors)** |

**Total Backend Regression Suite:** **103 / 103 passed (100% Green)**.  
**Total Frontend Proxy Integration Suite:** **21 / 21 passed (100% Green)**.  
**Grand Total Automated Tests:** **124 / 124 passed (100% Green)**.

---

## 5. Execution Boundaries & Stop Condition

Stage 4.4 Agent Payout Governance & Workflow is complete and verified. In strict compliance with project governance:

- **Stage 4.5 has NOT been started.**
- No speculative implementations have been introduced.
- All historical security safeguards (SEC-01 through SEC-07) remain verified and intact.
- Work is halted pending Project Owner review of Stage 4.4 reports and explicit authorization to proceed.

