# Community OTT --- Agent Platform Architecture

**Document type:** Architecture and implementation contract\
**Status:** Proposed baseline for implementation\
**Audience:** Backend engineers, frontend/mobile engineers, QA, security
reviewers, and coding agents\
**System context:** Agent Platform within the multi-tenant Community OTT
product\
**Purpose:** Define the architecture, domain boundaries, data
relationships, authorization rules, approval workflows, APIs, and
testing obligations required to implement the Agent Platform
consistently.

------------------------------------------------------------------------

## 1. Purpose and Context

Community OTT serves multiple communities through one shared backend.
Each community is a **tenant**. A tenant may have its own domain,
branding, languages, content catalogue, members, subscription
configuration, regions, and agent organization.

The Agent Platform is a business capability within Community OTT. It
manages who may recruit or manage members, which community they operate
in, which region they cover, how members relate to one another, who can
approve agent appointments, and what actions each user can perform.

The objective is **not** to create a separate backend per community. The
objective is to run one application and enforce tenant-specific business
data and permissions using a trusted `tenant_id` context.

### Core principles

1.  **Tenant isolation first:** every tenant-owned operation is
    constrained to the resolved community.
2.  **RBAC:** permissions are assigned to roles; roles are assigned to
    users through tenant-scoped assignments.
3.  **Declarative endpoint authorization:** each protected API declares
    the permission it requires,
    e.g. `@require_permission("agent.create")`.
4.  **Scope-aware authorization:** permissions are additionally
    constrained by region, resource relationship, and policy.
5.  **Separate concepts:** user identity, tenant membership, roles,
    permissions, region assignment, member hierarchy, agent appointment,
    and approval are distinct records.
6.  **Approval before activation:** proposing an agent never grants
    agent privileges.
7.  **Least privilege:** no role, scope, or hierarchy relationship
    grants implicit capabilities beyond explicit policy.
8.  **Auditability:** security-sensitive actions and approval decisions
    are recorded.
9.  **Loose coupling:** future modules such as notifications, reports,
    and community-specific workflows integrate through defined
    interfaces and events.
10. **One shared implementation:** business modules reuse the same
    tenant context, authorization service, and policy engine.

### Non-negotiable security rule

> No protected operation may execute until the server has resolved the
> tenant, authenticated the caller in that tenant, checked the required
> permission, validated region/resource access, and enforced the
> relevant business-state rules.

UI hiding is not authorization. All enforcement happens on the server.

------------------------------------------------------------------------

## 2. Product and Domain Terminology

  -----------------------------------------------------------------------
  Term                                Meaning
  ----------------------------------- -----------------------------------
  Platform                            The shared Community OTT software
                                      and its operators.

  Tenant / Community                  An independent community using the
                                      OTT platform. Every tenant has a
                                      stable ID.

  Tenant domain                       A verified hostname mapped to a
                                      tenant,
                                      e.g. `community-a.example.com`. A
                                      domain identifies the tenant
                                      context; it does not itself
                                      authorize a user.

  User identity                       Authentication identity for a
                                      person. It may be usable across
                                      multiple communities if the product
                                      supports shared identity.

  Tenant membership                   The relationship between a user and
                                      one community. Roles and
                                      operational status are
                                      tenant-specific.

  Agent                               A tenant member who has an
                                      approved, active agent assignment.
                                      It is a business capability, not a
                                      separate authentication system by
                                      default.

  Member                              A person who participates in a
                                      community and may be part of the
                                      community's organizational/referral
                                      hierarchy.

  Role                                A named collection of permissions,
                                      such as `COMMUNITY_ADMIN`,
                                      `REGION_AGENT`, or
                                      `AGENT_REVIEWER`.

  Permission                          An operation code such as
                                      `agent.create`, `agent.approve`, or
                                      `member.stats.read`.

  Region                              A canonical tenant-owned geographic
                                      or operational region.

  Region tag                          Metadata for
                                      search/filtering/classification. A
                                      free-form tag is not an
                                      authorization grant.

  Region assignment                   A tenant-scoped relationship that
                                      authorizes a user/agent to operate
                                      in a region.

  Member relationship                 An explicit relationship between
                                      members, e.g. `REPORTS_TO`,
                                      `REFERRED_BY`, or `MANAGES`.

  Agent appointment                   A request to appoint a user as an
                                      agent with a specific role and
                                      region scope.

  Approval request                    Workflow record that tracks review
                                      and decision of an appointment or
                                      another protected action.

  Principal                           The authenticated user and trusted
                                      authorization context used by the
                                      server.

  Domain event                        A fact that occurred,
                                      e.g. `AgentAppointmentApproved`.
  -----------------------------------------------------------------------

Use "tenant" in code and database contracts, and "community" in
product-facing language where appropriate. Avoid using "admin" as an
implicit bypass. Administrative authority must be expressed through
roles, permissions, and policy.

------------------------------------------------------------------------

## 3. High-Level Architecture

### 3.1 Logical architecture

``` text
Community A domain ─┐
Community B domain ─┼──> Shared Community OTT backend
Community C domain ─┘                |
                                     v
                       Tenant Domain Resolver
                                     |
                                     v
                         Trusted TenantContext
                                     |
                                     v
                           Authentication
                                     |
                                     v
                    Tenant Membership Validation
                                     |
                                     v
                    Shared Authorization Framework
                   (RBAC + region + relationships
                       + resource/business policy)
                                     |
                                     v
             ┌──────────────────────────────────────────┐
             │ Agent Platform modules                   │
             │                                          │
             │ Identity / Tenant Membership             │
             │ Agent Management                         │
             │ Role & Permission Administration         │
             │ Region Management                        │
             │ Member Hierarchy                         │
             │ Approval Workflow                        │
             │ Audit                                    │
             │ Domain Events / Outbox                   │
             └──────────────────────────────────────────┘
                                     |
                                     v
                       Shared PostgreSQL database
                      (tenant-owned data is isolated)
                                     |
                                     v
                      Outbox worker / event consumers
                        ├── Notifications (future)
                        ├── Reporting (future)
                        └── Integrations (future)
```

### 3.2 Recommended deployment style

Start with a **modular monolith** in the existing Community OTT backend.
The Agent Platform should be a set of well-defined modules/packages, not
a separate backend deployed per community.

Do not split every module into a microservice initially. Keep ownership
and interfaces clear so a module can be extracted later if a
demonstrated scaling, availability, or team-ownership need arises.

### 3.3 Technology alignment

The existing Community OTT backend context includes a shared
tenant-aware backend and PostgreSQL. Implement this architecture in the
project's established stack and conventions. Do not introduce a second
authentication, database, or authorization framework solely for the
Agent Platform.

Use the existing tenant resolution and authentication design if already
implemented. If tenant support is still being implemented, this
document's tenant-context rules are the required contract.

------------------------------------------------------------------------

## 4. Tenant Context and Isolation

### 4.1 Resolve tenant before tenant-scoped identity and data access

For a request to a community domain:

1.  Read the request hostname from a trusted proxy/server configuration.
2.  Resolve the hostname through the verified tenant-domain registry.
3.  Verify that the tenant is active.
4.  Create a trusted request-scoped `TenantContext`.
5.  Authenticate the user.
6.  Verify the user has an active membership in that tenant, unless the
    endpoint is an explicitly authorized platform-level endpoint.
7.  Evaluate tenant-scoped RBAC and policies.
8.  Query or mutate records constrained to that tenant.

Conceptual sequence:

``` text
Trusted hostname
    -> tenant_domains lookup
    -> active tenant
    -> TenantContext(tenant_id)
    -> authenticate principal
    -> tenant membership check
    -> permission + region + relationship checks
    -> tenant-scoped service/repository
```

The hostname is a routing input, not an authorization credential. Do not
trust an arbitrary `tenant_id` in a body, query parameter, or header. If
the platform supports a trusted internal tenant header behind a gateway,
accept it only from the verified gateway and document the trust
boundary.

### 4.2 Token consistency

If the authentication token contains a `tenant_id`, verify it matches
the tenant resolved for the current request. Do not silently switch
tenant context based on a token or request field without the product's
explicit multi-tenant login flow.

A shared identity may have memberships in multiple tenants, but every
request must operate within one explicitly resolved tenant context.

### 4.3 Tenant-owned records

Every tenant-owned record must either: - contain a non-null `tenant_id`,
or - be reached only through a tenant-owned parent with enforceable
tenant-consistent foreign keys.

Prefer explicit `tenant_id` on high-value business records for query
clarity and defense in depth. Define a clear exception list for truly
global reference data such as the platform-wide permission catalogue.

Tenant-owned data includes, at minimum: - tenant memberships and tenant
role assignments - custom roles and role-permission links - regions and
region assignments - agent appointments and approval records - members
and member relationships - tenant-specific configuration and audit
events - future tenant-owned notification preferences and reports

### 4.4 Tenant isolation requirements

-   Every tenant-scoped query is constrained by the trusted tenant
    context.
-   Never use an ID lookup alone for tenant-owned resources; query by
    `(tenant_id, resource_id)` or validate ownership before use.
-   A relationship must not connect a parent and child from different
    tenants.
-   An agent assignment must not reference a region from another tenant.
-   An appointment must not grant a role or region belonging to another
    tenant.
-   Cache keys, event envelopes, background jobs, exports, and audit
    queries must carry tenant context.
-   Background workers must re-establish tenant context from trusted
    event/job data and validate referenced records.
-   Add cross-tenant isolation tests for read, update, delete, list,
    search, exports, and background jobs.

PostgreSQL Row-Level Security may be used as defense in depth, but
application-level tenant enforcement remains mandatory. If RLS is
enabled, transaction/session tenant context must be set safely and reset
to avoid connection-pool leakage.

------------------------------------------------------------------------

## 5. Identity, Tenant Membership, and Agent Status

Do not combine user identity, tenant membership, and agent status in one
table or one `account_type` column.

### 5.1 Recommended conceptual model

``` text
users
  └── tenant_memberships
        ├── user_role_assignments
        ├── region_assignments
        ├── member profile (if applicable)
        └── agent appointments / active agent assignments
```

Suggested entities:

**`users`** --- global identity, authentication-provider reference,
global account status.

**`tenant_memberships`** --- `tenant_id`, `user_id`, tenant-specific
status, joined/created timestamps. Unique `(tenant_id, user_id)` if one
membership per user per tenant is the chosen policy.

**`agent_appointments`** --- records a proposed agent role and scope and
its approval lifecycle.

**`agent_assignments`** (or a well-defined approved appointment record)
--- records the active, approved operational assignment. Avoid
duplicating state without a clear source of truth.

A person can be an ordinary member in Community A and an agent in
Community B if the product permits it. Their roles, region access, and
agent appointment in one tenant must not carry over to another.

### 5.2 Status checks

An authenticated user is not automatically an active agent. Before
agent-only operations, verify: - the global account is permitted to
authenticate; - the tenant membership is active; - the relevant role
assignment is active and within its validity period; - the agent
appointment/assignment is approved and active where the operation
requires agent status; - the relevant region assignment is active.

------------------------------------------------------------------------

## 6. RBAC: Roles and Permissions

### 6.1 Relationship model

``` text
users
  └── tenant_memberships
        └── user_role_assignments
                 └── roles
                      └── role_permissions
                               └── permissions
```

Permissions define actions. Roles group permissions. User-role
assignments grant roles within a tenant and, where needed, a scope.

The permission catalogue can be shared platform-wide. Role definitions
and their permission assignments should be tenant-specific by default,
unless a specific role is deliberately designated as platform-managed
and immutable to tenant administrators.

### 6.2 Example roles

Illustrative roles; do not hardcode these role names into business
logic:

-   `COMMUNITY_OWNER` or `COMMUNITY_ADMIN`
-   `REGION_ADMIN`
-   `AGENT`
-   `AGENT_REVIEWER`
-   `MEMBER_SUPERVISOR`
-   `AUDITOR`
-   `MEMBER`

A tenant may define custom roles, subject to protected-role and
delegation rules.

### 6.3 Initial permission catalogue

Use lowercase dot-separated identifiers and maintain them in a
permission registry/migration.

**Agent operations** - `agent.create` --- submit a new agent
appointment - `agent.read` --- read agents the caller is allowed to
see - `agent.update` --- update permitted agent profile fields -
`agent.cancel` --- cancel a pending appointment, subject to policy -
`agent.approve` --- approve an eligible appointment - `agent.reject` ---
reject an eligible appointment - `agent.suspend` --- suspend an active
assignment - `agent.revoke` --- revoke an assignment

**Role and permission administration** - `role.create` - `role.read` -
`role.update` - `role.delete` - `role.assign` - `role.revoke` -
`permission.read` - `role_permission.assign` - `role_permission.revoke`

**Region administration** - `region.create` - `region.read` -
`region.update` - `region.manage` - `region.assign`

**Member and hierarchy operations** - `member.create` - `member.read` -
`member.update` - `member.deactivate` - `member.relationship.manage` -
`member.stats.read` - `member.descendants.read` -
`member.descendants.create`

**Audit and future features** - `audit.read` - `notification.read` -
`notification.manage`

Finalize the catalogue against actual APIs. Do not add a permission to
code without registering it. Unknown permission codes must fail closed
and should be caught by automated tests/startup validation.

### 6.4 RBAC is necessary but not sufficient

Do not implement authorization as `if user has role X`. Check a
permission and evaluate the relevant policies. A caller with
`role.create` must not automatically be allowed to create a protected
platform role. A caller with `role_permission.assign` must not be able
to grant any permission to any role.

### 6.5 Permission delegation

The authorization policy must determine which roles and permissions the
caller may assign. A safe baseline is: - a caller may only manage roles
within their tenant and delegated scope; - a caller may only grant
permissions they are explicitly authorized to delegate; - tenant
administrators cannot grant platform-level privileges; -
protected/system roles cannot be modified or deleted by tenant
administrators; - high-risk role/permission changes can require approval
if configured; - all security configuration changes are audited.

These restrictions belong in the service/policy layer, not just in the
UI.

------------------------------------------------------------------------

## 7. Declarative API Authorization

### 7.1 Required endpoint pattern

Every protected endpoint declares its required permission.

Python-style conceptual example:

``` python
@require_permission(name="agent.create")
def create_agent_api(request):
    return agent_service.submit_appointment(
        tenant_id=request.tenant.id,
        requested_by=request.current_user.id,
        data=request.json,
    )
```

``` python
@require_permission(name="agent.approve")
def approve_agent_api(request, appointment_id):
    return agent_service.approve_appointment(
        tenant_id=request.tenant.id,
        appointment_id=appointment_id,
        reviewer=request.current_user,
    )
```

Role administration follows the same pattern:

``` python
@require_permission(name="role.create")
def create_role_api(request):
    ...

@require_permission(name="role_permission.assign")
def assign_permission_to_role_api(request, role_id, permission_id):
    ...
```

The examples describe the contract. Implement them using the project's
actual framework, e.g. a decorator/middleware in Python or method
security/interceptor in Spring Boot. Do not create two competing
authorization implementations.

### 7.2 Shared authorization service

The decorator/interceptor is a gate, not the complete policy engine. It
should delegate to a central service such as:

``` python
authorization_service.require(
    principal=current_principal,
    tenant_id=current_tenant.id,
    permission="member.create",
    resource=target_region_or_member,
    context={"operation": "create_member"},
)
```

The service evaluates: 1. trusted tenant context and active tenant; 2.
active user and tenant membership; 3. active role assignments and
required permission; 4. region scope; 5. relationship-based access, if
applicable; 6. resource state and business policies; 7. delegation
restrictions, where relevant.

The handler must not run if authorization fails.

### 7.3 Fail closed and error conventions

-   `401` --- missing/invalid authentication.
-   `403` --- authenticated but unauthorized.
-   `404` --- optionally conceal resource existence, according to a
    consistent policy.
-   `409` --- conflict with current workflow/resource state.
-   `400` or `422` --- invalid input according to the project's API
    convention.

If required authorization data or policy evaluation is unavailable,
protected actions fail closed. Never treat an exception, missing
permission, or unknown permission as an allow decision.

------------------------------------------------------------------------

## 8. Region Model: Canonical Regions Plus Metadata Tags

### 8.1 Do not use free-form tags as authorization

The product may use region tags for metadata, search, filtering, and
categorization. Examples include `coastal`, `rural`, `tribal`, or a
local classification.

However, a tag such as `"visakhapatnam"` must not be the sole security
boundary. Use a canonical region record with a stable ID and parent
relationship. Region assignments reference that ID.

### 8.2 Suggested tables

**`regions`** - `id` - `tenant_id` - `parent_region_id` (nullable for
root) - `region_type` (e.g. state, district, mandal, village, locality;
final taxonomy is configurable) - `code` - `name` - `status` -
timestamps

**`region_tags`** or tenant-safe metadata table - `tenant_id` -
`region_id` - `tag_key` / `tag_value` or a normalized tag relationship -
optional creator/timestamps

**`region_assignments`** - `id` - `tenant_id` - `tenant_membership_id`
or `user_id` - `region_id` - `assignment_status` - `assigned_by` -
`valid_from`, `valid_until` - timestamps/revocation metadata

Add tenant-consistent foreign keys and appropriate uniqueness
constraints.

### 8.3 Multiple agents in one region

Many agents can be assigned to one region. One agent may be assigned to
multiple regions if policy allows it. Therefore, model assignments as a
relationship table, not a single `agent_id` column on `regions`.

An agent can only operate in a region when the assignment is active and
the agent has the required permission. A pending appointment or pending
region assignment grants no access.

### 8.4 Region hierarchy rules

A parent region may cover descendants only if the policy for the
operation permits inherited scope. A state-level assignment should not
be assumed to grant every permission in all descendant regions;
permission and scope are separate checks.

------------------------------------------------------------------------

## 9. Member Hierarchy and Relationship-Based Access

The member hierarchy is distinct from the region hierarchy and distinct
from RBAC.

Example:

``` text
Tenant T001
  └── Member 1
       └── Member 2
            ├── Member 3
            └── Member 4
```

Member 1 may be allowed to view statistics for Member 2 and descendants
if: - Member 1 has the required permission, such as `member.stats.read`
and/or `member.descendants.read`; - the relationship is active and
belongs to the same tenant; - the target member is a direct/indirect
descendant under the configured policy; - the requested data type is
allowed by policy.

The relationship alone must not automatically grant profile-edit,
role-management, subscription-management, or arbitrary personal-data
access.

### 9.1 Suggested table

**`member_relationships`** - `id` - `tenant_id` - `parent_member_id` -
`child_member_id` - `relationship_type` (e.g. `REPORTS_TO`,
`REFERRED_BY`, `MANAGES`) - `status` - `created_by` - `created_at` -
`ended_at` / revocation metadata

### 9.2 Relationship constraints

-   parent and child must belong to the same tenant;
-   parent and child cannot be the same record;
-   prevent cycles for hierarchy types such as `REPORTS_TO`;
-   prevent duplicate active relationships where not allowed;
-   define whether each child has one parent or multiple parents;
-   define whether descendant access is direct-only or recursive;
-   revoking a relationship must remove current relationship-based
    access;
-   historical records may remain for audit/reporting without granting
    current access.

### 9.3 Statistics and descendants

Keep statistics APIs separate from general member read APIs if they
expose different data. Prefer aggregate/read-model queries that return
only permitted fields rather than loading all descendant profiles.
Enforce tenant and hierarchy filters in the database query, not just in
the UI.

------------------------------------------------------------------------

## 10. Agent Appointment and Approval Workflow

### 10.1 Separate identity from appointment

Creating or finding a user does not immediately make them an active
agent. The appointment request defines the requested role, region, and
any required attributes. It must be reviewed before the corresponding
assignment becomes effective.

A person can already be a member and later be appointed as an agent
without creating a duplicate identity.

### 10.2 Appointment state

Suggested states: - `PENDING` - `APPROVED` - `REJECTED` - `CANCELLED` -
`EXPIRED`

Active assignment status is separate: - `ACTIVE` - `SUSPENDED` -
`REVOKED` - `EXPIRED`

Do not conflate approval outcome with current operational status. An
approved assignment can later be suspended or revoked while retaining
its decision history.

### 10.3 Flow

1.  Caller invokes the agent appointment API with `agent.create`.
2.  Backend validates tenant, requested role, region, and delegation
    authority.
3.  Backend records a pending appointment.
4.  Workflow identifies eligible reviewers or routes it to a review
    queue.
5.  Reviewer reads the request under the proper tenant and scope.
6.  Reviewer invokes approve/reject endpoint with `agent.approve` or
    `agent.reject`.
7.  Workflow validates reviewer scope, no-self-approval policy, current
    state, and requested-versus-approved details.
8.  Approval and activation of exactly the approved role/region
    assignment occur atomically or through an explicitly reliable
    transactional design.
9.  Audit events and outbox events are recorded.
10. Notifications, if enabled, consume the events asynchronously.

If no eligible reviewer exists, the request remains pending and is
escalated/reassigned according to policy. It must never be auto-approved
because no reviewer is available.

### 10.4 Reviewer mapping

Reviewer authority is independent from operational agent authority. A
reviewer must have an explicit permission and be eligible for the
appointment's tenant, type, and scope. A reviewer role name alone is not
enough. The requester should not approve their own appointment by
default.

### 10.5 Concurrency

Use transactions and version/locking controls to prevent: - concurrent
approve and reject decisions; - approving a cancelled, expired, or
already decided request; - duplicate active assignments from retries; -
activation of a role or region different from the approved request.

Use idempotency where repeated client requests could create duplicates.
Return `409 Conflict` when a state transition is no longer valid.

------------------------------------------------------------------------

## 11. Role and Region Assignment Lifecycles

Keep the following relationships explicit and independently auditable:

-   user ↔ tenant membership
-   tenant membership ↔ role assignment
-   role ↔ permission
-   tenant membership ↔ region assignment
-   member ↔ member relationship
-   appointment ↔ approval decision

A region assignment does not grant permissions. A role assignment does
not grant access outside its scope. A member relationship does not grant
arbitrary permissions. An approved appointment only activates the exact
role and region scope approved.

If the product requires role and scope to be inseparable for certain
appointments, represent them as one `agent_assignment` record
referencing both a role and a region. Otherwise, separate role and
region assignments may be used. Do not accidentally create contradictory
duplicate sources of truth.

------------------------------------------------------------------------

## 12. Suggested Data Model

The following is a logical model, not a final migration script. Align
names with existing Community OTT conventions.

### Platform/global records

-   `tenants`
-   `tenant_domains`
-   `users` (global identity, if shared identity is supported)
-   `permissions` (global permission catalogue)

### Tenant-owned records

-   `tenant_memberships`
-   `roles`
-   `role_permissions` (tenant-scoped if roles are tenant-specific)
-   `user_role_assignments`
-   `regions`
-   `region_tags` or equivalent metadata
-   `region_assignments`
-   `agent_appointments`
-   `approval_requests`
-   `approval_decisions`
-   `agent_assignments` (if separate from approved appointment)
-   `members`
-   `member_relationships`
-   `audit_events`
-   `outbox_events`

### Data consistency rules

-   Use primary keys and foreign keys.
-   Include `tenant_id` in tenant-owned rows and unique constraints as
    appropriate.
-   Prefer composite foreign keys `(tenant_id, id)` or equivalent
    constraints to prevent cross-tenant references.
-   Index tenant-first access patterns, e.g. `(tenant_id, status)`,
    `(tenant_id, region_id)`, `(tenant_id, user_id)`, and pending
    approval queues.
-   Do not hard-delete approval/audit history.
-   Use versioned database migrations.
-   Use transaction boundaries for role/permission changes and approval
    decisions.
-   Use UTC timestamps and convert for display.
-   Avoid duplicating an active assignment if the approved appointment
    itself is the source of truth. Choose one authoritative lifecycle
    model.

------------------------------------------------------------------------

## 13. Suggested API Contracts

These endpoints are illustrative and must be reconciled with existing
API versioning and route conventions.

### 13.1 Agent appointment and review

  -----------------------------------------------------------------------------------------------------
  Method            Endpoint                                    Required           Additional checks
                                                                permission         
  ----------------- ------------------------------------------- ------------------ --------------------
  `POST`            `/api/v1/agent-appointments`                `agent.create`     Tenant, role
                                                                                   delegation, region
                                                                                   scope

  `GET`             `/api/v1/agent-appointments/{id}`           `approval.read` or Tenant and
                                                                specific           reviewer/requester
                                                                appointment-read   scope
                                                                permission         

  `GET`             `/api/v1/agent-appointments`                `approval.read`    Tenant and
                                                                                   authorized queue
                                                                                   scope

  `POST`            `/api/v1/agent-appointments/{id}/approve`   `agent.approve`    Eligible reviewer,
                                                                                   pending state, no
                                                                                   self-approval

  `POST`            `/api/v1/agent-appointments/{id}/reject`    `agent.reject`     Eligible reviewer,
                                                                                   reason and state
                                                                                   validation

  `POST`            `/api/v1/agent-appointments/{id}/cancel`    `agent.cancel`     Requester/manager
                                                                                   authority and valid
                                                                                   state

  `GET`             `/api/v1/agents`                            `agent.read`       Tenant, role, region
                                                                                   and visibility
                                                                                   filters

  `POST`            `/api/v1/agents/{id}/suspend`               `agent.suspend`    Tenant and
                                                                                   administrative scope

  `POST`            `/api/v1/agents/{id}/revoke`                `agent.revoke`     Tenant, authority,
                                                                                   audit
  -----------------------------------------------------------------------------------------------------

### 13.2 Role administration

  ----------------------------------------------------------------------------------------------------
  Method                  Endpoint                                          Required permission
  ----------------------- ------------------------------------------------- --------------------------
  `POST`                  `/api/v1/roles`                                   `role.create`

  `GET`                   `/api/v1/roles`                                   `role.read`

  `PATCH`                 `/api/v1/roles/{id}`                              `role.update`

  `DELETE`                `/api/v1/roles/{id}`                              `role.delete`

  `GET`                   `/api/v1/permissions`                             `permission.read`

  `POST`                  `/api/v1/roles/{id}/permissions`                  `role_permission.assign`

  `DELETE`                `/api/v1/roles/{id}/permissions/{permissionId}`   `role_permission.revoke`

  `POST`                  `/api/v1/memberships/{id}/roles`                  `role.assign`

  `DELETE`                `/api/v1/memberships/{id}/roles/{assignmentId}`   `role.revoke`
  ----------------------------------------------------------------------------------------------------

### 13.3 Region and member hierarchy

  ---------------------------------------------------------------------------------------------
  Method                  Endpoint                               Required permission
  ----------------------- -------------------------------------- ------------------------------
  `POST`                  `/api/v1/regions`                      `region.create`

  `GET`                   `/api/v1/regions`                      `region.read`

  `POST`                  `/api/v1/region-assignments`           `region.assign`

  `DELETE`                `/api/v1/region-assignments/{id}`      `region.assign` or dedicated
                                                                 revoke permission

  `POST`                  `/api/v1/members`                      `member.create`

  `GET`                   `/api/v1/members/{id}`                 `member.read`

  `POST`                  `/api/v1/members/{id}/relationships`   `member.relationship.manage`

  `DELETE`                `/api/v1/member-relationships/{id}`    `member.relationship.manage`

  `GET`                   `/api/v1/members/{id}/stats`           `member.stats.read`

  `GET`                   `/api/v1/members/{id}/descendants`     `member.descendants.read`
  ---------------------------------------------------------------------------------------------

For every endpoint, scope filters must be applied server-side. Do not
fetch cross-tenant or out-of-scope records and rely on frontend
filtering.

------------------------------------------------------------------------

## 14. Audit, Domain Events, and Future Notifications

### 14.1 Audit records

Capture at least: - tenant ID - actor/user ID - action/event type -
target resource type and ID - relevant region/scope -
success/denial/failure outcome where appropriate - timestamp and
correlation/request ID - approval decision and reason where applicable -
safe change metadata for role, permission, region, and membership
changes

Do not log passwords, tokens, session cookies, or unnecessary sensitive
personal data. Normal administrators must not be able to rewrite past
audit records. Protect audit queries with `audit.read` and tenant/scope
checks.

### 14.2 Domain events

Examples: - `AgentAppointmentSubmitted` - `AgentAppointmentApproved` -
`AgentAppointmentRejected` - `AgentAssignmentSuspended` -
`AgentAssignmentRevoked` - `MemberRegistered` -
`MemberRelationshipCreated` - `MemberRelationshipRevoked` -
`RolePermissionAssigned` - `RolePermissionRevoked` -
`RegionAssignmentChanged`

Every tenant-owned event must include `tenant_id`, event ID, event
type/schema version, occurrence time, and relevant resource identifiers.
Avoid putting unnecessary personal data in the event payload.

### 14.3 Transactional outbox

For important events, persist the business change and outbox record in
the same database transaction. A worker publishes the event after
commit. Consumers must be idempotent and safe to retry.

Notifications are a future consumer, not part of the authorization
decision. A failed notification must not undo an already committed
approval.

------------------------------------------------------------------------

## 15. Caching, Background Jobs, and Search

Tenant isolation applies outside normal HTTP request handling too.

### Cache

-   Include `tenant_id` in keys for tenant-owned data.
-   Include relevant user/role/region version information in
    authorization-sensitive caches.
-   Invalidate after role, permission, membership, region assignment, or
    hierarchy changes.
-   Do not allow stale authorization data to outlive the documented
    revocation window.
-   Fail closed or consult authoritative data if cache correctness
    cannot be established for a protected action.

### Background jobs

-   Include tenant ID and resource IDs in job envelopes.
-   Validate tenant ownership again in the worker.
-   Do not trust a client-provided tenant ID simply because it was
    copied into a job.
-   Ensure retries are idempotent.

### Search and reporting

-   Apply tenant and authorization filters before returning results.
-   Include tenant partitioning in indexes and search documents.
-   Prevent cross-tenant aggregation or autocomplete leakage.
-   Reports must honor region and member hierarchy access, not merely
    tenant membership.

------------------------------------------------------------------------

## 16. Testing Requirements

Testing must prove both correct access and denied access.

### Tenant isolation

-   A Community A user cannot read, mutate, enumerate, or export
    Community B data.
-   A domain mapped to an inactive or unknown tenant is rejected.
-   A token tenant mismatch is rejected.
-   Cross-tenant member relationships, region assignments, role
    assignments, and approvals are rejected.
-   Background jobs and event consumers cannot cross tenant boundaries.
-   Cache keys and search indexes cannot leak cross-tenant records.

### RBAC

-   Missing required permission is denied.
-   Correct permission plus valid policy is allowed.
-   Unknown permission code fails closed.
-   Role permission removal takes effect within the defined revocation
    window.
-   Suspended tenant membership or agent assignment is denied.
-   Tenant admin cannot grant platform-level permissions or protected
    roles.

### Region and hierarchy

-   Multiple agents can be assigned to the same region.
-   Agent cannot operate outside assigned region.
-   Parent-region inheritance follows documented policy.
-   Parent member can read only permitted direct/indirect descendant
    information.
-   Relationship revocation removes current relationship-based access.
-   Cycles, self-links, duplicate active relationships, and cross-tenant
    relationships are rejected.

### Approval workflow

-   Submission creates a pending appointment and no active agent
    privilege.
-   Only eligible reviewers can approve/reject.
-   Self-approval is denied by default.
-   Concurrent conflicting decisions cannot both succeed.
-   Cancelled/expired/decided requests cannot be approved.
-   Approval activates exactly the role and region scope approved.
-   Missing reviewer does not trigger automatic approval.

### Audit/events

-   Required security mutations produce audit history.
-   Business mutation and outbox record commit consistently.
-   Retries do not duplicate assignments or notification side effects.
-   Notification failures do not roll back completed business
    operations.

------------------------------------------------------------------------

## 17. Implementation Guidance for Coding Agents

Any coding agent implementing a feature in the Agent Platform must:

1.  Read this document and inspect existing Community OTT
    tenant/authentication patterns before coding.
2.  Reuse the existing tenant resolver, tenant context, identity model,
    and database conventions. Do not create a second tenant system.
3.  Identify module ownership and public interfaces.
4.  Document the endpoint contract and required permission before
    implementation.
5.  Register permission codes and seed/migrate them consistently.
6.  Apply the shared permission decorator/interceptor to every protected
    endpoint.
7.  Use the shared authorization service for tenant, role, region,
    relationship, and resource policy checks.
8.  Never trust request-body `tenant_id`, role names, region tags, or
    member IDs as proof of authority.
9.  Keep region metadata separate from region assignments.
10. Keep member relationships separate from roles and region hierarchy.
11. Keep appointment submission separate from approval and activation.
12. Do not access or mutate another module's tables directly; use its
    public interface or an approved shared contract.
13. Add migrations, API documentation, audit behavior, and
    positive/negative tests.
14. Do not weaken tenant isolation or privilege-escalation safeguards to
    make tests pass.
15. Do not invent undecided product rules. Document the decision needed
    and use the safest default.
16. Update architecture or module specifications when a reviewed
    contract changes.

### Required deliverables for each feature

-   Module ownership and design summary
-   API request/response contract
-   Permission mapping
-   Tenant and scope policy
-   Database migration and constraints, if applicable
-   Audit and event behavior
-   Positive, negative, tenant-isolation, and concurrency tests
-   Updated API/permission documentation
-   Known limitations and unresolved decisions

------------------------------------------------------------------------

## 18. Definition of Done

A feature is not complete until:

-   [ ] It reuses the shared Community OTT tenant context.
-   [ ] Tenant ownership is enforced for every read and mutation.
-   [ ] Every protected endpoint declares a required permission.
-   [ ] Region and member relationship policies are enforced
    server-side.
-   [ ] No pending appointment grants active agent privileges.
-   [ ] Role/permission delegation cannot exceed the caller's authority.
-   [ ] Database constraints prevent cross-tenant relationships where
    possible.
-   [ ] Required audit and outbox records are consistent with the
    transaction.
-   [ ] Positive, negative, tenant-isolation, concurrency, and
    regression tests pass.
-   [ ] Logs/errors do not expose secrets or cross-tenant data.
-   [ ] The API and permission registry documentation are updated.

------------------------------------------------------------------------

## 19. Decisions to Finalize Before Production

The core architecture is established, but these product policies require
explicit decisions:

1.  **Identity:** can one global user identity join multiple
    communities, or does each community require separate credentials?
2.  **Tenant onboarding:** who creates a community's initial
    administrator, and how is that identity verified?
3.  **Region taxonomy:** which geographic levels are supported and which
    source defines canonical region IDs?
4.  **Scope inheritance:** which operations inherit access from a parent
    region to child regions?
5.  **Member hierarchy:** does a member have one parent or multiple
    relationships, and which relationship types are supported?
6.  **Descendant access:** which roles can view aggregate statistics,
    member profiles, or create descendants?
7.  **Delegation:** which roles/permissions can each tenant
    administrator grant, and which changes require approval?
8.  **Approval:** one reviewer or multiple stages; expiry, reassignment,
    escalation, and self-approval policy.
9.  **Revocation:** accepted delay for changes to roles, permissions,
    membership, region assignments, and relationships to take effect.
10. **Audit retention:** retention period, access restrictions, and any
    external archival requirements.

Until decided, use least privilege, deny ambiguous access, preserve
history, and do not automatically grant descendant or cross-region
access.

------------------------------------------------------------------------

## 20. Architecture Summary

The Community OTT Agent Platform uses one shared backend and a
tenant-aware modular architecture.

-   **Tenant context** identifies the community whose data is being
    accessed.
-   **Identity and tenant membership** establish who the user is and
    whether they belong to that community.
-   **RBAC** determines which operations the user may perform.
-   **Region assignments** constrain where the user may operate.
-   **Member relationships** define reporting/referral/supervisory
    relationships and potential descendant visibility.
-   **Approval workflow** controls when proposed agent assignments
    become effective.
-   **Audit and events** provide accountability and loose coupling for
    future features.

These dimensions must remain separate and be evaluated together by the
shared authorization framework. This enables each Community OTT tenant
to have its own agent organization and member hierarchy without a
separate backend or accidental cross-community access.

------------------------------------------------------------------------

## 21. Registration Photo Capture and Upload Requirements

This section is an architecture amendment for agent creation by an administrator and user/member creation by an administrator. It supplements the existing identity, tenant isolation, authorization, API, audit, and testing contracts. If an earlier section is silent about registration photos, this section is authoritative.

### 21.1 Product rules

1. **Agent creation:** a profile photo is required when an authorized administrator creates an agent or submits an agent appointment. The administrator must be able to either:
   - capture a photo live using a supported device camera; or
   - upload an existing picture from the device.
2. **User/member creation:** the same two photo methods must be available, but a photo is optional by default.
3. **Super Admin configuration:** Super Admin can configure whether a photo is required for administrator-created users/members. The default is `OPTIONAL`. When changed to `REQUIRED`, new user/member creation must be blocked until a valid photo has been captured or uploaded. When changed back to `OPTIONAL`, new user/member creation may proceed without a photo.
4. **Configuration scope:** store this policy per community/tenant so Super Admin can configure the requirement for the selected community without unintentionally changing another community's policy. Only Super Admin may change this setting unless a future explicit delegation policy is approved.
5. **Equivalent methods:** live capture and file upload satisfy the same photo requirement. The system must not require both methods.
6. **Existing records:** changing the policy affects new user/member creation from the effective time of the change. It does not automatically invalidate existing users or remove existing photos.
7. **Agent policy:** the user/member photo setting must not weaken the mandatory agent photo requirement.

### 21.2 User-facing registration flow

#### Agent creation by Admin

1. Admin opens Agent Management and starts agent creation/appointment.
2. Admin enters the required agent details and chooses **Capture Photo** or **Upload Photo**.
3. For live capture, the browser requests camera permission and shows a preview before the administrator confirms the capture. If permission is denied or the camera is unavailable, the upload option remains available.
4. For upload, the interface accepts an allowed image file and displays a preview so the administrator can confirm or replace it.
5. The frontend validates basic file constraints for usability; the backend independently validates the uploaded content.
6. The backend verifies the caller's tenant, permission, role/region delegation, and required fields, then validates and stores the photo securely.
7. The appointment is saved as `PENDING`. The photo requirement does not bypass the existing approval workflow and does not activate the agent.
8. The reviewer can view the photo only if authorized to view that appointment. Approval activates only the approved role and region scope.

#### User/member creation by Admin

1. Admin opens Members/Users and starts creating a user.
2. The frontend loads the photo requirement setting for the current tenant from the backend.
3. If the setting is `OPTIONAL`, the form allows submission without a photo and offers **Capture Photo** and **Upload Photo**.
4. If the setting is `REQUIRED`, the form requires one valid photo from either method before enabling successful submission.
5. The backend rechecks the current tenant policy at submission time. Frontend validation alone is never sufficient.
6. The backend validates the user data and photo (when supplied), creates the user/member record, and stores a reference to the photo where applicable.
7. If the policy changed after the form loaded, the backend's current policy wins. A submission missing a newly required photo is rejected with a clear validation error so the administrator can add one and retry.

### 21.3 Super Admin setting

Provide a protected setting in the Super Admin community configuration area:

- Setting name: `User profile photo requirement`
- Values: `Optional` / `Required`
- Default for a newly configured community: `Optional`
- Scope: one selected tenant/community
- Permission: Super Admin-only by default
- Audit: record the actor, tenant, previous value, new value, and timestamp

The frontend must retrieve this setting through the backend API. Do not hardcode the policy in frontend components or infer it from the administrator's role. Cache invalidation must ensure a policy change takes effect within the documented configuration propagation window; the create-user API must always enforce the authoritative current setting.

### 21.4 API contract additions

The following are proposed additions and must be aligned with the existing API versioning and naming conventions.

| Method | Endpoint | Purpose | Authorization |
|---|---|---|---|
| `GET` | `/api/v1/tenant-settings/user-photo-policy` | Read the selected tenant's user-photo policy for form rendering | Authenticated user with access to the tenant settings needed for user creation; no cross-tenant reads |
| `PATCH` | `/api/v1/platform/tenants/{tenant_id}/settings/user-photo-policy` | Set `OPTIONAL` or `REQUIRED` for a selected community | Super Admin platform-level permission; verify the target tenant is valid |
| `POST` | Existing agent appointment/creation endpoint | Accept the required agent photo as multipart upload or a securely issued upload reference | `agent.create` plus existing tenant, scope, and delegation checks |
| `POST` | Existing user/member creation endpoint | Accept an optional or required photo according to current tenant policy | Existing member/user creation permission plus tenant and policy checks |

Photo bytes may be submitted as `multipart/form-data`, or through a pre-authorized upload flow if the project already has one. If a storage upload reference is used, the backend must verify that it belongs to the current tenant, was issued for the intended operation, and references a completed, validated upload. Do not expose storage credentials or unrestricted object-storage URLs to the frontend.

Suggested policy response:

```json
{
  "tenant_id": "resolved-by-server",
  "user_photo_policy": "OPTIONAL"
}
```

The `tenant_id` shown above is illustrative response data, not a value the client may use to select or override its trusted tenant context. Create/update requests must not accept a client-provided tenant ID as proof of authority.

### 21.5 Data and storage requirements

- Add a tenant-scoped settings record or extend the existing tenant-settings model with `user_photo_policy`, constrained to `OPTIONAL` or `REQUIRED`.
- Keep the global permission catalogue separate from tenant-specific configuration.
- Store photo metadata/reference separately from the user's other profile fields where consistent with existing conventions. Suggested metadata includes storage key/reference, content type, file size, upload method (`CAMERA_CAPTURE` or `FILE_UPLOAD`), uploader, tenant ID, and timestamps.
- Store image bytes in private object/file storage appropriate to the existing deployment, rather than placing large binary files directly in ordinary relational rows unless an existing architecture explicitly requires that design.
- Serve photos through an authorized backend endpoint or short-lived, access-controlled URL. Do not make identity photos publicly readable by default.
- Enforce tenant ownership when uploading, replacing, reading, or deleting a photo. A photo reference alone is not authorization.
- Apply configurable file-size limits and an explicit image-format allowlist. Verify the actual file content rather than trusting the filename or browser-provided MIME type; reject malformed files and unexpected formats.
- Use generated storage names rather than user-supplied filenames. Apply malware/content scanning where supported by the deployment, and strip unnecessary image metadata where appropriate.
- Do not log image bytes, credentials, or unnecessary sensitive personal data. Audit policy changes and security-relevant photo operations without copying the image itself into audit logs.
- Obtain appropriate notice/consent for camera use and explain the photo's purpose. Camera permission denial must not be treated as a successful capture; for agent creation, the administrator may use upload instead.
- Photo capture is not identity verification or liveness detection. Do not claim that a captured/uploaded photo proves the person's identity unless a separate verified process is implemented.

### 21.6 Authorization and validation sequence

For both creation endpoints, the server must:

1. Resolve the tenant from the trusted request/domain context.
2. Authenticate the caller and verify active tenant membership, where applicable.
3. Check the required creation permission and relevant scope/delegation policy.
4. Load the current photo policy from authoritative tenant settings.
5. Require a photo for agent creation; for user/member creation, require one only when the current policy is `REQUIRED`.
6. Validate the file or authorized upload reference, tenant ownership, content type, size, and storage completion.
7. Validate the remaining business fields and any existing approval/state rules.
8. Persist the user/appointment and photo reference consistently. If persistence fails, avoid leaving an unreferenced uploaded object indefinitely; use cleanup/retry handling.
9. Record required audit information. Agent appointment submission remains `PENDING` until the existing approval process completes.

A missing required photo should return the project's standard `400` or `422` validation response with a field-level message. An unauthorized policy change should return `403`. Invalid or cross-tenant upload references must be rejected. The exact error envelope should follow the existing API convention.

### 21.7 Frontend behavior and accessibility

- Show two clear actions: **Capture Photo** and **Upload Photo**.
- Display a preview and provide **Retake**, **Replace**, and **Remove** actions as allowed by the current policy.
- Clearly mark the agent photo as required. Mark the user/member photo as required or optional according to the latest policy returned by the backend.
- Explain why a photo is required when the tenant policy makes it mandatory.
- Support camera permission denial and devices without cameras by retaining the upload path.
- Do not submit an unconfirmed camera preview as if it were a captured photo.
- Provide accessible labels and visible validation messages; do not rely on color alone to indicate errors.
- Keep the UI consistent with the existing frontend stack and make all policy and photo operations go through the backend API. Frontend routes, components, and utilities must not connect directly to PostgreSQL or any other database.

### 21.8 Required tests

- Agent creation without a photo is rejected.
- Agent creation succeeds with a valid captured photo or valid uploaded image, subject to all existing permissions and workflow rules.
- User/member creation without a photo succeeds when the policy is `OPTIONAL`.
- User/member creation without a photo fails when the policy is `REQUIRED`.
- User/member creation with either valid photo method succeeds when policy permits it.
- Super Admin can change the policy for an authorized tenant; non-Super Admin callers cannot change it by default.
- A policy change for one tenant does not affect another tenant.
- The backend enforces the current policy even if the frontend is stale or bypassed.
- Camera denial/unavailability leaves the upload option usable.
- Oversized, malformed, disallowed-format, incomplete, or cross-tenant uploads are rejected.
- Unauthorized users cannot view or replace photos by guessing identifiers or storage keys.
- Failed creation does not leave unmanaged photo objects indefinitely.
- Audit history records policy changes and required security-relevant operations without storing photo bytes or secrets.
- Existing agent approval, tenant-isolation, and role/region tests continue to pass.

### 21.9 Acceptance criteria

- [ ] Agent creation offers live capture and image upload, and requires one valid photo.
- [ ] User/member creation offers live capture and image upload.
- [ ] User/member photo is optional by default.
- [ ] Super Admin can switch the user/member photo policy between optional and required per community.
- [ ] The backend enforces the policy regardless of frontend behavior.
- [ ] Photo access and storage are tenant-isolated and protected.
- [ ] The change does not bypass agent approval or grant additional permissions.
- [ ] API documentation, migrations/settings, audit behavior, and automated tests are updated.

------------------------------------------------------------------------
