# Community OTT Agent Platform — Stage 5.0 Staging UAT & Release Checklist

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Date:** October 22, 2026  
**Auditor:** Automated System Audit & Architecture Verification Framework  
**Scope:** Staging User Acceptance Testing (UAT) Test Plan, Release Gates & Go/No-Go Verification Matrix  
**Status:** `CHECKLIST PROPOSED — STRICT READ-ONLY MODE (AWAITING OWNER AUTHORIZATION)`  

---

## 1. Executive Overview & Test Objectives

This document establishes the official **Staging User Acceptance Testing (UAT) & Production Release Checklist** for the **Community OTT Agent Platform**.

The objective is to provide a deterministic, verifiable operational protocol that validates all platform capabilities across all user roles (Platform Super Admin, Community Admin, Agent, and Subscriber) in a production-identical staging environment before authorizing commercial production traffic.

---

## 2. Staging UAT Environment Prerequisites

Prior to initiating the UAT test cycles, the following environmental baseline must be verified:

1. **Database:** PostgreSQL staging database migrated to Alembic linear head `20261021_20 (head)`.
2. **Backend API:** FastAPI running in staging mode with HTTPS URL configured.
3. **Frontend Application:** Next.js application built with production optimization (`NODE_ENV=production`).
4. **Tenant Configuration:** At least two distinct community tenants provisioned (e.g., `alpha.avanflix.com` and `beta.avanflix.com`) to verify cross-tenant data isolation.
5. **Messaging Service:** Staging notification service configured with live SMS/WhatsApp test credentials.
6. **Object Storage:** Private Cloudflare R2 staging bucket provisioned and configured.
7. **Payment Gateway:** Razorpay test/sandbox mode configured with active webhooks.

---

## 3. Comprehensive 12-Journey Staging UAT Test Plan

---

### Journey 01: Operator-Mediated Tenant Onboarding & Activation
- **User Role:** Platform Super Admin / Prospective Community Admin.
- **Prerequisites:** Valid domain name with DNS access for TXT challenge verification.
- **Execution Steps:**
  1. Community applicant submits onboarding request with tenant metadata and domain name (`tenant-uat.avanflix.com`).
  2. Applicant retrieves DNS TXT challenge token from system.
  3. Applicant configures `_avanflix-challenge.tenant-uat.avanflix.com` TXT record in DNS provider.
  4. Applicant triggers challenge verification via `POST /api/v1/onboarding/verify-domain`.
  5. Platform Super Admin reviews verified onboarding request and executes operator approval.
  6. Super Admin triggers tenant activation intent.
- **Expected Results:**
  - System verifies DNS TXT record within timeout.
  - Tenant status transitions: `DRAFT` $\to$ `PENDING_REVIEW` $\to$ `VERIFIED` $\to$ `ACTIVE`.
  - Tenant database records and default configuration partitions created.
  - Activation completion event recorded in outbox table.
- **Evidence to Capture:**
  - Tenant UUID, DNS TXT record query output, Super Admin audit log entry.

---

### Journey 02: Mobile OTP Authentication & Anti-Abuse Rate Limiting
- **User Role:** Community Member / Agent.
- **Prerequisites:** Valid E.164 mobile number (+91XXXXXXXXXX).
- **Execution Steps:**
  1. Submit OTP request for unregistered mobile number via `POST /api/v1/auth/otp/request`.
  2. Verify system returns generic HTTP 202 Accepted (anti-enumeration defense).
  3. Submit OTP request for registered mobile number.
  4. Verify SMS OTP arrives within 15 seconds.
  5. Attempt 4 consecutive OTP requests within 60 seconds to test rate limiting.
  6. Attempt 5 consecutive invalid OTP code entries to test brute-force defense.
  7. Submit correct OTP code to complete authentication.
- **Expected Results:**
  - Anti-enumeration: Identical HTTP 202 response regardless of registration status.
  - Sliding-window rate limit triggers HTTP 429 Too Many Requests on the 4th request.
  - Brute-force lockout triggers after 5 failed attempts with challenge invalidation.
  - Valid OTP returns partitioned HttpOnly session cookie (`community_session`).
- **Evidence to Capture:**
  - SMS delivery timestamp, HTTP 429 response body, session cookie headers.

---

### Journey 03: Platform Super Admin Subscription Plan Management
- **User Role:** Platform Super Admin.
- **Prerequisites:** Authenticated Super Admin session.
- **Execution Steps:**
  1. Navigate to `/superadmin/plans`.
  2. Create a new subscription plan (e.g., "Annual Gold", ₹1,200.00, 365 days, 4 screens).
  3. Edit plan description and benefit tags.
  4. Toggle plan status to `INACTIVE` and verify it disappears from agent catalog.
  5. Toggle plan status back to `ACTIVE` and verify availability.
  6. Attempt to modify plan using a non-Super Admin session.
- **Expected Results:**
  - Plan CRUD operations succeed with full server-side validation.
  - Durations and amounts stored in integer days and paise.
  - Non-Super Admin receives HTTP 403 Forbidden (`CAPABILITY_REQUIRED`).
  - Administrative audit log record generated for each change.
- **Evidence to Capture:**
  - Plan UUID, audit trail log JSON, unauthorized 403 response body.

---

### Journey 04: Agent Appointment & Dual-Eye Approval Lifecycle
- **User Role:** Community Admin, Platform Super Admin, Agent Candidate.
- **Prerequisites:** Registered user account with verified mobile number.
- **Execution Steps:**
  1. Community Admin initiates agent nomination for candidate with commission plan assignment.
  2. Verify candidate agent status is initially `PENDING` (cannot perform agent actions).
  3. Community Admin attempts to approve their own nomination (anti-self-approval test).
  4. Platform Super Admin reviews nomination, verifies KYC photos, and submits approval decision.
  5. Verify candidate status transitions to `ACTIVE`.
  6. Super Admin tests lifecycle suspension via `POST /api/v1/admin/agents/{id}/lifecycle/suspend`.
  7. Attempt direct `PATCH /api/v1/agents/{id}` with `{"status": "ACTIVE"}` to verify SEC-07 remediation.
- **Expected Results:**
  - Self-approval blocked with HTTP 403 `SELF_APPROVAL_PROHIBITED`.
  - Nomination transitions cleanly upon authorized approval.
  - Direct status modification blocked with HTTP 403 `LIFECYCLE_WORKFLOW_REQUIRED` (SEC-07 verified).
- **Evidence to Capture:**
  - Appointment UUID, lifecycle transition logs, SEC-07 blocked 403 response.

---

### Journey 05: Member Subscription Purchase & Agent Agreed Pricing Overrides
- **User Role:** Agent, Subscriber.
- **Prerequisites:** Active Agent, Active Subscription Plan ("Annual Gold", Base: ₹1,200.00, Floor: ₹900.00).
- **Execution Steps:**
  1. Agent selects subscriber and assigns "Annual Gold" plan.
  2. Agent sets agreed override price of ₹1,000.00 (within allowed floor).
  3. Verify subscription is created in `PENDING_PAYMENT` status.
  4. Agent attempts to set override price below floor price (₹800.00).
  5. Verify system rejects price below floor with validation error.
- **Expected Results:**
  - Override price within floor accepted; amount stored as 100,000 paise.
  - Price below floor rejected with HTTP 422 / 400 validation error.
  - Subscription cannot be activated until payment is verified.
- **Evidence to Capture:**
  - Subscription UUID, agreed price in paise, floor validation error response.

---

### Journey 06: Server-Side Razorpay Payment Processing & Webhook Verification
- **User Role:** Subscriber, Payment Gateway.
- **Prerequisites:** `PENDING_PAYMENT` subscription.
- **Execution Steps:**
  1. Subscriber initiates checkout via `POST /api/v1/subscriptions/{id}/checkout`.
  2. Verify server creates Razorpay order with exact paise amount (₹1,000.00 $\to$ 100,000 paise).
  3. Subscriber completes checkout in Razorpay UI with test card.
  4. Razorpay sends signed `payment.captured` webhook to `/backend-api/api/v1/payments/webhook`.
  5. Webhook signature is validated using HMAC-SHA256 and `RAZORPAY_WEBHOOK_SECRET`.
  6. Resend duplicate webhook event to verify idempotency.
- **Expected Results:**
  - Server validates webhook signature and transitions payment to `CAPTURED`.
  - Member subscription transitions from `PENDING_PAYMENT` to `ACTIVE`.
  - Duplicate webhook returns HTTP 200 with idempotent acknowledgment without double-processing.
- **Evidence to Capture:**
  - Razorpay Order ID, Payment ID, webhook HMAC signature header, subscription status `ACTIVE`.

---

### Journey 07: Multi-Tier Referral Commission Attribution Engine
- **User Role:** System Automated Workflow / Financial Engine.
- **Prerequisites:** 3-tier member hierarchy established:
  - Agent A (Tier 1 direct seller)
  - Agent B (Tier 2 upline, invited Agent A)
  - Agent C (Tier 3 upline, invited Agent B)
- **Execution Steps:**
  1. Trigger verified payment of ₹1,000.00 for Agent A's customer.
  2. Inspect financial ledger entries generated by the attribution engine.
  3. Verify commission calculations:
     - Tier 1 (10%): ₹100.00 credited to Agent A's `EARNED_COMMISSION` account.
     - Tier 2 (3%): ₹30.00 credited to Agent B's `EARNED_COMMISSION` account.
     - Tier 3 (1%): ₹10.00 credited to Agent C's `EARNED_COMMISSION` account.
     - Total Commission (14%): ₹140.00 debited from `COMMUNITY_EXPENSE` account.
  4. Check account balances for all 3 agents and community expense account.
- **Expected Results:**
  - Single atomic `COMMISSION_ACCRUAL` journal created with 4 ledger entry lines.
  - Exact double-entry balance: $\sum \text{Debits} (140.00) == \sum \text{Credits} (100.00 + 30.00 + 10.00)$.
  - Account balances reflect exact credits without rounding errors.
- **Evidence to Capture:**
  - Journal UUID, 4 ledger entry UUIDs, account balance query outputs.

---

### Journey 08: Agent Payout Request & Dual-Eye Governance Review
- **User Role:** Agent, Community Admin.
- **Prerequisites:** Agent A has ₹500.00 available balance in `EARNED_COMMISSION`.
- **Execution Steps:**
  1. Agent A submits payout request for ₹300.00 via `POST /api/v1/payouts/request`.
  2. Verify ₹300.00 is locked: debited from `EARNED_COMMISSION` and credited to `PAYOUT_HOLD`.
  3. Agent A available balance drops to ₹200.00.
  4. Agent A attempts to submit another payout for ₹300.00 (exceeds available balance).
  5. Verify system rejects request with `INSUFFICIENT_FUNDS`.
  6. Agent A attempts to approve their own payout request.
  7. Community Admin reviews request and submits approval decision with mandatory review notes.
- **Expected Results:**
  - `PAYOUT_HOLD` double-entry journal created immediately upon request.
  - Self-approval blocked with HTTP 403 `SELF_APPROVAL_PROHIBITED`.
  - Overdraft attempt blocked with HTTP 400 `INSUFFICIENT_FUNDS`.
  - Community Admin approval transitions request status from `PENDING_REVIEW` to `APPROVED`.
- **Evidence to Capture:**
  - Payout Request UUID, hold journal UUID, review audit record.

---

### Journey 09: Platform Super Admin Disbursement & Reversal Journals
- **User Role:** Platform Super Admin.
- **Prerequisites:** Approved payout request in `APPROVED` status.
- **Execution Steps:**
  1. Platform Super Admin executes platform-level disbursement via bank transfer.
  2. Super Admin records disbursement with banking UTR:
     - `POST /api/v1/payouts/{id}/disburse` with `{"utr": "UTR1234567890", "method": "NEFT"}`.
  3. Verify disbursement double-entry journal:
     - ₹300.00 debited from `PAYOUT_HOLD` and credited to `PLATFORM_SETTLEMENT`.
  4. Payout status transitions to `DISBURSED` (terminal state).
  5. Attempt to disburse the same payout a second time (replay attack test).
  6. Test Rejection / Reversal path on a second test payout:
     - Community Admin rejects a ₹200.00 request.
     - Verify compensating `PAYOUT_REVERSAL` journal releases ₹200.00 back to `EARNED_COMMISSION`.
- **Expected Results:**
  - Disbursement succeeds with mandatory UTR validation.
  - Replay disbursement blocked with HTTP 409 Conflict.
  - Reversal journal perfectly restores agent's available balance to ₹500.00.
  - All transactions balance to zero.
- **Evidence to Capture:**
  - UTR number, disbursement journal UUID, reversal journal UUID, rejection audit notes.

---

### Journey 10: KYC Photo Registration & Cloudflare R2 Storage Verification
- **User Role:** Agent, Subscriber, Cloudflare R2.
- **Prerequisites:** KYC photo registration enabled for tenant.
- **Execution Steps:**
  1. Agent uploads subscriber profile photo (valid JPEG, 1.2 MB).
  2. Verify Pillow validation passes: dimensions $\le 4096 \times 4096$, format in `[JPEG, PNG, WEBP]`.
  3. Attempt upload of oversized file (> 5 MB) or decompression bomb image.
  4. Verify encrypted object is uploaded to private Cloudflare R2 bucket (`avanflix-community-ott-prod`).
  5. Attempt direct public URL access to R2 bucket object (verify private access block).
  6. Access image via backend streaming preview endpoint with valid HMAC token.
- **Expected Results:**
  - Valid image uploaded to private R2 key (`user-photos/<uuid>.jpg`).
  - Oversized/malformed images rejected with HTTP 400 `INVALID_IMAGE`.
  - Direct public S3 access denied (HTTP 403 Forbidden).
  - Backend streaming preview endpoint delivers image cleanly.
- **Evidence to Capture:**
  - R2 object key, S3 direct 403 response, streaming preview HTTP 200 headers.

---

### Journey 11: Multi-Tenant Boundary Isolation & Spoofing Defense
- **User Role:** Malicious Actor / Penetration Tester.
- **Prerequisites:** Two active tenants: Tenant A (`alpha.avanflix.com`) and Tenant B (`beta.avanflix.com`).
- **Execution Steps:**
  1. Authenticate as Community Admin of Tenant A.
  2. Attempt to query Tenant B's agent list: `GET /api/v1/admin/agents` with `Host: beta.avanflix.com` using Tenant A's session cookie.
  3. Attempt to manipulate Tenant B's payout request using Tenant A's credentials.
  4. Send request with spoofed `X-Tenant-ID` or invalid Host header (`malicious.com`).
  5. Verify cross-tenant data leakage in member hierarchy queries.
- **Expected Results:**
  - Mismatched host/cookie results in HTTP 401/403 or empty tenant dataset.
  - Cross-tenant mutations strictly blocked with HTTP 403/404.
  - Spoofed `Host` headers rejected by tenant resolver (`UNKNOWN_TENANT`).
  - Zero cross-tenant data visible in any response.
- **Evidence to Capture:**
  - HTTP 401/403 audit logs, tenant resolution failure records.

---

### Journey 12: End-to-End System Ledger Integrity Reconciliation
- **User Role:** Platform Auditor / System Administrator.
- **Prerequisites:** All prior 11 journeys executed on staging database.
- **Execution Steps:**
  1. Execute automated ledger reconciliation script:
     ```powershell
     python -c "from app.modules.commissions.attribution_service import verify_ledger_integrity; verify_ledger_integrity()"
     ```
  2. Verify zero-sum invariant across all journals:
     $$\sum_{\text{all entries}} \text{Debits} == \sum_{\text{all entries}} \text{Credits}$$
  3. Verify non-negative balance invariant:
     $$\text{Balance}(\text{Account}_i) \ge 0 \quad \forall i$$
  4. Verify no dangling or orphaned entries exist in database.
- **Expected Results:**
  - 100% mathematical zero-sum reconciliation confirmed across all tenants.
  - 0 account balance discrepancies detected.
  - Verification script exits with code 0.
- **Evidence to Capture:**
  - Total system debits, total system credits, delta = ₹0.00, execution log.

---

## 4. Production Release Gates & Go / No-Go Decision Criteria

To achieve authorization for commercial production launch, every release gate must achieve a verified `PASS` verdict. A single `FAIL` or `UNRESOLVED` gate mandates an immediate `NO-GO` decision.

| Gate ID | Release Gate Description | Threshold / Criteria | Status | Gatekeeper |
| :---: | :--- | :--- | :---: | :--- |
| **GATE-01** | **Backend Pytest Regression Suite** | 100% pass (392 passed, 0 failed, 0 errors) | `PASS` | Backend Lead |
| **GATE-02** | **Alembic Migration Integrity** | Single linear head `20261021_20 (head)`, zero branches | `PASS` | Database Admin |
| **GATE-03** | **Frontend Proxy Integration Tests** | 100% pass (21/21 passed) | `PASS` | Frontend Lead |
| **GATE-04** | **Frontend Production Compilation** | 0 TypeScript errors, 0 ESLint errors (`npm run build`) | `PASS` | Frontend Lead |
| **GATE-05** | **Legacy MongoDB Decommissioning** | 0 occurrences of `mongodb` or `@/lib/repo` in `src/` | `PENDING` | Frontend Lead |
| **GATE-06** | **Live Notification Microservice** | Real SMS OTP delivery verified on staging | `PENDING` | Platform Ops |
| **GATE-07** | **Cloudflare R2 Private Bucket** | Encrypted photo upload & streaming preview verified | `PENDING` | DevOps Lead |
| **GATE-08** | **Razorpay Live Merchant Account** | Live order creation, webhook verification & idempotency | `PENDING` | Finance Lead |
| **GATE-09** | **Automated PostgreSQL Backups** | Daily `pg_dump` cron + offsite R2 replication verified | `PENDING` | DevOps Lead |
| **GATE-10** | **Disaster Recovery Restoration** | Staging restore from automated backup snapshot verified | `PENDING` | Database Admin |
| **GATE-11** | **Production TLS Ingress Proxy** | Nginx HTTP/2 + TLS 1.3 + Wildcard DNS verified | `PENDING` | Network Security |
| **GATE-12** | **Staging Human UAT Execution** | All 12 UAT Journeys executed and signed off | `PENDING` | QA Lead & Owner |
| **GATE-13** | **Double-Entry Ledger Reconciliation** | System-wide $\sum \text{Debits} == \sum \text{Credits}$ verified | `PASS` | Financial Auditor |

---

## 5. Stakeholder Sign-Off Matrix

| Role | Name / Title | Decision | Signature / Approval Timestamp | Notes / Comments |
| :--- | :--- | :---: | :---: | :--- |
| **Platform Project Owner** | Project Owner | `PENDING` | _______________________ | Awaiting resolution of P0/P1 blockers. |
| **Backend & Architecture Lead** | Backend Lead | `PASS` | October 22, 2026 — Verified | Core software & ledger sealed. |
| **Frontend & UI Lead** | Frontend Lead | `PENDING` | _______________________ | Awaiting legacy Mongo removal. |
| **DevOps & Infrastructure Lead** | DevOps Lead | `PENDING` | _______________________ | Awaiting backup scripts & TLS proxy. |
| **Security & Compliance Auditor**| Security Auditor | `PASS` | October 22, 2026 — Verified | Phase 2, 3, 4 security controls verified. |
| **QA & Acceptance Lead** | QA Lead | `PENDING` | _______________________ | Awaiting live staging UAT execution. |

