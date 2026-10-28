# Phase 2: Target Management — Implementation & Verification Report

**Project:** Community OTT Platform (`D:\AVANFlix\OTT_Agent_App`)  
**Phase:** Phase 2 — Target Management  
**Date:** October 24, 2026  
**Status:** Complete & Fully Verified  

---

## 1. Executive Summary

Phase 2: Target Management introduces an end-to-end, hierarchical KPI target configuration, allocation, and tracking architecture across the Community OTT platform.

The system supports:
1. **Configurable KPI Definitions:** Database-backed `TargetType` models rather than hardcoded UI components or static queries.
2. **Authoritative PostgreSQL Business Metric Counting:** Automatic real-time achievement counting against authoritative PostgreSQL tables (`agent_appointments`, `subscriber_profiles`, `member_subscriptions`).
3. **Architectural Gap Remediation (`CONTENT_UPLOADED`):** Formal documentation that no content catalog table currently exists in PostgreSQL, backed by an auditable manual achievement adjustment workflow (`target_achievement_adjustments`).
4. **Hierarchical Allocation & Quota Enforcement:** Strict parent-child allocation constraints where Community Administrators cascade target quotas to Field Agents without exceeding their authorized parent allocation.
5. **Role-Based Access Control (RBAC) & Tenant Isolation:** Granular permissions (`target.read`, `target.allocate`, `target.manage`) strictly enforced at the FastAPI backend and isolated across communities/tenants.
6. **Frontend Integration:** Full dashboards for Super Admin, Community Admin, and Field Agent, plus profile-level KPI summaries in `EmployeeDetail`.
7. **Production Verification:** Additive Alembic migration (`20261024_26`), 100% automated test suite pass rate (backend pytests + proxy tests), and clean Next.js production build.

---

## 2. Architecture & Data Model

### A. Database Models (`backend/app/modules/targets/models.py`)

Three PostgreSQL tables were created via Alembic migration `20261024_26_target_management_schema.py`:

#### 1. `target_types`
Stores configurable KPI target definitions.
- `id`: UUID (Primary Key)
- `code`: String(64) Unique indexed (e.g. `AGENTS_RECRUITED`, `USERS_REGISTERED`, `CONTENT_UPLOADED`, `SUBSCRIPTIONS_GENERATED`)
- `name`: String(128) Human-readable name
- `description`: Text
- `unit`: String(32) Unit label (e.g. `agents`, `users`, `videos`, `subscriptions`)
- `calculation_type`: String(32) Check constraint: `AUTOMATIC` or `MANUAL`
- `applicable_role`: String(32) Check constraint: `ADMIN`, `AGENT`, or `BOTH`
- `is_active`: Boolean (Default: True)
- `created_at` / `updated_at`: DateTime (with timezone)

#### 2. `target_assignments`
Stores hierarchical target allocations assigned to Administrators or Field Agents.
- `id`: UUID (Primary Key)
- `tenant_id`: UUID Foreign Key to `tenants.id` (Nullable for platform-level cross-tenant targets)
- `parent_target_id`: UUID Foreign Key to `target_assignments.id` (Nullable; references the admin's parent target when cascaded to an agent)
- `target_type_id`: UUID Foreign Key to `target_types.id`
- `assigned_to_user_id`: UUID Foreign Key to `users.id`
- `assigned_to_membership_id`: UUID Foreign Key to `tenant_memberships.id` (Nullable)
- `assigned_by_user_id`: UUID (Tracks the operator or admin user who created the assignment)
- `assigned_role`: String(32) Check constraint: `ADMIN` or `AGENT`
- `target_quantity`: Integer Check constraint: `> 0`
- `start_date` / `deadline`: Date Check constraint: `start_date <= deadline`
- `period`: String(32) Check constraint: `WEEKLY`, `MONTHLY`, `QUARTERLY`, `ANNUAL`, `CUSTOM`
- `status`: String(32) Check constraint: `ACTIVE`, `COMPLETED`, `CANCELLED`, `OVERDUE`
- `notes`: Text (Nullable)
- `created_at` / `updated_at`: DateTime (with timezone)

#### 3. `target_achievement_adjustments`
Stores auditable manual adjustments to target achievements (used for `CONTENT_UPLOADED` and audited manual modifications).
- `id`: UUID (Primary Key)
- `target_id`: UUID Foreign Key to `target_assignments.id` (On delete cascade)
- `tenant_id`: UUID Foreign Key to `tenants.id` (Nullable)
- `adjusted_by_user_id`: UUID
- `adjustment_quantity`: Integer (Positive or negative delta)
- `reason`: Text (Mandatory auditable justification)
- `created_at`: DateTime (with timezone)

---

## 3. Authoritative PostgreSQL Business State Counting

Achievement counts are calculated dynamically in PostgreSQL by counting authoritative successful states within the target's date range `[start_date 00:00:00 UTC, deadline 23:59:59.999999 UTC]`:

| KPI Type | Role | Authoritative Business Source | Counting Logic |
| :--- | :--- | :--- | :--- |
| **`AGENTS_RECRUITED`** | `ADMIN` | `agent_appointments` | `status = 'APPROVED'` and `requested_by_user_id = admin_user_id` within date window. |
| **`USERS_REGISTERED`** | `AGENT` | `subscriber_profiles` | `registered_by_agent_membership_id = agent_membership.id` within date window. |
| **`USERS_REGISTERED`** | `ADMIN` | `subscriber_profiles` | Aggregate count across all agents reporting to this admin or belonging to the community within date window. |
| **`SUBSCRIPTIONS_GENERATED`** | `AGENT` | `member_subscriptions` | `created_by_user_id = agent_user_id` with `status = 'ACTIVE'` or `payment_status = 'SETTLED'` within date window. |
| **`SUBSCRIPTIONS_GENERATED`** | `ADMIN` | `member_subscriptions` | Active/settled subscriptions across the community tenant within date window. |
| **`CONTENT_UPLOADED`** | `BOTH` | `target_achievement_adjustments` | **Architectural Gap Documented:** PostgreSQL contains no content/video catalog table. Automatic count returns 0; achievements are recorded via auditable manual adjustments. |

### Achievement Calculations
- **Achieved Quantity:** `achieved = max(0, automatic_count + sum(adjustments))`
- **Remaining Quantity:** `remaining = max(target_quantity - achieved, 0)`
- **Progress Percentage:** `progress_pct = (achieved / target_quantity) * 100` (safe against zero division; un-capped so overachievement like 120% is clearly visible).
- **Dynamic Status:**
  - If target in DB is `CANCELLED` -> `CANCELLED`
  - If `achieved >= target_quantity` -> `COMPLETED`
  - If `today > deadline and achieved < target_quantity` -> `OVERDUE`
  - Else -> `ACTIVE`

---

## 4. Hierarchical Target Allocation Rules

### A. Super Admin Assigns Admin Target (`POST /api/v1/targets/admin`)
- Accessible only by `SUPER_ADMIN`.
- Validates that the administrator exists and the target type applies to `ADMIN` or `BOTH`.
- Validates date range (`start_date <= deadline`).
- Establishes a parent target assignment with `parent_target_id = None`.

### B. Admin Cascades to Agent (`POST /api/v1/targets/allocate`)
- Enforces parent-child quota limits:
  $$\sum \text{active\_children.target\_quantity} + \text{new\_quantity} \le \text{parent.target\_quantity}$$
- If allocation exceeds parent quota, rejects with HTTP 422 `TARGET_ALLOCATION_EXCEEDED` showing remaining available quota.
- Enforces date bounds: child `start_date >= parent.start_date` and child `deadline <= parent.deadline`.
- Enforces KPI applicability: rejects cascading admin-only KPIs (e.g. `AGENTS_RECRUITED`) to agents.
- Enforces tenant and regional isolation: Admin A cannot allocate from Admin B's targets or allocate to agents in another community.

---

## 5. Catalog Permissions & RBAC

Updated `backend/app/modules/authorization/catalog.py` and `seeding.py`:
- `PermissionCode.TARGET_READ = "target.read"`: Read target metrics and summaries. Granted to `ADMIN` and `AGENT`.
- `PermissionCode.TARGET_ALLOCATE = "target.allocate"`: Cascade and allocate targets to reporting team members. Granted to `ADMIN`.
- `PermissionCode.TARGET_MANAGE = "target.manage"`: Create targets and record adjustments. Granted to `SUPER_ADMIN`.

---

## 6. API Surface & Next.js Proxy Routes

### API Endpoints (`/api/v1/targets`)
- `GET /types`: List all active KPI definitions (`AGENTS_RECRUITED`, `USERS_REGISTERED`, `CONTENT_UPLOADED`, `SUBSCRIPTIONS_GENERATED`).
- `GET /administrators`: List eligible administrators across communities for Super Admin assignment dropdown.
- `POST /admin`: Super Admin creates administrator target.
- `GET /admin`: List administrator targets (Super Admin views all or filtered; Admin views own).
- `POST /allocate`: Admin allocates child target to Agent from parent quota.
- `GET /agent`: List agent targets (Super Admin views all; Admin views community team; Agent views own).
- `GET /summary`: Aggregated dashboard summary (total targets, total achieved, active, completed, overdue, overall progress %).
- `GET /{target_id}`: Detailed view with child allocations and adjustments.
- `POST /{target_id}/adjust`: Record auditable manual achievement adjustment.
- `GET /{target_id}/adjustments`: View audit history of manual adjustments.

### Next.js Proxy Integration (`frontend/src/app/backend-api/[...path]/route.ts`)
Configured allowlist and proxy forwarding for `/backend-api/api/v1/targets/*`:
- Forwards `community_session` and `community_platform_session`.
- Enforces HTTP methods, 32KB payload size limits, and `application/json` Content-Type on mutation endpoints.
- Passes query parameters cleanly to FastAPI backend.

---

## 7. Frontend Dashboards & Components

1. **`SuperAdminTargetManager.tsx` (`frontend/src/app/superadmin/dashboard/page.tsx`):**
   - KPI Target Overview StatCards (Total Targets, Total Quota, Total Achieved, Overall Progress %).
   - Administrator Target Performance Table with KPI filters, Status filters, and drill-down links to `/superadmin/employees/[id]`.
   - "Assign Admin Target" Modal: Select Administrator, KPI Type, Quota, Period, Start Date, Deadline, Notes.
   - "Manual Achievement Adjustment" Modal: Auditable adjustments for manual KPIs like `CONTENT_UPLOADED`.
2. **`AdminTargetManager.tsx` (`frontend/src/app/admin/dashboard/page.tsx`):**
   - Community KPI Targets card grid showing parent targets assigned by Super Admin.
   - Real-time display of unallocated quota (`remaining_to_allocate`).
   - "Allocate Target to Team" Modal with validation preventing over-allocation.
   - Cascaded Team Performance Table listing all field agent targets and progress.
3. **`AgentTargetCards.tsx` (`frontend/src/app/agent/dashboard/page.tsx`):**
   - Personal Target Cards grid with uncapped progress bars, verified counts, remaining to goal, and deadline countdown badges.
   - Clean, motivating, read-only interface.
4. **`EmployeeTargetSummary.tsx` (`frontend/src/components/EmployeeDetail.tsx`):**
   - Embedded into employee profile view, displaying active targets and verified progress for that individual.

---

## 8. Verification & Test Evidence

### A. Alembic Migration Upgrade
Executed against PostgreSQL with transactional DDL:
```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade 20261024_25 -> 20261024_26, Create target management schema and seed initial target types.
```

### B. Automated Backend Test Suite (`backend/tests/test_target_management.py`)
**8 out of 8 tests passed:**
1. `test_list_target_types`: Validates seeded KPI types (`AGENTS_RECRUITED`, `USERS_REGISTERED`, `CONTENT_UPLOADED`, `SUBSCRIPTIONS_GENERATED`).
2. `test_super_admin_create_admin_target`: Validates Super Admin assignment, role checks, and date order validation.
3. `test_hierarchical_target_allocation_and_quota_limits`: Validates cascading to agent, parent remaining tracking, over-allocation rejection (HTTP 422 `TARGET_ALLOCATION_EXCEEDED`), and date bounding.
4. `test_cross_tenant_isolation`: Confirms Admin in Community B cannot access or allocate from Admin in Community A's targets.
5. `test_automatic_achievement_calculation`: Tests automatic real-time calculation for `AGENTS_RECRUITED` (via `AgentAppointment`) and `USERS_REGISTERED` (via `SubscriberProfile`), uncapped overachievement (120%), and `COMPLETED` status.
6. `test_manual_adjustment_and_content_uploaded`: Tests `CONTENT_UPLOADED` manual tracking and auditable adjustment logging.
7. `test_overdue_status_transition`: Tests dynamic transition to `OVERDUE` when deadline has passed and quota is unfulfilled.
8. `test_dashboard_summary_endpoint`: Tests metric aggregation across all active targets.

### C. Regression Test Suites
- `test_super_admin_notifications_and_profiles.py`: **4 passed**
- `test_otp_login_multiple_results_and_webhook_logging.py`: **12 passed**
- `node frontend/test/proxy-integration.test.mjs`: **25 passed**

### D. Frontend Verification
- `npm --prefix frontend run typecheck`: **Exit code 0 (0 errors)**
- `npm --prefix frontend run build`: **Exit code 0 (All 29 routes built successfully)**

---

## 9. Conclusion

Phase 2: Target Management is completely implemented, verified, and integrated without regressions. All database schemas are backed by additive migrations, all business rules and quota limits are enforced on the backend, and all dashboards present clear, real-time KPI metrics to Super Admins, Community Admins, and Field Agents.

