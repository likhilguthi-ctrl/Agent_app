# Community OTT Agent Platform — Phase 2 Implementation Report
## Agent Appointment Approval Workflows & Capability-Based RBAC Migration

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Date:** October 17, 2026  
**Status:** COMPLETE & VERIFIED  
**Mode:** Phase 2 implementation complete — STOPPED for project-owner review. Phase 3 NOT started.

---

## 1. Executive Summary

Phase 2 successfully delivered the complete end-to-end Agent Appointment Approval workflow and completed the migration of applicable backend endpoints from coarse role checks (`require_super_admin`, `require_admin_or_super`, `require_agent`) to centralized, capability-based, tenant-scoped authorization (`require_session_capability` / `require_permission`).

### Core Accomplishments
1. **Unapproved Agent Privilege Lockout:** Candidate agents created through appointment submission are held in `inactive`/`PENDING` state across identity, credentials, role assignments, and region assignments. Authentication via `/api/v1/auth/login` fails closed (`HTTP 401 Unauthorized`), ensuring unapproved agents cannot obtain active credentials or access agent endpoints.
2. **Atomic Approval & Activation Lifecycle:** Peer administrators or platform super administrators review pending appointments. Approving an appointment transitions `AgentAppointment.status` to `APPROVED`, records an auditable `ApprovalDecision`, activates the candidate's `User`, `TenantMembership`, `AccountCredential`, `UserRoleAssignment`, and `UserRegionAssignment` records in an atomic database transaction, and emits audit and outbox events.
3. **Strict Tenant & Self-Approval Isolation:**
   - **Self-Approval Prohibition:** If `requested_by_user_id == reviewer.user_id`, approval is strictly rejected with `HTTP 403 Forbidden` (`SELF_APPROVAL_PROHIBITED`).
   - **Cross-Tenant Isolation:** Community Admins are strictly forbidden from viewing, approving, or rejecting appointments outside their tenant (`HTTP 403 Forbidden` / `CROSS_TENANT_APPOINTMENT_FORBIDDEN`).
   - **Terminal State Idempotency:** Any attempt to approve, reject, or cancel an already decided appointment returns `HTTP 409 Conflict` (`INVALID_STATE_TRANSITION`).
4. **Additive Database Migration:** Added Alembic revision `20261017_14_agent_appointments_and_approvals.py` chaining linearly from `20261017_13`. Head resolution confirmed single head in test suite.
5. **Next.js Proxy Allowlist:** Updated `/backend-api/[...path]` in `frontend/src/app/backend-api/[...path]/route.ts` to forward `api/v1/agent-appointments`, `api/v1/agents/appointments`, and `/api/v1/platform/communities/{id}/appointments`.
6. **Full Verification Matrix:** All 5 new Phase 2 appointment & RBAC test scenarios pass (100%), full backend test suite passed (308 passed, 2 skipped, 0 failed), frontend proxy tests passed (18 passed, 0 failed), TypeScript typecheck passed (0 errors), lint passed (0 errors), and Next.js production build succeeded (27 pages generated).

---

## 2. Architecture & Data Model Changes

### 2.1 Persistence Models

#### `AgentAppointment` (`backend/app/modules/agent_appointments/models.py`)
- **Table:** `agent_appointments`
- **Fields:**
  - `id`: `UUID` (PK)
  - `tenant_id`: `UUID` (FK -> `tenants.id`, ondelete RESTRICT)
  - `user_id`: `UUID` (FK -> `users.id`, ondelete RESTRICT)
  - `membership_id`: `UUID` (FK -> `tenant_memberships.id`, ondelete RESTRICT)
  - `agent_profile_id`: `UUID` (FK -> `agent_profiles.id`, ondelete RESTRICT)
  - `requested_by_user_id`: `UUID` (Actor polymorphism: supports both tenant user IDs and platform operator IDs)
  - `role_id`: `UUID` (FK -> `roles.id`, ondelete RESTRICT)
  - `status`: `VARCHAR(16)` with `CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED', 'EXPIRED')", name="ck_agent_appointments_status")`
  - `proposed_commission_rate`: `NUMERIC(5, 2)` (Optional override proposal)
  - `notes`: `TEXT` (Optional requester justification)
  - `created_at`, `updated_at`: `TIMESTAMPTZ`

#### `ApprovalDecision` (`backend/app/modules/approval_workflows/models.py`)
- **Table:** `approval_decisions`
- **Fields:**
  - `id`: `UUID` (PK)
  - `tenant_id`: `UUID` (FK -> `tenants.id`, ondelete RESTRICT)
  - `appointment_id`: `UUID` (FK -> `agent_appointments.id`, ondelete CASCADE)
  - `decision`: `VARCHAR(16)` with `CheckConstraint("decision IN ('APPROVED', 'REJECTED')", name="ck_approval_decisions_decision")`
  - `decided_by_user_id`: `UUID` (Actor polymorphism: supports both tenant reviewer IDs and platform operator IDs)
  - `reason`: `TEXT` (Optional decision justification)
  - `decided_at`: `TIMESTAMPTZ`

### 2.2 Additive Migration
- **Revision:** `20261017_14_agent_appointments_and_approvals.py`
- **Down Revision:** `20261017_13_independent_activation_review`
- **Tables Created:** `agent_appointments`, `approval_decisions`
- **Indexes:** `ix_agent_appointments_tenant_status`, `ix_agent_appointments_user`, `ix_approval_decisions_appointment`

---

## 3. Capability-Based RBAC Migration

### 3.1 Authorization Dependency: `require_session_capability`
In `backend/app/modules/authorization/dependencies.py`, added `require_session_capability(permission: PermissionCode | str)`:
- Authenticates caller session via `get_current_resolved_session` (supporting both cookie tokens and `Authorization: Bearer <token>` headers).
- If caller has `platform_operator` account type with `SUPER_ADMIN` platform role, all capability checks pass.
- Evaluates explicit tenant permissions assigned to the caller's role via `load_state`.
- Recognizes canonical tenant permissions:
  - `ADMIN`: `agent.create`, `agent.approve`, `agent.reject`, `agent.read`, `agent.update`, `region.assign`, `region.read`, `member.create`, `member.read`, `approval.read`, `subscriber.read`, `subscriber.create`
  - `AGENT`: `agent.read`, `agent.policy.read`, `subscriber.read`, `subscriber.create`

### 3.2 Endpoint Migration Summary
The following endpoints in `backend/app/modules/agents/api.py` and `backend/app/modules/agent_appointments/api.py` were migrated to granular capabilities:

| Endpoint | Previous Guard | Migrated Guard | Required Permission |
|---|---|---|---|
| `GET /api/v1/agents` | `require_admin_or_super` | `require_session_capability` | `agent.read` |
| `GET /api/v1/agents/{id}` | `require_admin_or_super` | `require_session_capability` | `agent.read` |
| `POST /api/v1/agents` | `require_admin_or_super` | `require_session_capability` | `agent.create`, `member.create`, `region.assign` |
| `PATCH /api/v1/agents/{id}` | `require_admin_or_super` | `require_session_capability` | `agent.update` |
| `GET /api/v1/community/regions` | `require_admin_or_super` | `require_session_capability` | `region.read` |
| `GET /api/v1/agent-appointments` | New | `require_session_capability` | `approval.read` |
| `POST /api/v1/agent-appointments` | New | `require_session_capability` | `agent.create` |
| `GET /api/v1/agent-appointments/{id}` | New | `require_session_capability` | `approval.read` |
| `POST /api/v1/agent-appointments/{id}/approve` | New | `require_session_capability` | `agent.approve` |
| `POST /api/v1/agent-appointments/{id}/reject` | New | `require_session_capability` | `agent.reject` |
| `POST /api/v1/agent-appointments/{id}/cancel` | New | `require_session_capability` | `agent.create` |
| `POST /api/v1/platform/communities/{id}/appointments` | New | `require_super_admin` | Platform Super Admin |
| `GET /api/v1/platform/communities/{id}/appointments` | New | `require_super_admin` | Platform Super Admin |
| `POST /api/v1/platform/communities/{id}/appointments/{aid}/approve` | New | `require_super_admin` | Platform Super Admin |

---

## 4. Next.js API Proxy Configuration

File: `frontend/src/app/backend-api/[...path]/route.ts`

Added explicit allowlist paths for appointment workflows:
```typescript
"api/v1/agent-appointments",
"api/v1/agents/appointments",
// platform community appointments subroutes
if (path.length === 6 && path[0] === "api" && path[1] === "v1" && path[2] === "platform" && path[3] === "communities" && path[5] === "appointments") return true;
if (path.length === 8 && path[0] === "api" && path[1] === "v1" && path[2] === "platform" && path[3] === "communities" && path[5] === "appointments" && (path[7] === "approve" || path[7] === "reject")) return true;
```

---

## 5. Verification Matrix

| Verification Step | Command | Result | Details |
|---|---|---|---|
| **Phase 2 Tests** | `pytest backend/tests/test_agent_appointments_and_rbac.py -v` | **5 / 5 PASSED (100%)** | Submission, unapproved lockout, self-approval prevention, cross-tenant isolation, approval activation, idempotency, rejection, super admin workflow |
| **Security Migration Head** | `pytest backend/tests/test_security_migration.py` | **4 / 4 PASSED (100%)** | Single revision head `20261017_14`, linear down-revision to `20261017_13`, additive SQLite reflection |
| **Agent Storage Regression** | `pytest backend/tests/test_phase5_agent_user_storage.py` | **7 / 7 PASSED (100%)** | AES-256-GCM encryption at rest, R2 storage validation, commission inheritance |
| **Full Backend Suite** | `pytest backend/tests` | **308 PASSED, 2 SKIPPED (100%)** | 0 failed, 0 errors across entire backend test suite |
| **Frontend Proxy Integration** | `npm run test:proxy` (in `frontend/`) | **18 / 18 PASSED (100%)** | Fast fail-closed unknown routes, cookie isolation, session forwarding |
| **Frontend TypeScript** | `npm run typecheck` (in `frontend/`) | **CLEAN (0 ERRORS)** | Full TypeScript compiler check passed |
| **Frontend Linter** | `npm run lint` (in `frontend/`) | **CLEAN (0 ERRORS)** | ESLint verification passed |
| **Frontend Production Build** | `npm run build` (in `frontend/`) | **CLEAN (0 ERRORS)** | 27 static and dynamic routes compiled successfully |

---

## 6. Security Guarantees Confirmed

1. **Zero Privilege Leakage Before Approval:**
   - Database entities for newly submitted agents (`User`, `TenantMembership`, `AccountCredential`, `UserRoleAssignment`, `UserRegionAssignment`) are created in `inactive` status.
   - Authentication resolution (`SQLAlchemyAuthenticationRepository.resolve_session` and `authenticate_with_credentials`) strictly requires `status == "active"`. Candidate login attempts return `HTTP 401 Unauthorized`.
2. **Self-Approval Prohibition:**
   - Evaluated before decision execution: `if appointment.requested_by_user_id == reviewer_user_id: raise HTTPException(403)`.
   - Verified that both Community Admins and Platform Super Admins cannot approve appointments they initiated.
3. **Cross-Tenant Isolation:**
   - Evaluated before decision execution: `if caller_tenant_id is not None and appointment.tenant_id != caller_tenant_id: raise HTTPException(403)`.
   - Verified that Community Admins cannot view or decide appointments belonging to other communities.
4. **State Transition Idempotency:**
   - Evaluated before decision execution: `if appointment.status != "PENDING": raise HTTPException(409)`.
   - Re-approving or rejecting already resolved appointments is deterministically rejected.

---

## 7. Status & Next Actions

Phase 2 implementation and verification is complete.
Per prompt instructions:
- **Phase 3 has NOT been started.**
- **STOPPED and awaiting Project Owner review and approval.**

