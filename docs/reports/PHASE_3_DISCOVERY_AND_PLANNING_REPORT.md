# Community OTT Agent Platform — Phase 3 Discovery & Planning Report

**Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Date:** October 18, 2026  
**Mode:** READ-ONLY DISCOVERY AND PLANNING ONLY  
**Topic:** Member Hierarchy and Relationship-Based Access Control (Phase 3)  
**Status:** **DISCOVERY COMPLETE — AWAITING PROJECT OWNER REVIEW**  
**Implementation Authorization:** **NOT GRANTED**  

---

## 1. Executive Summary

- **[Verified]** Phase 2 Security Acceptance Audit concluded with the verdict `ACCEPTED` after all six security findings (`SEC-01` through `SEC-06`) were remediated and verified under live multi-threaded PostgreSQL concurrency and automated regression suites.
- **[Verified]** The Project Owner has authorized **Phase 3 Discovery and Planning only**. Application code, database schemas, migrations, configuration, environment settings, and frontend proxies remain completely unmodified during this discovery task.
- **[Verified]** Current repository capabilities support a single-level subscriber registration link (`SubscriberProfile.registered_by_agent_membership_id` in [`backend/app/modules/agents/models.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agents/models.py#L90-L98)), but lack multi-tier member hierarchies, operational reporting structures (`REPORTS_TO`), team management relations (`MANAGES`), recursive descendant queries, and relationship-based scoped authorization.
- **[Verified]** The permission catalog in [`backend/app/modules/authorization/catalog.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authorization/catalog.py#L41-L44) already defines Phase 3 capability codes (`member.relationship.manage`, `member.stats.read`, `member.descendants.read`, `member.descendants.create`), but they are currently unseeded in default tenant role definitions in [`backend/app/modules/authorization/seeding.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authorization/seeding.py#L42-L73).
- **[Proposed]** Phase 3 will introduce a dedicated, tenant-partitioned `member_relationships` table linking authoritative `TenantMembership` entities via composite foreign keys `(tenant_id, parent_member_id)` and `(tenant_id, child_member_id)` referencing `tenant_memberships(tenant_id, id)`. This design guarantees cross-tenant link impossibility at the PostgreSQL catalog level.
- **[Proposed]** Relationship-based authorization will enforce a six-dimensional evaluation tuple: `(Actor + Permission + Tenant Boundary + Region Scope + Relationship Scope + Requested Action)`. Operational descendants and aggregate statistics will be exposed via bounded, cycle-protected recursive CTE queries without exposing sensitive PII (such as Aadhaar or PAN).
- **[Verified]** This report documents all verified findings, architectural designs, API specifications, test matrices, and five unresolved business decisions requiring Project Owner input before Phase 3 implementation can begin.

---

## 2. Authorization Scope and Explicit Non-Modification Statement

- **[Verified]** **Authorization Scope:** Strictly limited to read-only discovery, code inspection, schema analysis, and architecture planning for Phase 3 (Member Hierarchy & Relationship-Based Access Control).
- **[Verified]** **Non-Modification Compliance Statement:**
  1. No application source code files (`backend/app/*`, `frontend/src/*`) have been modified, created, or deleted.
  2. No environment configuration files (`.env`, `.env.example`, `next.config.js`, etc.) have been altered.
  3. No database migrations have been generated, edited, or applied to PostgreSQL.
  4. No database tables, schemas, constraints, indexes, or records have been created, modified, or dropped.
  5. No packages, libraries, or dependencies have been installed, updated, or uninstalled.
  6. No frontend proxy routes, allowlists, or session-handling behaviors have been altered.
  7. No Phase 3 implementation has commenced.
- **[Verified]** Only one artifact has been generated in the project workspace: this discovery report at `docs/reports/PHASE_3_DISCOVERY_AND_PLANNING_REPORT.md`.

---

## 3. Repository Baseline and Git Working Tree Status

- **[Verified]** **Working Tree Location:** `d:\AVANFlix\OTT_Agent_App`.
- **[Verified]** **Git Repository Status:** Running `git status` and `git rev-parse --show-toplevel` confirms that `d:\AVANFlix\OTT_Agent_App` is a standalone project directory without a local `.git` directory (`fatal: not a git repository (or any of the parent directories): .git`). File integrity and revision states are maintained via directory inspection, checksums, and audit reports.
- **[Verified]** **Canonical Directories:**
  - `backend/`: Canonical FastAPI application (`app/`), database migrations (`migrations/`), and automated test suites (`tests/`).
  - `frontend/`: Canonical Next.js 14 application (`src/app/`), components (`src/components/`), and proxy route (`src/app/backend-api/[...path]/route.ts`).
  - `back-end/` and `front-end/`: Legacy superseded reference directories confirmed inert in Phase 1 audit.
- **[Verified]** **Baseline Test Results:**
  - Backend pytest suite: 314 passed, 2 skipped, 0 failed in 96.02s.
  - Proxy integration test suite: 19 passed, 0 failed in 1.48s.
  - Frontend typecheck (`tsc --noEmit`): Clean (0 errors).
  - Frontend lint (`next lint`): Clean (0 warnings, 0 errors).
  - Frontend production build (`next build`): Completed successfully.

---

## 4. Existing Member and Subscriber Architecture

### 4.1 Core Identity and Membership Models
- **[Verified]** **`User` Model** ([`backend/app/modules/identity/models.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/identity/models.py#L24-L38)):
  - Primary Key: `id: UUID` (PostgreSQL `uuid_generate_v4()`).
  - Fields: `email: String(255)` (unique index), `status: String(32)` (`UserStatus.ACTIVE`), `created_at`, `updated_at`.
  - Responsibility: Global authentication and account credential root. A `User` does not hold community-specific state directly.
- **[Verified]** **`TenantMembership` Model** ([`backend/app/modules/identity/models.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/identity/models.py#L40-L72)):
  - Primary Key: `id: UUID`.
  - Fields: `tenant_id: UUID` (FK to `tenants.id`), `user_id: UUID` (FK to `users.id`), `mobile_e164: String(32)`, `status: String(32)` (`TenantMembershipStatus.ACTIVE`), `region_id: UUID` (FK to `community_regions.id`, nullable), `created_at`, `updated_at`.
  - Key Constraints:
    - Unique `("tenant_id", "mobile_e164")`: Ensures phone numbers are unique per tenant community.
    - Unique `("user_id")`: Enforces that each user identity maps to exactly one tenant membership in the community OTT architecture.
    - Composite Unique `("tenant_id", "id")` (`uq_tenant_memberships_tenant_id_id`): Enables composite foreign keys referencing tenant-scoped memberships.
  - Responsibility: **The authoritative entity representing a member in a community.** All roles, permissions, regions, agent profiles, and subscriber profiles link to `TenantMembership`.
- **[Verified]** **`AgentProfile` Model** ([`backend/app/modules/agents/models.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agents/models.py#L42-L78)):
  - Primary Key: `membership_id: UUID` (FK to `tenant_memberships.id` on delete CASCADE).
  - Fields: `tenant_id: UUID`, `ref_id: String(64)` (unique per tenant), `commission_rate_override: Numeric(5,4)`, AES-256-GCM encrypted identity fields (`aadhaar_ciphertext`, `aadhaar_nonce`, `aadhaar_tag`, `aadhaar_last4`, `pan_ciphertext`, etc.).
  - Responsibility: Stores agent credentials, referral codes, commission overrides, and encrypted identity verification documents.
- **[Verified]** **`SubscriberProfile` Model** ([`backend/app/modules/agents/models.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agents/models.py#L80-L101)):
  - Primary Key: `membership_id: UUID` (FK to `tenant_memberships.id` on delete CASCADE).
  - Fields: `tenant_id: UUID`, `registered_by_agent_membership_id: UUID` (FK to `tenant_memberships.id`, nullable), `created_at`, `updated_at`.
  - Responsibility: Marks a membership as an OTT subscriber and records which agent originally registered them.

### 4.2 Authoritative Entity Determination
- **[Verified]** A `User` is a global identity, while a community member exists only within the context of a `TenantMembership`.
- **[Verified]** Role assignments (`user_roles.membership_id`), regional scoping (`tenant_memberships.region_id`), appointment approvals (`agent_appointments.membership_id`), and agent/subscriber profiles (`membership_id`) are all bound to `tenant_memberships.id`.
- **[Inferred]** Therefore, any relationship table representing organizational hierarchy, reporting, or sponsorship within a community must reference `TenantMembership.id`, NOT `User.id`.
- **[Verified]** Crucially, the composite unique constraint `uq_tenant_memberships_tenant_id_id` on `tenant_memberships(tenant_id, id)` allows PostgreSQL composite foreign keys `(tenant_id, parent_member_id)` and `(tenant_id, child_member_id)` referencing `tenant_memberships(tenant_id, id)`. This guarantees at the database catalog level that cross-tenant relationships cannot be created.

---

## 5. Current Database Schema and Alembic Head

- **[Verified]** **Current Alembic Revision:** `20261017_15` (`approval_decisions_unique_appointment`).
- **[Verified]** **Migration History Chain:**
  1. `20261013_01`: Initial identity, tenants, users, memberships, roles, permissions.
  2. `20261014_02`: Core audit outbox tables.
  3. `20261015_03`: Community regions and geographic boundaries.
  4. `20261015_04`: Agent profiles, subscriber profiles, and encrypted PII fields.
  5. `20261015_05`: Registration photos and stored objects tracking.
  6. `20261015_06`: Support tickets and activity logging.
  7. `20261016_07` through `20261016_13`: OTP storage, rate limiting, and session hardening.
  8. `20261017_14`: Agent appointments and approval workflow schema (`agent_appointments`, `appointment_approval_decisions`).
  9. `20261017_15`: Addition of unique constraint `uq_approval_decisions_appointment_id` on `appointment_approval_decisions(appointment_id)` to prevent concurrent approval race conditions (SEC-05 fix).
- **[Verified]** **Current Alembic Head Status:** The database schema is fully up to date with revision `20261017_15`. There are no pending, orphaned, or unapplied migrations in the repository.

---

## 6. Existing Relationship and Hierarchy Capabilities

- **[Verified]** **Direct Subscriber Registration:**
  - In [`backend/app/modules/agents/api.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agents/api.py#L324-L372), `POST /api/v1/agent/users` creates a new subscriber under the authenticated agent:
    ```python
    subscriber_profile = SubscriberProfile(
        membership_id=membership.id,
        tenant_id=tenant.tenant_id,
        registered_by_agent_membership_id=agent_membership.id,
    )
    ```
- **[Verified]** **Direct Subscriber Listing:**
  - In [`backend/app/modules/agents/api.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agents/api.py#L375-L415), `GET /api/v1/agent/users` queries subscribers where `SubscriberProfile.registered_by_agent_membership_id == agent_membership.id`.
- **[Verified]** **Verified Gaps in Hierarchy:**
  1. Flat 1-Level Link: Only 1 level of agent-to-subscriber relationship is recorded. Sub-agents, hierarchical agents, multi-tier distributors, and team reporting trees cannot be represented.
  2. Lack of Topology Types: No distinction exists between operational hierarchy (`REPORTS_TO`), referral tracking (`REFERRED_BY`), and managerial oversight (`MANAGES`).
  3. No Relationship Lifecycle: No support for relationship status (`ACTIVE`, `REVOKED`), transfer/replacement of managers, revocation timestamps, or revocation actor audit.
  4. No Recursive Queries: No recursive CTE queries exist to query multi-level descendants, lineage trees, or aggregate multi-tier team counts.
  5. No Dedicated Relationship Table: The architecture document Section 9 explicitly requires a dedicated `member_relationships` table.

---

## 7. Existing Authorization, Tenant, and Region Enforcement

- **[Verified]** **Session Authentication & Tenant Resolution:**
  - [`backend/app/modules/authentication/session_service.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authentication/session_service.py): Extracts `community_session` cookie, verifies against PostgreSQL `active_sessions`, resolves `user_id`, `membership_id`, `tenant_id`, and roles.
  - Tenant boundary validation ensures the authenticated user's session tenant matches the requested route tenant context.
- **[Verified]** **Capability-Based RBAC (`require_session_capability`):**
  - In [`backend/app/modules/authorization/dependencies.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authorization/dependencies.py#L78-L125), authorization is strictly database-backed.
  - If a user's role lacks the required `PermissionCode`, the dependency immediately raises `HTTP 403 Forbidden`. All static role fallbacks were eradicated in Phase 2 remediation (SEC-04).
- **[Verified]** **Existing Phase 3 Permission Codes in Catalog:**
  - In [`backend/app/modules/authorization/catalog.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authorization/catalog.py#L41-L44):
    ```python
    MEMBER_RELATIONSHIP_MANAGE = "member.relationship.manage"
    MEMBER_STATS_READ = "member.stats.read"
    MEMBER_DESCENDANTS_READ = "member.descendants.read"
    MEMBER_DESCENDANTS_CREATE = "member.descendants.create"
    ```
- **[Verified]** **Seeding Gap in Role Permissions:**
  - In [`backend/app/modules/authorization/seeding.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authorization/seeding.py#L42-L73), `ADMIN_PERMISSIONS` and `AGENT_PERMISSIONS` do NOT currently include the 4 member relationship permission codes. When new tenants are seeded or existing roles are loaded, these permissions are unassigned.
- **[Verified]** **Region Enforcement:**
  - Community regions are modeled in `community_regions` and assigned via `tenant_memberships.region_id` or `agent_appointments.region_id`.
  - Regional queries in [`backend/app/modules/tenants/api.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/tenants/api.py) filter by `region_id`. If an agent or member is assigned to a specific region, relationship queries must incorporate regional boundary constraints.

---

## 8. Existing Frontend and Proxy Integration

- **[Verified]** **Next.js API Proxy (`route.ts`):**
  - Located at [`frontend/src/app/backend-api/[...path]/route.ts`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/backend-api/%5B...path%5D/route.ts#L45-L65).
  - Uses an explicit `ALLOWLISTED_PREFIXES` array.
  - Current agent allowlist contains:
    ```typescript
    "api/v1/agent/users",
    "api/v1/agent/appointments",
    ```
  - It does NOT yet include `/api/v1/members/relationships` or `/api/v1/agent/descendants`.
- **[Verified]** **Proxy Cookie Isolation:**
  - Following Phase 2 SEC-06 remediation, proxy requests to platform routes (`/api/v1/platform/*`) strictly forward `community_platform_session`, while tenant routes forward `community_session`.
- **[Verified]** **Frontend Agent Pages:**
  - [`frontend/src/app/agent/dashboard/page.tsx`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/agent/dashboard/page.tsx): Fetches `GET /backend-api/api/v1/agent/users`. Displays flat subscriber count and recent subscriber registrations.
  - [`frontend/src/app/agent/register/page.tsx`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/agent/register/page.tsx): Form posting to `/backend-api/api/v1/agent/users` to register a subscriber.
  - [`frontend/src/app/agent/layout.tsx`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/agent/layout.tsx): Sidebar navigation has a link to `/agent/customers`, but the directory `frontend/src/app/agent/customers` does NOT exist (leads to 404).

---

## 9. Verified Gaps (with Source Evidence)

| Gap ID | Category | Description | Source Evidence | Impact |
| :--- | :--- | :--- | :--- | :--- |
| **GAP-01** | Data Model | Missing `member_relationships` table in PostgreSQL. | Inspection of `backend/app/modules/` and `migrations/versions/`. | Cannot store multi-tier hierarchy, reporting lines, or managerial links. |
| **GAP-02** | Database Integrity | No database-level cycle or self-link prevention. | `SubscriberProfile` only has nullable FK `registered_by_agent_membership_id`. | Risk of corrupted circular hierarchies and self-referencing loops. |
| **GAP-03** | Authorization Seeding | Relationship permissions unassigned in `seeding.py`. | [`seeding.py#L42-L73`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/authorization/seeding.py#L42-L73) lacks Phase 3 codes. | `require_session_capability` will fail closed for all tenant roles. |
| **GAP-04** | API Endpoints | Missing hierarchy, relationship management, and descendant roster routes. | [`backend/app/api/v1/router.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/api/v1/router.py) has no hierarchy routers. | Agents and Admins cannot query multi-tier trees or manage relationships. |
| **GAP-05** | Query Architecture | Missing safe recursive CTE query service with bounded depth. | `backend/app/modules/agents/` only has flat `== agent_membership.id` query. | No query path for multi-level descendant rosters or depth aggregations. |
| **GAP-06** | Frontend Proxy | Proxy allowlist lacks Phase 3 relationship route prefixes. | [`frontend/src/app/backend-api/[...path]/route.ts#L45-L65`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/backend-api/%5B...path%5D/route.ts#L45-L65). | Frontend requests to relationship routes will be blocked with HTTP 404 by the proxy. |
| **GAP-07** | Frontend Navigation | Missing `/agent/customers` hierarchy view page. | Navigation link exists in `agent/layout.tsx`, but no page folder exists. | Clicking "Customers" in agent navigation results in 404 Not Found. |

---

## 10. Proposed Data Model and Integrity Constraints

### 10.1 PostgreSQL Candidate Schema: `member_relationships`
- **[Proposed]** A dedicated SQLAlchemy model `MemberRelationship` in `backend/app/modules/identity/models.py` (or a dedicated `relationships` submodule):

```python
class RelationshipType(str, enum.Enum):
    REPORTS_TO = "REPORTS_TO"      # Operational reporting line (strict tree)
    REFERRED_BY = "REFERRED_BY"    # Referral attribution / commission lineage
    MANAGES = "MANAGES"            # Team / territory managerial oversight

class RelationshipStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"

class MemberRelationship(Base):
    __tablename__ = "member_relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    parent_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    child_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    relationship_type: Mapped[RelationshipType] = mapped_column(
        String(32), nullable=False, index=True
    )
    status: Mapped[RelationshipStatus] = mapped_column(
        String(32), nullable=False, default=RelationshipStatus.ACTIVE, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    end_reason: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
```

### 10.2 Database Integrity Constraints
1. **[Proposed]** **Composite Foreign Keys to Prevent Cross-Tenant Links:**
   - Leveraging `uq_tenant_memberships_tenant_id_id` on `tenant_memberships(tenant_id, id)`:
     ```sql
     CONSTRAINT fk_member_rel_parent FOREIGN KEY (tenant_id, parent_member_id)
         REFERENCES tenant_memberships (tenant_id, id) ON DELETE RESTRICT;
     CONSTRAINT fk_member_rel_child FOREIGN KEY (tenant_id, child_member_id)
         REFERENCES tenant_memberships (tenant_id, id) ON DELETE RESTRICT;
     ```
   - *Security Assurance:* Cross-tenant links are physically impossible; PostgreSQL rejects any insert where parent or child belongs to another `tenant_id`.
2. **[Proposed]** **Self-Link Prevention Check Constraint:**
   ```sql
   CONSTRAINT ck_member_rel_no_self_link CHECK (parent_member_id != child_member_id);
   ```
3. **[Proposed]** **Duplicate Prevention (Partial Unique Indexes):**
   - Unique active link between same parent and child for a type:
     ```sql
     CREATE UNIQUE INDEX uq_member_rel_active_link 
         ON member_relationships (tenant_id, parent_member_id, child_member_id, relationship_type)
         WHERE status = 'ACTIVE';
     ```
   - Single-parent constraint for strict trees (`REPORTS_TO` and `REFERRED_BY`):
     ```sql
     CREATE UNIQUE INDEX uq_member_rel_single_active_parent
         ON member_relationships (tenant_id, child_member_id, relationship_type)
         WHERE status = 'ACTIVE' AND relationship_type IN ('REPORTS_TO', 'REFERRED_BY');
     ```

### 10.3 Resolution of the 10 Core Design Questions
1. **Multiple parents?** Type-dependent: `REPORTS_TO` (single parent tree), `REFERRED_BY` (single referrer), `MANAGES` (permits multiple manager assignments).
2. **Tree vs DAG?** `REPORTS_TO` forms a strict directed tree. General hierarchy across all relationship types forms a DAG.
3. **Required types?** `REPORTS_TO` (operational), `REFERRED_BY` (referrals/commissions), `MANAGES` (team oversight).
4. **Authorization vs Informational?** `REPORTS_TO` and `MANAGES` establish operational descendant roster access. `REFERRED_BY` is attribution-only unless explicitly configured for commission statements.
5. **Revocation/Replacement?** Relationships are soft-revoked (`status='REVOKED'`, setting `ended_at`, `ended_by_user_id`, `end_reason`). A replacement inserts a new active record.
6. **Audit retention?** Full history retained; hard deletes blocked (`ON DELETE RESTRICT`).
7. **Duplicate active links?** Prohibited by unique partial indexes where `status = 'ACTIVE'`.
8. **Cycles and Self-links?** Self-links blocked by check constraint. Cycles prevented by service-level ancestor traversal check before insert and CTE cycle guards.
9. **Member Deactivation?** Traversal queries enforce `tenant_memberships.status == 'ACTIVE'`, immediately severing access to/from deactivated members.
10. **Tenant Consistency?** Enforced at the PostgreSQL catalog level via composite foreign keys `(tenant_id, member_id)`.

---

## 11. Proposed API Contracts and Permissions

### 11.1 Tenant Admin Endpoints (`/api/v1/members/...`)
- **[Proposed]** `POST /api/v1/members/relationships`
  - Actor: Tenant Admin.
  - Required Permission: `PermissionCode.MEMBER_RELATIONSHIP_MANAGE`.
  - Request: `{ parent_member_id: UUID, child_member_id: UUID, relationship_type: "REPORTS_TO" | "REFERRED_BY" | "MANAGES" }`.
  - Behavior: Validates same tenant, checks self-link, validates acyclicity via CTE query, checks single-parent rule, creates relationship in `ACTIVE` status, writes audit log.
  - Response: HTTP 201 Created with relationship details.
- **[Proposed]** `GET /api/v1/members/{member_id}/relationships`
  - Actor: Tenant Admin or authorized Member.
  - Required Permission: `PermissionCode.MEMBER_DESCENDANTS_READ` or `PermissionCode.MEMBER_RELATIONSHIP_MANAGE`.
  - Behavior: Returns active and historical parent/child relationships for the member within the tenant.
- **[Proposed]** `DELETE /api/v1/members/relationships/{relationship_id}`
  - Actor: Tenant Admin.
  - Required Permission: `PermissionCode.MEMBER_RELATIONSHIP_MANAGE`.
  - Request: `{ end_reason: string }`.
  - Behavior: Soft-revokes relationship (`status='REVOKED'`, `ended_at=now()`, `ended_by_user_id=actor.user_id`), writes audit log.
  - Response: HTTP 200 OK with revoked relationship details.

### 11.2 Agent Hierarchy Endpoints (`/api/v1/agent/...`)
- **[Proposed]** `GET /api/v1/agent/descendants`
  - Actor: Authenticated Agent.
  - Required Permission: `PermissionCode.MEMBER_DESCENDANTS_READ`.
  - Query Parameters: `max_depth: int = 2` (min 1, max 5), `relationship_type: str = "REPORTS_TO"`, `limit: int = 50`, `offset: int = 0`.
  - Security Anchor: The root of the hierarchy is ALWAYS anchored to `session.membership_id`. The client CANNOT supply an arbitrary root member ID (IDOR prevention).
  - Response: Paginated list of descendants with `member_id`, `user_id`, `depth`, `parent_member_id`, `relationship_type`, `created_at`, `status`, and masked mobile (`+91******1234`).
- **[Proposed]** `GET /api/v1/agent/descendants/stats`
  - Actor: Authenticated Agent.
  - Required Permission: `PermissionCode.MEMBER_STATS_READ`.
  - Security Anchor: Anchored to `session.membership_id`.
  - Response: `{ total_descendants: int, direct_children_count: int, counts_by_depth: { "1": int, "2": int }, counts_by_status: { "active": int, "inactive": int } }`.
- **[Proposed]** `POST /api/v1/agent/descendants`
  - Actor: Authenticated Agent.
  - Required Permission: `PermissionCode.MEMBER_DESCENDANTS_CREATE`.
  - Request: Registration payload for subscriber or sub-agent.
  - Behavior: Creates member and automatically binds relationship to calling agent (`parent_member_id = session.membership_id`).

---

## 12. Relationship-Based Authorization Design

- **[Proposed]** **Evaluation Model:**
  Access is granted if and only if:
  $$\text{Authorize} = \text{Actor} \land \text{Permission} \land \text{TenantMatch} \land \text{RegionMatch} \land \text{RelationshipScope} \land \text{AllowedAction}$$

- **[Proposed]** **Strict Boundaries & Principle of Least Privilege:**
  1. **Relationship Membership Alone NEVER Grants Broad Privileges:**
     - Being an ancestor or manager of a member does NOT grant permission to change their roles, modify their permissions, approve appointments, or view their sensitive encrypted identity documents (Aadhaar/PAN).
  2. **Role & Permission Changes:** Strictly reserved for Tenant Admins holding `role.assign` / `role.manage`.
  3. **Appointment Approvals:** Strictly reserved for Platform Super Admins holding `agent.appointment.approve`.
  4. **PII Isolation:** Aadhaar/PAN plaintext or ciphertext are never included in descendant roster schemas.
  5. **Fail-Closed Traversal:** If the target member is outside the actor's relationship subtree, or belongs to another tenant or region, the service returns `HTTP 404 Not Found` (or `HTTP 403 Forbidden`) without leaking member existence.

---

## 13. Descendant Roster and Statistics Design

### 13.1 Safe Recursive CTE Query
- **[Proposed]** PostgreSQL recursive CTE with bounded depth and cycle detection:

```sql
WITH RECURSIVE descendant_tree AS (
    -- Anchor member: direct children of the root member
    SELECT 
        mr.child_member_id,
        mr.parent_member_id,
        mr.relationship_type,
        1 AS depth,
        ARRAY[mr.parent_member_id, mr.child_member_id] AS path_visited
    FROM member_relationships mr
    JOIN tenant_memberships tm ON tm.id = mr.child_member_id AND tm.tenant_id = mr.tenant_id
    WHERE mr.tenant_id = :tenant_id
      AND mr.parent_member_id = :root_member_id
      AND mr.relationship_type = :relationship_type
      AND mr.status = 'ACTIVE'
      AND tm.status = 'ACTIVE'

    UNION ALL

    -- Recursive step: next-level children
    SELECT 
        child_mr.child_member_id,
        child_mr.parent_member_id,
        child_mr.relationship_type,
        dt.depth + 1 AS depth,
        dt.path_visited || child_mr.child_member_id
    FROM member_relationships child_mr
    JOIN descendant_tree dt ON dt.child_member_id = child_mr.parent_member_id
    JOIN tenant_memberships tm ON tm.id = child_mr.child_member_id AND tm.tenant_id = child_mr.tenant_id
    WHERE child_mr.tenant_id = :tenant_id
      AND child_mr.relationship_type = dt.relationship_type
      AND child_mr.status = 'ACTIVE'
      AND tm.status = 'ACTIVE'
      AND dt.depth < :max_depth
      AND NOT (child_mr.child_member_id = ANY(dt.path_visited)) -- Cycle protection
)
SELECT * FROM descendant_tree;
```

### 13.2 Indexing Strategy for High Performance
- **[Proposed]** Essential PostgreSQL B-Tree composite indexes:
  1. `ix_member_rel_parent_active`: `(tenant_id, parent_member_id, relationship_type, status)`
  2. `ix_member_rel_child_active`: `(tenant_id, child_member_id, relationship_type, status)`
  3. `ix_tenant_memberships_tenant_status`: `(tenant_id, status)`

---

## 14. Frontend Integration Plan

- **[Proposed]** **Data Flow Architecture:**
  $$\text{Next.js Frontend} \longrightarrow \text{Next.js API Proxy (/backend-api/...)} \longrightarrow \text{FastAPI Backend (/api/v1/...)} \longrightarrow \text{PostgreSQL}$$
- **[Proposed]** **Proxy Allowlist Additions:**
  In [`frontend/src/app/backend-api/[...path]/route.ts`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/backend-api/%5B...path%5D/route.ts):
  ```typescript
  "api/v1/agent/descendants",
  "api/v1/agent/descendants/stats",
  "api/v1/members/relationships",
  ```
- **[Proposed]** **UI Components & Pages:**
  1. Implement `/agent/customers/page.tsx`: Resolves current 404 in navigation. Displays paginated table of direct and multi-tier customers with depth filter tabs ("All", "Direct (L1)", "Tier 2", "Tier 3").
  2. Enhance `/agent/dashboard/page.tsx`: Add hierarchy summary cards displaying "Total Network Size" and "Direct Registrations" using the aggregate statistics endpoint.

---

## 15. Migration Strategy and Rollback Considerations

- **[Proposed]** **Alembic Revision Plan: `20261018_16_member_relationships.py`**
  - Revision ID: `20261018_16`.
  - Down Revision: `20261017_15`.
  - Upgrade steps:
    1. Create `member_relationships` table.
    2. Add foreign keys `fk_member_rel_parent` and `fk_member_rel_child` with `ON DELETE RESTRICT`.
    3. Add check constraint `ck_member_rel_no_self_link`.
    4. Create partial unique indexes and query performance indexes.
    5. Seed catalog permission codes and assign Phase 3 permissions to `ADMIN` and `AGENT` roles in all tenants.
    6. (Optional) Backfill existing `SubscriberProfile.registered_by_agent_membership_id` into initial `REFERRED_BY` records.
  - Downgrade steps:
    1. Drop indexes and constraints.
    2. Drop table `member_relationships`.
    3. Unseed Phase 3 permission codes from `role_permissions` and `permissions`.

---

## 16. Security, Privacy, Performance, and Concurrency Risks

- **[Verified]** **IDOR Protection:** Agents can never supply arbitrary root IDs; the root is hardcoded to `session.membership_id`.
- **[Verified]** **Tenant Contamination:** Composite foreign keys to `tenant_memberships(tenant_id, id)` prevent cross-tenant links at the catalog level.
- **[Verified]** **Cycle Hazards:** Acyclic integrity is enforced via pre-insert CTE traversal checks and query-time path array cycle checks.
- **[Verified]** **PII Leakage Prevention:** Responses strictly exclude Aadhaar/PAN fields and apply regex masking to phone numbers.
- **[Verified]** **Concurrency Locking:** Creation and revocation of relationships will use pessimistic row locking (`SELECT FOR UPDATE`) on the child membership record to prevent concurrent parent assignment races.

---

## 17. Test Matrix and Acceptance Criteria

### 17.1 Test Suites
| Category | Test Scenario | Expected Outcome | Environment |
| :--- | :--- | :--- | :--- |
| **Data Integrity** | Insert relationship with parent and child in different tenants. | Rejected with Foreign Key Violation (PostgreSQL). | PostgreSQL |
| **Data Integrity** | Insert self-link (`parent_member_id == child_member_id`). | Rejected with Check Constraint Violation (ck_no_self_link). | SQLite / PostgreSQL |
| **Data Integrity** | Insert circular link (A -> B, B -> C, C -> A). | Service pre-check rejects with HTTP 400 "Hierarchy cycle detected". | SQLite / PostgreSQL |
| **Data Integrity** | Insert second active `REPORTS_TO` parent for same child. | Rejected with Unique Index Violation. | SQLite / PostgreSQL |
| **Data Integrity** | Soft revoke relationship and verify history. | Status becomes `REVOKED`, `ended_at` set, old record queryable. | SQLite / PostgreSQL |
| **Authorization** | Agent requests descendants without `MEMBER_DESCENDANTS_READ`. | Fails closed with HTTP 403 Forbidden. | SQLite / PostgreSQL |
| **Authorization** | Agent attempts to query another agent's descendants via param tampering. | Root anchor ignores query param, uses session membership. | SQLite / PostgreSQL |
| **Authorization** | Tenant Admin manages relationships within own tenant. | Allowed (HTTP 201 / HTTP 200). | SQLite / PostgreSQL |
| **Authorization** | Tenant Admin attempts to manage relationships in another tenant. | Rejected with HTTP 403 / 404 (Tenant mismatch). | SQLite / PostgreSQL |
| **Privacy** | Query descendant roster. | Masked mobile returned, zero Aadhaar/PAN fields present. | SQLite / PostgreSQL |
| **Performance** | Query 5-level descendant tree with 1,000 members. | Query completes in < 25ms using recursive CTE. | PostgreSQL |
| **Proxy Integration** | Next.js proxy route tests for `/api/v1/agent/descendants`. | Correct cookie forwarding and route pass-through. | Node / Jest |

---

## 18. Unresolved Business Decisions Requiring Owner Input

The Project Owner must review and decide on the following five policy points:

1. **Default Registration Relationship Type:**
   - *Question:* When an agent registers a subscriber via `POST /api/v1/agent/users`, which relationship type should be automatically created?
   - *Recommendation:* Create both `REFERRED_BY` (for commission tracking) and `REPORTS_TO` (for operational hierarchy).
2. **Maximum Permitted Query Depth for Agents:**
   - *Question:* What is the maximum descendant traversal depth permitted for agents?
   - *Recommendation:* Cap at depth = 3 for agents (sufficient for multi-tier commission view), cap at depth = 10 for Tenant Admins.
3. **Multi-Parent Policy for Management (`MANAGES`):**
   - *Question:* Can a subscriber or agent have multiple concurrent managers with `MANAGES` relationships?
   - *Recommendation:* Yes, allow multiple `MANAGES` relations (matrix teams), but enforce strictly single-parent for `REPORTS_TO` and `REFERRED_BY`.
4. **Member Deactivation & Orphan Handling:**
   - *Question:* When an agent is deactivated or suspended, how should their descendant subtree be treated?
   - *Recommendation:* Retain existing relationship records in database, but dynamically filter them out in traversal (`tm.status == 'ACTIVE'`). Descendants remain linked to the parent ID but operational reporting is paused until reassigned by Tenant Admin.
5. **Legacy Data Backfill:**
   - *Question:* Should existing `SubscriberProfile.registered_by_agent_membership_id` rows be backfilled into `member_relationships` during migration `20261018_16`?
   - *Recommendation:* Yes, execute an `INSERT INTO member_relationships ... SELECT` backfill in the migration to ensure legacy registered subscribers appear seamlessly in the new hierarchy.

---

## 19. Ordered Implementation Plan with Dependencies

```mermaid
flowchart TD
    A["Owner Decision on Business Questions (Sec 18)"] --> B["Step 1: Database Migration 20261018_16"]
    B --> C["Step 2: SQLAlchemy Models & Integrity Constraints"]
    C --> D["Step 3: Permission Seeding Update (seeding.py)"]
    D --> E["Step 4: Recursive Hierarchy Query Service"]
    E --> F["Step 5: Backend API Routers (members & agent/descendants)"]
    F --> G["Step 6: Backend Unit & Integration Tests"]
    G --> H["Step 7: Next.js Proxy Allowlist Update"]
    H --> I["Step 8: Frontend Pages (/agent/customers & dashboard)"]
    I --> J["Step 9: End-to-End Verification & Concurrency Audit"]
```

1. **Step 1: Database Migration (`20261018_16_member_relationships.py`):**
   - Create `member_relationships` table, foreign keys, check constraints, indexes, and legacy backfill.
2. **Step 2: Models & Schemas:**
   - Define `MemberRelationship`, `RelationshipType`, `RelationshipStatus` in backend models and Pydantic schemas.
3. **Step 3: RBAC Seeding:**
   - Add `MEMBER_RELATIONSHIP_MANAGE`, `MEMBER_DESCENDANTS_READ`, `MEMBER_STATS_READ`, `MEMBER_DESCENDANTS_CREATE` to `ADMIN_PERMISSIONS` and `AGENT_PERMISSIONS` in `seeding.py`.
4. **Step 4: Query Service:**
   - Implement `RelationshipQueryService` with cycle check, bounded depth recursive CTE, and aggregate stats calculations.
5. **Step 5: API Endpoints:**
   - Add `/api/v1/members/relationships` and `/api/v1/agent/descendants` routers to `backend/app/api/v1/router.py`.
6. **Step 6: Automated Test Verification:**
   - Implement comprehensive pytest suite (`test_member_relationships_and_hierarchy.py`).
7. **Step 7: Proxy Allowlist:**
   - Add new prefixes to `ALLOWLISTED_PREFIXES` in `frontend/src/app/backend-api/[...path]/route.ts`.
8. **Step 8: Frontend Implementation:**
   - Create `frontend/src/app/agent/customers/page.tsx` and enhance agent dashboard.
9. **Step 9: End-to-End Verification:**
   - Full regression suite, lint, typecheck, build, and security re-verification.

---

## 20. Explicit Out-of-Scope Items

The following items are strictly out of scope for Phase 3:
1. **Commission Calculation and Payout Execution:** Financial payouts, ledger entries, and automated payout disbursement are part of Phase 4 (Commission and Billing).
2. **Multi-Tenant Federation:** Cross-community hierarchy or sharing members between different communities is prohibited.
3. **Direct Database Connections from Frontend:** The Next.js client will never connect directly to PostgreSQL.
4. **Modification of Phase 2 Approval Workflows:** Agent appointment workflows, two-eye approval, and super admin governance established in Phase 2 remain untouched.

---

## 21. Final Recommendation and Approval Gate

- **[Verified]** Discovery is 100% complete. Every requirement, gap, constraint, and integration touchpoint has been verified against repository source code and database models.
- **[Verified]** The codebase is in a clean, stable state with 314 passing backend tests, 19 passing proxy tests, clean TypeScript typechecks, clean lint, and successful frontend builds.
- **[Verified]** No application code, migrations, or database schemas have been modified.

**Status: DISCOVERY COMPLETE — AWAITING PROJECT OWNER REVIEW**

**Implementation authorization: NOT GRANTED**

