# Community OTT Agent Platform — Phase 2 Security Acceptance Audit Report

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Date:** 2026-10-17  
**Audit Mode:** READ-ONLY VERIFICATION  
**Scope:** Phase 2 Implementation (`docs/reports/PHASE_2_IMPLEMENTATION_REPORT.md`), Capability-Based RBAC Migration, and Agent Appointment Approval Workflows  
**Auditor:** Independent Security Acceptance Agent  

---

## Executive Summary & Audit Verdict

### Audit Verdict: **`NOT_ACCEPTED`**

While Phase 2 successfully introduced core data models, Alembic migrations (`20261017_14`), unit test coverage (9/9 passing), and proxy integration suites (18/18 passing), **critical and high-severity security vulnerabilities were discovered during read-only source code inspection**.

Most critically, a **P0 Cross-Tenant Authorization Bypass** exists on platform appointment approval endpoints, allowing a Community Admin from one tenant to approve or reject agent appointments in any other tenant. Additionally, a **P1 Approval Bypass** exists in legacy agent creation routes, a **P1 Privilege Escalation** vulnerability permits assigning administrative roles through agent appointments, and authorization fallback logic bypasses database permissions in favor of hardcoded role checks.

Phase 3 development **MUST NOT** proceed until all identified P0 and P1 vulnerabilities are remediated and verified.

---

## Findings Matrix

| Finding ID | Severity | Category | Vulnerability / Issue Description | Affected Files | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SEC-01** | **P0 (Critical)** | Cross-Tenant Authorization Bypass | Platform appointment approval endpoints use tenant-level capability checks and caller tenant parameter override, allowing a tenant admin to approve/reject appointments for any community. | `backend/app/modules/agent_appointments/api.py`<br>`frontend/src/app/backend-api/[...path]/route.ts` | **OPEN** |
| **SEC-02** | **P1 (High)** | Workflow Bypass / Two-Eye Review Void | Legacy `POST /api/v1/agents` directly provisions active users, credentials, role assignments, and creates an auto-`APPROVED` appointment without review. | `backend/app/modules/agents/api.py` | **OPEN** |
| **SEC-03** | **P1 (High)** | Privilege Escalation | `submit_appointment` accepts arbitrary `role_id` without verifying `role.code == "agent"`, enabling appointment and activation of Community Admin credentials. | `backend/app/modules/agent_appointments/service.py` | **OPEN** |
| **SEC-04** | **P1 (High)** | Authorization Fail-Open / Hardcoded Fallback | `require_session_capability` falls back to hardcoded string role checks (`ADMIN` / `AGENT`) when database permissions are empty or unassigned, failing open rather than closed. | `backend/app/modules/authorization/dependencies.py` | **OPEN** |
| **SEC-05** | **P2 (Medium)** | Race Condition / Duplicate Decisions | `approve_appointment` and `reject_appointment` lack pessimistic row locking (`with_for_update()`), and `approval_decisions` lacks a unique constraint on `appointment_id`. | `backend/app/modules/agent_appointments/service.py`<br>`backend/app/modules/approval_workflows/models.py` | **OPEN** |
| **SEC-06** | **P2 (Medium)** | Proxy Cookie Ambient Forwarding | The Next.js API proxy forwards `community_session` to platform routes if `community_platform_session` is absent, bridging tenant sessions into platform routes. | `frontend/src/app/backend-api/[...path]/route.ts` | **OPEN** |
| **SEC-07** | **Informational** | Data Integrity & Retention | Migration `20261017_14` properly implements `RESTRICT` foreign keys and linear migration history, preserving audit integrity. | `backend/migrations/versions/20261017_14_agent_appointments_and_approvals.py` | **VERIFIED** |

---

## Detailed Audit Findings

### 1. SEC-01: [P0 Critical] Cross-Tenant Authorization Bypass via Platform Appointment Endpoints

#### Location
- [`backend/app/modules/agent_appointments/api.py#L215-L303`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agent_appointments/api.py#L215-L303)
- [`backend/app/modules/agent_appointments/service.py#L380-L405`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agent_appointments/service.py#L380-L405)
- [`frontend/src/app/backend-api/[...path]/route.ts#L510-L518`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/backend-api/%5B...path%5D/route.ts#L510-L518)

#### Mechanism
In `backend/app/modules/agent_appointments/api.py`:
```python
@router.post(
    "/platform/communities/{community_id}/appointments/{appointment_id}/approve",
    response_model=AppointmentSummary,
    summary="Super Admin: Approve an appointment in a community",
)
def platform_approve_agent_appointment(
    community_id: UUID,
    appointment_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_APPROVE))],
    session: Annotated[Session, Depends(get_db)],
    body: AppointmentDecisionRequest | None = None,
) -> AppointmentSummary:
    return appointment_service.approve_appointment(
        session=session,
        appointment_id=appointment_id,
        reviewer_user_id=caller.user_id,
        reviewer_membership_id=caller.membership_id,
        caller_tenant_id=community_id, # <--- Controlled by path parameter, not caller session!
        data=body,
    )
```

1. **Missing Platform Role Enforcement:** The endpoint relies on `require_session_capability(PermissionCode.AGENT_APPROVE)`. Under `dependencies.py`, any tenant administrator (`caller.role == "ADMIN"`) possesses `PermissionCode.AGENT_APPROVE` in the hardcoded `admin_capabilities` fallback set.
2. **Path Parameter Overrides Tenant Scope:** `caller_tenant_id` is set to `community_id` directly from the URL path.
3. **Flawed Boundary Check:** In `appointment_service.approve_appointment`:
   ```python
   if caller_tenant_id is not None and appointment.tenant_id != caller_tenant_id:
       raise HTTPException(403, detail={"error": "CROSS_TENANT_APPOINTMENT_FORBIDDEN", ...})
   ```
   Because `caller_tenant_id` was set to the URL path's `community_id`, `appointment.tenant_id == caller_tenant_id` evaluates to `True`, completely bypassing tenant boundary isolation.
4. **Proxy Cookie Forwarding:** In `frontend/src/app/backend-api/[...path]/route.ts#L513-L518`, if a user is logged in as a Community Admin (holding `community_session`) and has no platform session, the proxy forwards `community_session` to platform routes.
5. **Exploit Scenario:** Admin of Community A sends a request to `/api/v1/platform/communities/{Community_B_ID}/appointments/{Appointment_ID}/approve`. The request succeeds and activates an agent in Community B.

---

### 2. SEC-02: [P1 High] Appointment Approval Bypass via Legacy `POST /api/v1/agents`

#### Location
- [`backend/app/modules/agents/api.py#L515-L683`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agents/api.py#L515-L683)

#### Mechanism
`POST /api/v1/agents` (`_create_agent_impl`) was retained for backward compatibility. However, inspecting lines 521-683 reveals:
```python
# 1. User
user = User(id=user_id, status=body.status, ...) # body.status defaults to "active"
# 2. TenantMembership
membership = TenantMembership(..., status=body.status, ...)
# 4. UserRoleAssignment
assignment = UserRoleAssignment(..., status=body.status, ...)
# 5. AccountCredential
credential = AccountCredential(..., status=body.status, ...)
# 8b. Create AgentAppointment record
appointment_status = "APPROVED" if body.status == "active" else "PENDING"
appointment = AgentAppointment(
    ...,
    requested_by_user_id=created_by_user_id,
    role_id=agent_role.id,
    status=appointment_status, # <--- Immediately marked APPROVED
    ...
)
```
1. **Direct Provisioning:** When `body.status == "active"` (the schema default), all accounts, credentials, and role assignments are activated immediately upon creation.
2. **Fabricated Approval Record:** The `AgentAppointment` is persisted with `status="APPROVED"` without creating any corresponding `ApprovalDecision` record.
3. **Circumvention of Multi-Eye Governance:** Any admin or actor with `agent.create` can bypass the entire two-person review requirement defined in Architecture Section 10.4 by calling `POST /api/v1/agents` instead of the appointment endpoints.

---

### 3. SEC-03: [P1 High] Privilege Escalation via Arbitrary Request-Body Role ID

#### Location
- [`backend/app/modules/agent_appointments/service.py#L143-L157`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agent_appointments/service.py#L143-L157)
- [`backend/app/modules/agent_appointments/service.py#L478-L487`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agent_appointments/service.py#L478-L487)

#### Mechanism
In `AgentAppointmentService.submit_appointment`:
```python
# Role resolution
role: Role | None = None
if data.role_id is not None:
    role = session.execute(
        select(Role).where(
            Role.id == data.role_id,
            Role.tenant_id == tenant.id,
        )
    ).scalar_one_or_none()
```
1. **Unchecked Role Type:** The service only validates that `Role.tenant_id == tenant.id`. It does not restrict the role to `role.code == "agent"`.
2. **Escalation Path:** An applicant or requester can specify the UUID of the tenant's `admin` role in `data.role_id`.
3. **Privilege Grant Upon Approval:** In `approve_appointment`:
   ```python
   assignment = session.execute(
       select(UserRoleAssignment).where(
           UserRoleAssignment.membership_id == appointment.membership_id,
           UserRoleAssignment.role_id == appointment.role_id,
       )
   ).scalar_one_or_none()
   if assignment:
       assignment.status = "active"
   ```
   When the appointment is approved, the candidate receives full Community Admin privileges rather than Agent privileges.

---

### 4. SEC-04: [P1 High] Authorization Fallback Overrides Database Permissions (Fail-Open Role Check)

#### Location
- [`backend/app/modules/authorization/dependencies.py#L74-L115`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authorization/dependencies.py#L74-L115)

#### Mechanism
In `require_session_capability`:
```python
state = authz_repo.load_state(
    tenant_id=caller.tenant_id,
    user_id=caller.user_id,
    membership_id=caller.membership_id,
)
if state and required in state.permissions:
    return caller

if caller.role == "ADMIN":
    admin_capabilities = {
        PermissionCode.AGENT_CREATE,
        ...,
        PermissionCode.AGENT_APPROVE,
        ...
    }
    if required in admin_capabilities:
        return caller
```
1. **Fallback Bypass:** In current installations where granular `RolePermission` records are not seeded in PostgreSQL, `state.permissions` is empty. Instead of failing closed (`403 Forbidden`), the dependency catches the empty state and falls back to coarse hardcoded role matching (`caller.role == "ADMIN"`).
2. **Revocation Ineffectiveness:** If an administrator explicitly removes a permission from an admin role in the database, the check still passes because the fallback executes whenever the DB state does not contain the permission.
3. **Violation of Architecture RBAC Model:** Architecture Document Section 10.1 mandates capability checks backed by database role-permission mappings. The current implementation relies on string role comparisons with a pseudo-RBAC wrapper.

---

### 5. SEC-05: [P2 Medium] Missing Concurrency Locks & Duplicate Decision Vulnerability

#### Location
- [`backend/app/modules/agent_appointments/service.py#L387-L394`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agent_appointments/service.py#L387-L394)
- [`backend/app/modules/approval_workflows/models.py#L25-L33`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/approval_workflows/models.py#L25-L33)
- [`backend/migrations/versions/20261017_14_agent_appointments_and_approvals.py#L49-L74`](file:///d:/AVANFlix/OTT_Agent_App/backend/migrations/versions/20261017_14_agent_appointments_and_approvals.py#L49-L74)

#### Mechanism
1. **No Pessimistic Row Lock:** `AgentAppointmentService.approve_appointment` and `reject_appointment` query `AgentAppointment` using standard `select(...)` without `.with_for_update()`.
2. **Race Condition Under Read Committed Isolation:** Two concurrent requests (e.g., an approve and a reject arriving at the same millisecond from two distinct reviewers) can both observe `appointment.status == "PENDING"`.
3. **Missing Unique Constraint:** Neither `approval_workflows/models.py` nor migration `20261017_14` places a `UniqueConstraint("appointment_id")` or unique index on `approval_decisions`.
4. **Impact:** Both conflicting decisions will commit to `approval_decisions`, corrupting the immutable audit trail and leaving the final appointment status determined by nondeterministic transaction commit order.

---

### 6. SEC-06: [P2 Medium] API Proxy Ambient Cookie Forwarding to Platform Routes

#### Location
- [`frontend/src/app/backend-api/[...path]/route.ts#L510-L518`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/backend-api/%5B...path%5D/route.ts#L510-L518)

#### Mechanism
```typescript
if (route.session) {
  const token = request.cookies.get(platformSessionCookieName)?.value;
  if (token && /^[A-Za-z0-9_-]{1,128}$/.test(token)) {
    headers.set("Cookie", `${platformSessionCookieName}=${token}`);
  } else if (!routePath.startsWith("api/v1/platform/auth/oidc")) {
    const authToken = request.cookies.get(authCookieName)?.value;
    if (authToken && /^[A-Za-z0-9_-]{1,128}$/.test(authToken)) {
      headers.set("Cookie", `${authCookieName}=${authToken}`);
    }
  }
}
```
1. **Ambient Fallback:** When a client issues a request to any route configured with `session: true`, if `community_platform_session` is missing, the proxy forwards `community_session`.
2. **Boundary Confusion:** This behavior was intended for tenant routes, but `routePath.startsWith("api/v1/platform/communities")` is also matched. Consequently, tenant admin cookies are forwarded to platform endpoints, directly enabling SEC-01.

---

### 7. SEC-07: [Informational / Verified] Migration & Foreign Key Safety

#### Location
- [`backend/migrations/versions/20261017_14_agent_appointments_and_approvals.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/migrations/versions/20261017_14_agent_appointments_and_approvals.py)

#### Verification Evidence
1. **Linear Version Chain:** Head `20261017_14` correctly references `down_revision = "20261017_13"`.
2. **Reversibility:** `downgrade()` drops child table `approval_decisions` first, followed by `agent_appointments`, adhering to relational dependency order.
3. **Audit Retention:** All primary entity foreign keys (`tenants.id`, `users.id`, `tenant_memberships.id`, `agent_profiles.id`, `roles.id`, `agent_appointments.id`) specify `ondelete="RESTRICT"`. Hard deletion of referenced entities while audit history exists is prevented by the database engine.
4. **Polymorphic Actor Identifiers:** `requested_by_user_id` and `decided_by_user_id` are stored as unconstrained UUID columns, correctly supporting platform operator identities without requiring synthetic user records in tenant tables.

---

## Verification & Test Suite Execution

### 1. Backend Security Test Execution
Command:
```powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_agent_appointments_and_rbac.py backend/tests/test_security_migration.py -q
```
Result:
- **9 passed, 36 warnings in 16.42s**
- Warnings: Standard deprecation warnings regarding httpx cookie syntax and SQLite expression index reflection.
- **Coverage Gap Identified:** Existing tests only exercise authorized platform superadmin sessions and isolated tenant admin sessions. No test asserts rejection when a tenant admin invokes `/api/v1/platform/communities/{other}/appointments/{id}/approve` or provides an admin `role_id` to `submit_appointment`.

### 2. Frontend Proxy Integration Test Execution
Command:
```powershell
npm run test:proxy
```
Result:
- **18 passed, 0 failed in 28.18s**
- Verifies upstream boundary confinement, header filtering, and CSRF enforcement.

---

## Required Remediation Plan (Prerequisites for Phase 3)

Before Phase 3 can be approved and initiated, the following remediation tasks must be implemented and independently verified:

### Remediation Step 1: Secure Platform Appointment Endpoints (Fix for SEC-01 & SEC-06)
- In `backend/app/modules/agent_appointments/api.py`, change dependencies on all `/platform/communities/{community_id}/appointments*` endpoints from `require_session_capability(...)` to `require_super_admin` (or require both `caller.is_super_admin` and the relevant capability).
- In `frontend/src/app/backend-api/[...path]/route.ts`, ensure that routes matching `api/v1/platform/*` **never** forward `community_session`, and only forward `community_platform_session`.

### Remediation Step 2: Enforce Role Assignment Whitelist (Fix for SEC-03)
- In `AgentAppointmentService.submit_appointment` (`backend/app/modules/agent_appointments/service.py`), add an explicit check ensuring that if `data.role_id` is supplied, `role.code == "agent"`. Reject any attempt to link appointments to `admin` or other non-agent roles with `HTTP 400 Bad Request`.

### Remediation Step 3: Deprecate or Align Legacy `POST /api/v1/agents` (Fix for SEC-02)
- Update `_create_agent_impl` in `backend/app/modules/agents/api.py` so that it either:
  1. Creates agents in a `pending` state and submits a formal `AgentAppointment` awaiting peer approval, OR
  2. Is strictly restricted to emergency maintenance by `require_super_admin` with explicit audit logging, preventing tenant admins from bypassing peer review.

### Remediation Step 4: True Fail-Closed RBAC & Permission Seeding (Fix for SEC-04)
- Remove hardcoded capability dictionaries (`admin_capabilities`, `agent_capabilities`) from `backend/app/modules/authorization/dependencies.py`.
- Seed standard role-permission mappings into PostgreSQL upon tenant provisioning so that `load_state` reliably loads permissions from the database.
- Ensure that if `state.permissions` lacks the required capability, access is denied immediately.

### Remediation Step 5: Concurrency Control and DB Uniqueness (Fix for SEC-05)
- In `AgentAppointmentService.approve_appointment`, `reject_appointment`, and `cancel_appointment`, acquire a pessimistic write lock using `select(AgentAppointment).where(...).with_for_update()`.
- Add a unique index/constraint on `approval_decisions(appointment_id)` in `approval_workflows/models.py` and create an Alembic migration to enforce exactly one final decision per appointment.

---

## Conclusion & Next Steps

Phase 2 cannot be accepted in its current state due to high-risk authorization vulnerabilities. 

**Recommendation:**
1. Acknowledge this audit report.
2. Complete the 5 remediation steps above.
3. Re-run the security acceptance audit.
4. Obtain project-owner sign-off before commencing Phase 3.

