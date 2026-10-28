# Community OTT Agent Platform API

This directory contains the independent FastAPI backend for a shared, tenant-aware modular monolith. The authoritative design is [the Community OTT Agent Platform architecture](../architecture/Community_OTT_Agent_Platform_Architecture.md) in the current workspace.

## Requirements and local setup

- Python 3.11 or newer (Python 3.12 is supported).
- PostgreSQL for registry and future application data. Persistence uses SQLAlchemy 2.x and Psycopg 3.
- The liveness endpoints and database-independent tenant tests do not require PostgreSQL.

From `backend/`, create and activate a virtual environment, then install requirements.

Windows PowerShell:

```powershell
python -m venv .venv
Copy-Item .env.example .env
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux/macOS:

```bash
python3 -m venv .venv
cp .env.example .env
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Edit the ignored local `.env` file. `DATABASE_URL` must use `postgresql+psycopg://...`. The checked-in example uses a placeholder password and is not a working credential. Production/staging require a database URL and explicit HTTPS `FRONTEND_ORIGINS`.

Start the development server from `backend\` with `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload`. Binding loopback is required for the local-only static OTP deployment. The platform health checks remain outside tenant resolution at `/health` and `/api/v1/health`; both return `{"status":"ok"}` with an `X-Request-ID`. Swagger UI is at `/docs` in development.

## Tenant resolution flow

Tenant-scoped modules must register endpoints on a router created by `create_tenant_router()` from `app.api.tenant_router`. That router applies `get_tenant_context` as a mandatory dependency. A handler that needs the resolved values can also declare `tenant: Annotated[TenantContext, Depends(get_tenant_context)]`; FastAPI request-scope dependency caching avoids repeating the registry lookup.

The resolution flow is:

1. Read exactly one `Host` header. `X-Forwarded-Host` is considered only when the actual ASGI peer address matches a configured trusted proxy CIDR.
2. Normalize the hostname to lower-case IDNA ASCII, remove a valid port and one trailing DNS dot, and reject malformed authorities and IP addresses.
3. Look up the hostname in `tenant_domains` joined to `tenants`.
4. Return an immutable `TenantContext` only when the domain is active, its verification status is `verified`, and its tenant is active.
5. Otherwise return the same non-sensitive `404 TENANT_NOT_RESOLVED` response for unknown, pending, revoked, inactive-domain, inactive-tenant, and malformed-host cases.

`TenantContext` is an immutable dataclass returned through FastAPI dependency injection. It is not stored in mutable global state or populated from request payloads. A body/query/header `tenant_id` cannot override the context. The hostname is a routing input, not user authentication or authorization.

Platform-level routes such as health checks do not use the tenant router and remain available without a registry record. All future tenant-owned routes must use the tenant router and scope database/service operations by its resolved context.

## Proxy trust and domain verification

`TRUSTED_PROXY_CIDRS` is empty by default, so forwarding headers never affect tenant selection. Configure only the exact proxy IPs/CIDRs for infrastructure that strips client-supplied forwarding headers and sets its own `X-Forwarded-Host`. This implementation does not use the standardized `Forwarded` header. Keep the backend inaccessible around any proxy trust boundary that could permit direct clients to impersonate a trusted proxy.

New tenants default to `inactive`; their slug is stored in lowercase. New domain rows default to `verification_status='pending'` and `is_active=false`. Store only the normalized output of `normalize_hostname()` in the registry. The Phase 2 onboarding API verifies DNS proof without activating a tenant; Phase 3 adds independent review and an audit-acknowledged activation intent. Production activation remains blocked until OIDC operator resolution/grants, a supported external audit sink and verified receipt, worker heartbeat, and PostgreSQL concurrency validation are available. No Community Admin or membership is provisioned by review or activation.

## Database and migrations

The source Alembic chain is linear through `20261013_09`: `20261011_01` creates the tenant/domain registry; `20261012_02` creates separate users and tenant memberships with mobile uniqueness; `20261012_03` adds OTP challenges, shared rate buckets, and opaque tenant-bound sessions; `20261012_04` adds tenant RBAC; `20261012_05` adds platform identity and audit/outbox; `20261012_06` adds onboarding/domain-proof history; `20261012_07` adds independent review and activation-attempt history; `20261012_08` binds completion audit receipt to tenant-resolution eligibility; `20261013_09` adds persisted platform OIDC login transactions and operator sessions. Migrations are never run automatically at startup. The applied database revision is unknown because no database connection was made.

Review the generated migrations before applying them. From `backend/`, configure `DATABASE_URL`, then run `python -m alembic upgrade head` against the intended non-production environment. To inspect SQL without connecting to a database, run `python -m alembic upgrade head --sql` with a syntactically valid PostgreSQL `DATABASE_URL`. Do not use production data for development tests.

No live PostgreSQL migration or connection validation is claimed unless separately run with a dedicated test database. The normal tenant-resolution tests use isolated in-memory data; the schema uniqueness test uses an isolated in-memory SQLite database to exercise the SQLAlchemy constraints without production infrastructure.

## Tests

From `backend/`, run `python -m pytest -q`. Tenant tests cover resolution, normalization, unknown/unverified/inactive records, malformed hosts, untrusted/trusted proxy behavior, client tenant-ID spoofing, context isolation across concurrent requests, health routes, request IDs, and database-enforced hostname uniqueness. Identity tests use isolated SQLite to check per-tenant mobile uniqueness, reuse across tenants, one-community-per-identity, and tenant-scoped active membership lookup. These tests do not skip when PostgreSQL is unavailable.

The separate PostgreSQL connectivity integration test runs only when `TEST_DATABASE_URL` points to a dedicated test database; otherwise it reports one explicit skip. It executes `SELECT 1` and does not modify records. Do not point it at production.

## Community authentication and permission-registry endpoints

Authentication and permission-registry endpoints below are mounted on the tenant router. FastAPI first resolves the request host through the active, verified tenant-domain registry. Request bodies reject extra fields, and no tenant ID from a client is used. Platform health routes remain public. The shared authorization service and schema are implemented; role administration and Agent Platform business workflows are not.

| Method and path | Request | Success |
| --- | --- | --- |
| `POST /api/v1/auth/otp/request` | `{"mobile_number":"+14155550123"}` | `202 {"status":"accepted","challenge_id":"<uuid>"}` |
| `POST /api/v1/auth/otp/verify` | `{"mobile_number":"+14155550123","otp":"…","challenge_id":"<uuid>"}` | `200` identity JSON and an HttpOnly session cookie |
| `GET /api/v1/auth/me` | Session cookie | `200` current identity and its CSRF token |
| `GET /api/v1/auth/context` | Session cookie | `200` trusted tenant, effective permissions, and explicit unavailable employee/product-role authority |
| `POST /api/v1/auth/logout` | Session cookie plus `X-CSRF-Token` | `204`, revokes the server-side session and clears the cookie |
| `GET /api/v1/permissions` | Session cookie and active `permission.read` grant | `200 {"permissions":[{"code":"...","description":"..."}]}` |
| `GET /api/v1/audit-events?limit=50&offset=0` | Session cookie and active `audit.read` grant | `200 {"events":[...]}` containing a bounded page of tenant-owned audit summaries |

Request-OTP always returns the same accepted response shape for eligible and unknown mobile numbers, and for suppressed delivery caused by rate limits/provider unavailability. It sends no OTP and creates no challenge for an unknown, inactive, or cross-tenant membership. Verify failures use a generic `401`. An OTP delivery response alone never creates an identity, activates a membership, or issues a session. Accounts and active memberships must be provisioned separately; this phase does not create them.

Community identity lookup is by `(server-resolved tenant_id, normalized E.164 mobile)`. The `users` and `tenant_memberships` records remain separate, platform administrators remain outside this schema, and no role or account-type column is added to either identity table. Tenant roles and assignments live in separate RBAC tables. Every session lookup rechecks that its resolved tenant, user, and membership are active. The database composite foreign key binds a session to the exact tenant membership. Moving the same cookie to another community host yields an unauthenticated response.

### Session and browser protections

The selected minimal session is a cryptographically random, opaque 256-bit bearer credential in an HttpOnly cookie. Only its SHA-256 digest is stored in `auth_sessions`; the raw credential is not returned in JSON or logged. The default lifetime is 12 hours (`AUTH_SESSION_TTL_SECONDS`). Every protected request checks expiry, revocation, tenant, user, and membership state against PostgreSQL. Logout sets `revoked_at`, so revocation is enforced server-side. There is no refresh token or recovery flow.

The browser-facing cookie uses `Path=/backend-api/api/v1/auth`, `HttpOnly`, `SameSite=Lax` by default, and `Secure` outside local development/tests. The path is validated against the explicit Next.js auth proxy path. Configure `AUTH_SESSION_COOKIE_SAMESITE=none` only for a genuinely cross-site frontend and HTTPS; settings require `Secure` with `None`. The authenticated response contains a random CSRF token bound to the session row. The frontend must send it as `X-CSRF-Token` on logout. The backend requires that header and exact configured CORS origins; never put the session cookie in JavaScript storage. Configure HTTPS at the edge and keep credentials out of URLs.

### Local development static OTP

An optional static-code verifier is available only when `DEVELOPMENT_STATIC_OTP_ENABLED=true`, `ENVIRONMENT=development`, a private `DEVELOPMENT_STATIC_OTP_CODE` of 6-12 digits and an explicit `OTP_CHALLENGE_TTL_SECONDS` are configured. Every configured frontend origin must be loopback (including `*.localhost`), and the request peer must also be loopback with an exact allowed `Origin`. The Next.js auth proxy is enabled only in its development runtime and only for loopback browser hosts. Use a tenant hostname already registered in the backend domain registry and ending in `.localhost` (for example, `http://<registered-tenant>.localhost:3000`). The proxy forwards only that browser host as `X-Forwarded-Host`; add only the actual local Next.js proxy address to `TRUSTED_PROXY_CIDRS` so FastAPI may resolve that host through the verified tenant-domain registry. Do not add or activate a tenant/domain just for this workflow.

Local startup (two PowerShell terminals; run only after configuring an existing isolated development database and approved membership/tenant records):

```powershell
# Terminal 1, from backend\
Set-Location D:\AVANFlix\OTT_Agent_App\backend
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2, from frontend\
Set-Location D:\AVANFlix\OTT_Agent_App\frontend
npm run dev -- --hostname 127.0.0.1 --port 3001
```

For the command above, the required backend settings have these safe forms (do not commit private credentials or a development OTP code):

```dotenv
ENVIRONMENT=development
DATABASE_URL=postgresql+psycopg://<local-user>:<private-password>@127.0.0.1:5432/<isolated-development-db>
FRONTEND_ORIGINS=http://<registered-tenant>.localhost:3001
TRUSTED_PROXY_CIDRS=127.0.0.1/32
OTP_CHALLENGE_TTL_SECONDS=300
DEVELOPMENT_STATIC_OTP_ENABLED=true
DEVELOPMENT_STATIC_OTP_CODE=<private-6-to-12-digit-code>
AUTH_SESSION_COOKIE_PATH=/backend-api/api/v1/auth
AUTH_SESSION_COOKIE_SECURE=false
```

The database URL must point to an already-migrated isolated development database containing the owner-approved existing tenant, verified hostname mapping, and active membership; the credentials are private. Put the development OTP code only in the ignored backend `.env`. The frontend `.env.local` must set `BACKEND_API_BASE_URL=http://127.0.0.1:8000`. Open the browser at the exact origin in `FRONTEND_ORIGINS`; its hostname must already be registered and verified in that database. Do not use production/staging data or credentials, and do not create tenant or membership records just to enable the flow. Static OTP stays disabled by default and is rejected outside `ENVIRONMENT=development`.

The static code is not returned by the API and does not create or select an account. The entered mobile must still resolve to an existing active membership in the backend-resolved tenant; the normal abuse limits, persisted challenge expiry, one-time consumption, transactional membership recheck, session issuance, CSRF, and logout revocation continue to apply. This mode can impersonate any active member whose mobile is known to a person with local access and the configured code. Use only on an isolated local development machine with non-production test identities. Never enable it in staging or production; configuration validation rejects those environments and rejects non-loopback frontend origins. No identity, membership, grant, tenant, or package is seeded by this feature.

The frontend proxy allowlists only OTP request/verify, `/me`, `/context`, and logout in local Next development. It forwards only the FastAPI community-session cookie on the two GETs and logout, preserves browser `Origin`/CSRF for logout, forwards only the expected HttpOnly session cookie with the exact browser path, and keeps all other FastAPI paths unavailable. The current context deliberately reports product-role and employee authority as unavailable; it does not enable Admin, Agent, or Super Admin navigation.

`GET /api/v1/permissions` is backend-only and is not exposed through the frontend proxy. It returns the sorted read-only registry from `PermissionCode` and its description mapping, guarded by trusted tenant resolution, the server-managed session, active membership validation, and the shared `permission.read` dependency. Successful catalog responses are non-cacheable. Catalog visibility does not grant permissions.

`GET /api/v1/roles?limit=50&offset=0` is backend-only and is not exposed through the frontend proxy. It requires the same trusted tenant/session/membership chain plus `role.read`, returns only role ID/code/display name/status and active registered permission codes for roles owned by the resolved tenant, and bounds pagination to 1–100 rows and offset 0–10,000. Inactive tenant roles are visible for inventory; inactive or revoked permission links are omitted. Responses are `no-store`. It does not list memberships or imply that a role assignment makes someone an employee or active Agent. Role creation, role/permission mutation, role assignment/revocation, employee provisioning, and Agent appointment APIs remain unavailable.

`GET /api/v1/memberships?limit=50&offset=0` is backend-only and is not exposed through the frontend proxy. It requires an active authenticated tenant membership and the existing registered `member.read` permission. Results are constrained by the trusted tenant context, ordered by membership creation time, and paginated with `limit` 1–100 and `offset` 0–10,000. The `no-store` response includes only membership/user UUIDs, membership and user statuses, and membership timestamps. It omits mobile numbers and does not expose login/session or identity-provider data. Membership inventory does not create identities or establish employee or Agent status. Database failures return `503 MEMBERSHIP_STORAGE_UNAVAILABLE`.

`GET /api/v1/audit-events` is also backend-only. It requires `audit.read`, filters by the resolved tenant ID in the audit repository, sorts newest-first, limits pages to 1–100 events, and caps offset at 10,000. Its response is `no-store` and contains only event ID/type, timestamps, actor category, action, outcome, and target entity type; it omits actor/target references and decision metadata. Audit storage failures return a generic `503`. This is a tenant-wide read permission; region/relationship scope and retention policy are not implemented.

### OTP abuse controls

`otp_rate_buckets` stores shared PostgreSQL counters keyed by tenant, normalized mobile, and operation. A single database upsert performs each check-and-consume atomically across application workers. Defaults are three sends per ten minutes with a 60-second resend cooldown, and five verification attempts per fifteen minutes. They are configurable with `OTP_SEND_*` and `OTP_VERIFY_*`; they are application abuse limits, not a replacement for the provider's OTP verification/expiry controls. They are not process-local counters. If the shared database/abuse store is unavailable, no SMS or session is issued. The public send operation keeps its generic accepted response and emits only a safe operational warning.

The current limiter is per tenant/mobile. It does not implement an IP/network quota; configure edge/gateway request limits as an additional deployment control. No account lockout, device fingerprint, or distributed denial-of-service protection is claimed.

`otp_challenges` stores one current internal challenge ID per tenant/mobile, plus expiry and consumption state. A resend replaces the row ID and invalidates any earlier challenge. A successful provider verification must atomically consume the challenge and create at most one session; a repeated or expired challenge fails. `OTP_CHALLENGE_TTL_SECONDS` has no production default: set it only after confirming the external service's OTP TTL and correlation behavior. The API service contract is not yet available, and the current messaging codec does not carry a provider challenge reference. Therefore the local challenge provides replay protection inside this backend, but does not prove that the external provider's own OTP is single-use or that its TTL matches. Live authentication remains blocked until those semantics are confirmed and mapped in the codec.

Expired/revoked session rows older than 30 days are pruned when that same member next authenticates. A periodic cleanup job for dormant identities is not part of this phase; database retention should be reviewed before high-volume rollout.

The `OtpRateBucket`, challenge, and session repositories use the shared configured PostgreSQL database. Missing database configuration or failed persistence fails closed. Database concurrency was exercised with isolated SQLite tests here; production-dialect concurrency still requires a dedicated PostgreSQL test instance.

Missing, invalid, expired, revoked, cross-tenant, or disabled-user sessions receive a generic `401`. A valid session whose community membership is no longer active receives `403 TENANT_MEMBERSHIP_REQUIRED`; neither response reveals account data.

## Phase 4 account-flow decisions and boundaries

**Confirmed product direction (2026-10-12):**

- Platform operators: an external OIDC identity provider with an explicit MFA/authentication-strength policy; platform operators remain separate from community memberships and roles. No provider or login flow has been selected or configured.
- Community Admin, Agent/Employee, and Community Member: mobile number plus OTP.
- Mobile numbers are normalized to E.164. The community identity lookup is tenant-scoped, so a number may be used in another community without resolving to the first community's membership.
- The identity and tenant-membership concepts remain separate. The community identity and membership schema is described below; platform operator authentication and permissions use separate platform-owned tables and an external identity boundary.

The backend now has separate `users` and `tenant_memberships` records. Under the confirmed per-community identity policy, a user can belong to at most one community; the membership owns the normalized mobile identifier, and the database enforces unique `(tenant_id, mobile_e164)` while allowing the same number in a different tenant. Both account and membership statuses default to inactive. This schema intentionally has no account-type or role column. Revision `20261012_02` creates these tables after the tenant-registry migration; it has not been applied to a database. The SQLAlchemy repository always looks up by both the server-resolved tenant ID and normalized mobile, and returns only an active user with an active membership.

`OtpAuthenticationService` is the public community-authentication application layer. The older `OtpLoginCoordinator` remains only for its existing protocol-level unit tests and is not registered on any route; it predates challenge replay controls and must not be wired into production. Platform operators have a provider-neutral OIDC token-validation boundary and separate identity/permission tables, but no authorization-code login, browser session, operator bootstrap, or role-assignment workflow is implemented. Account provisioning and recovery remain out of scope. The authoritative architecture describes a global user model conditionally; this migration applies the product's per-community identity decision without adding cross-community identity sharing.

The existing legacy frontend auth patterns remain unsafe backend contracts to reuse: admin login compares a submitted password directly with a MongoDB field; employee OTP is generated with `Math.random`, stored as plaintext, returned in the API response, and has no delivery provider; the `ep_session` cookie is unsigned JSON with a seven-day lifetime, and legacy logout only deletes that browser cookie. The login page now offers the bounded community OTP flow only in local Next development, but legacy routes remain in the repository and are not made safe by this integration. `ep_session` is never accepted by FastAPI. Production role-based application access remains blocked.

## Pluggable SMS/WhatsApp messaging

`MessagingService` exposes internal `send_otp`, `verify_otp`, and `send_whatsapp` operations. `MessagingProvider` is the adapter boundary. `HttpMessagingProvider` supplies configurable HTTP transport, timeouts, endpoint methods/paths, and an optional credential header. `MessagingContractCodec` is the single seam for external serialization and response interpretation. It intentionally has no production implementation because the separate service that fronts MSG91 integrations is still being developed. Until a codec is supplied, HTTP messaging operations fail clearly with a contract-not-configured error; a 2xx response alone can never count as OTP verification success.

Configuration is in `.env.example`. A local development setup leaves the unfinished service fields empty:

```dotenv
MESSAGING_PROVIDER=http
MESSAGING_API_BASE_URL=
MESSAGING_SEND_OTP_PATH=
MESSAGING_SEND_OTP_METHOD=
MESSAGING_VERIFY_OTP_PATH=
MESSAGING_VERIFY_OTP_METHOD=
MESSAGING_SEND_WHATSAPP_PATH=
MESSAGING_SEND_WHATSAPP_METHOD=
MESSAGING_API_KEY=
MESSAGING_AUTH_HEADER_NAME=
MESSAGING_AUTH_VALUE_PREFIX=
MESSAGING_CONNECT_TIMEOUT_SECONDS=3
MESSAGING_READ_TIMEOUT_SECONDS=10
OTP_CHALLENGE_TTL_SECONDS= # required in production after matching provider semantics
OTP_SEND_COOLDOWN_SECONDS=60
OTP_SEND_WINDOW_SECONDS=600
OTP_SEND_LIMIT=3
OTP_VERIFY_WINDOW_SECONDS=900
OTP_VERIFY_LIMIT=5
AUTH_SESSION_TTL_SECONDS=43200
AUTH_SESSION_COOKIE_NAME=community_session
AUTH_SESSION_COOKIE_SECURE= # unset enables Secure outside development/tests
AUTH_SESSION_COOKIE_SAMESITE=lax
```

In production/staging, the API base URL, HTTPS, OTP endpoint paths and HTTP methods are required at settings validation. Credential name/value are optional until the service specifies its authentication scheme; if configured, both the API key and header name are required. The base URL, endpoint paths, methods, auth header name/value prefix, connect timeout, and read timeout stay server-side. No credentials are sent to the frontend. Mobile input requires a country-coded E.164 value; no default country is inferred.

The test-only `MockMessagingProvider` requires `ENVIRONMENT=test`, accepts only explicitly registered test codes, and never returns an OTP from its send operation. Settings reject the mock provider in development, staging, and production. Automated tests use an in-process mock HTTP transport and do not send real SMS or WhatsApp messages.

### Contract details needed from the messaging service

Before live MSG91 integration, obtain and review:

- The service base URL for each environment, endpoint paths, HTTP methods, and redirect/TLS expectations.
- The authentication scheme, header name, credential format/rotation rules, and whether multiple headers or token exchange are required.
- Exact request and response schemas for send OTP, verify OTP, and WhatsApp send, including content types, success/failure fields, error codes, and status behavior.
- Whether OTPs are generated and verified by the service, challenge/reference correlation, TTL, resend cooldown, verification-attempt limits, and idempotency behavior.
- WhatsApp destination/template/payload requirements, delivery callbacks, retry rules, and status semantics.

The public routes use PostgreSQL-backed atomic abuse control, membership validation, challenge persistence, and session issuance. The external service's provider challenge semantics are still unconfirmed, so live OTP login remains blocked even though mock-backed endpoint behavior is tested.

## Phase 4 status

**Implemented and tested now:** tenant-scoped OTP request/verify, current-identity, and logout endpoints; E.164 normalization; active user/membership/tenant checks; opaque server-side sessions with expiry and revocation; session-bound CSRF token; atomic challenge consumption; persistent PostgreSQL rate-limit design; test-only mock provider; concurrent replay/rate tests using isolated SQLite. Phase 5 adds centralized permission evaluation and tenant-scoped RBAC persistence, but does not add RBAC administration APIs or seed any role assignment.

**Waiting for the external API contract:** production request/response mapping, real provider credential semantics, and live service validation. No real MSG91 request has been sent.

**Waiting for PostgreSQL validation:** the new session/challenge/rate-control migration is revision `20261012_03`, following `20261012_02` and `20261011_01`. Offline SQL generation can be reviewed, but no dedicated `TEST_DATABASE_URL` has been configured, so no PostgreSQL connection, migration, or PostgreSQL concurrency validation is claimed. No production database was contacted or modified.

**Blocked on external contract:** no MSG91-facing live codec exists. Its service base URL, authentication scheme, request/response schemas, challenge correlation, one-time-use semantics, TTL, and provider-side attempt limits remain unconfirmed. Until then the HTTP adapter cannot validate a real OTP and a session cannot be issued through live delivery. Platform OIDC login/session, operator bootstrap, account recovery, RBAC administration endpoints, agent workflows, and business endpoints are not implemented. No frontend code was modified.

## Deferred work and limitations

- There is no tenant/domain administration API; trusted provisioning and domain-proof operations need an audited platform operations contract.
- Account provisioning, a platform OIDC login/session flow, operator bootstrap, recovery, RBAC administration endpoints, region scope, member hierarchy, agents, approvals, and tenant-owned business endpoints are not implemented. Active identities/memberships must be provisioned by a separate approved process.
- Future tenant-owned queries, background jobs, and events must carry and enforce trusted tenant context; merely resolving a domain is not sufficient authorization.
- Existing frontend MongoDB compatibility routes and data remain separate. No MongoDB migration, database connection, table creation, or data mutation was performed by this backend change.

## Phase 5: centralized tenant-aware RBAC foundation

The authorization flow is trusted hostname resolution → authenticated session → active user/membership/tenant revalidation → active tenant role assignments → registered permission check → optional composable resource/scope constraints. `require_permission(PermissionCode.AGENT_READ)` creates a FastAPI dependency for protected tenant routes. It consumes only `TenantContext` and the authenticated server-side session; body/query/header tenant IDs or role names do not contribute to an authorization decision. Authorization records are read from PostgreSQL on each request so permission removals take effect on the next decision; no authorization cache is enabled.

The catalog is platform-global and contains the initial operation codes in `app/modules/authorization/catalog.py`. The migration seed is tested against that catalog. Roles, role-permission links, and user-role assignments are tenant-scoped. Composite foreign keys bind assignments, including their required assigning membership, to the same tenant. Assignment evaluation requires active user, tenant and membership records; an active role; an active role-permission link; an active assignment whose validity window includes the current UTC time; and the exact permission code. Inactive, revoked, expired, unknown, or cross-tenant state fails closed. New roles, permission links, and assignments default inactive; no role, privileged account, or grant is seeded.

Delegation checks are centralized in `AuthorizationService`. A caller must hold `role.assign` to assign a role; every permission in the target role must be explicitly marked delegable on one of the caller's active role-permission links. Permission assignment also requires `role_permission.assign` and explicit delegability of the requested permission. The `is_delegable` flag defaults false. The role inventory endpoint is read-only; there are no role mutation routes or provisioning commands, so delegability can only become true through an explicitly reviewed trusted provisioning process. A future role/permission write must repeat these checks inside the same database transaction as the mutation and record an audit event.

Region, relationship, appointment, and resource-state restrictions remain distinct composable `AuthorizationConstraint` policies; their semantics and implementations are deferred until the related product policies and business modules exist. The sample permission registry includes the architecture's illustrative initial codes, including `approval.read`; this is a catalog only and grants no user authority. The architecture still requires product decisions for tenant delegation boundaries, protected roles, approval of high-risk security changes, region inheritance, and revocation service-level objectives. Until reviewed, there are no platform-managed tenant roles, role-admin endpoints, or default grants.

Revision `20261012_04` seeds the permission catalog and creates `permissions`, `roles`, `role_permissions`, and `user_role_assignments`. Review offline SQL with `python -m alembic upgrade head --sql`; apply only to a dedicated non-production database after review. Schema tests use isolated SQLite and do not establish PostgreSQL dialect, migration, or concurrency validation. No live PostgreSQL database or production data was accessed for this phase.

The audit-event list and tenant role inventory are tenant-owned read endpoints. Agent/member/region workflows and role mutations remain unimplemented. Health remains public; authentication endpoints continue to require a resolved tenant and session only where appropriate. Every future tenant-owned business route must add `Depends(require_permission(...))` and any applicable server-side region/resource policies before handler execution. OTP verification never creates or assigns roles.

## Phase 6: agent and region management review

**Status: implementation held for policy review.** This review added no agent or region models, routes, migrations, permission grants, or data. The source migration head is now `20261012_05`; no migration was applied, and the applied database revision remains unknown.

The authoritative architecture treats an agent as more than a tenant membership or profile. Agent-only operations require an active identity and membership, an active role assignment, an approved and active appointment/assignment where applicable, and an active region assignment. It explicitly requires an appointment to be reviewed before the corresponding assignment becomes effective. Phase 6 excludes implementing that approval workflow, so creating or activating an agent profile here would either duplicate the appointment/assignment lifecycle or risk granting agent status without its required review. The `agent.*` permission codes already present in the catalog are operation identifiers only; no roles or grants are seeded and there are no agent endpoints.

The architecture suggests tenant-owned canonical regions with stable IDs and parent references, plus separate membership/region assignments. It does not define the authoritative geographic source or taxonomy, whether one agent may have multiple active region assignments, or the lifecycle and reassignment policy for those assignments. It says parent-region access must not be inherited unless a policy explicitly permits it. No region model or API currently exists, and the authorization service has no region-scope evaluator.

### Decisions required before agent and region schema/API work

1. Confirm the authoritative agent record: an approved appointment/operational assignment only (as the architecture suggests), or a separate non-authoritative profile before appointment. If a profile exists, define its allowed fields and lifecycle states and state explicitly that it conveys no permission or operational access.
2. Define who may submit, review, activate, suspend, revoke, and reassign an agent, including whether self-review is prohibited and what happens when tenant membership becomes inactive. Map each operation to the existing permission catalog and any required scope checks.
3. Confirm whether an active agent can hold multiple active region assignments at once. Define whether assignments attach to a membership or to an approved agent assignment, and whether appointment approval activates the exact region assignment atomically.
4. Identify the canonical region source and supported region types/codes. Define parent-child creation and reparenting rules, including cycle prevention and whether region codes are immutable or source-managed.
5. Confirm the safe default that region hierarchy grants no inherited access. For any exception, enumerate the operations that inherit scope and the exact descendant rules; until then, all scope checks must use explicit active assignments only.
6. Define whether region deactivation is allowed while active child regions or assignments exist, and whether deactivation suspends, preserves, or rejects those relationships.

The architecture already establishes these safety rules: identity, membership, roles, agent appointment/assignment, and region assignment stay separate; membership and agent/assignment states are checked independently; region assignment does not grant a permission; role assignment does not expand geographic scope; pending appointments and pending region assignments grant no access; and ambiguous or inherited access fails closed. The next implementation review should resolve the decisions above before adding a migration or protected write endpoints. No Phase 7 work has started.

## Phase 6 policy audit update

**Stage 1 audit complete. Schema gate: blocked.** The approved Phase 6 policy below supersedes conflicting open items in the earlier Phase 6 note. No Phase 6 schema, endpoint, permission grant, migration, or data was added.

### Policy audit

| Topic | Audit result |
| --- | --- |
| Agent profile cardinality | Target one profile per tenant membership, to be enforced by a unique `(tenant_id, membership_id)` key. A profile may exist before assignment approval and is never an authorization grant. The minimum profile fields should be identifiers, a non-authoritative profile status, and timestamps; do not duplicate the membership mobile number or invent personal profile fields. |
| Assignment lifecycle | Keep decision and operational state separate. Decision states follow the architecture: pending, approved, rejected, cancelled, expired. Only approval may atomically produce an active operational assignment. Active assignments may be suspended, revoked, or expired. Pending, rejected, cancelled, expired, suspended, and revoked records grant no agent access. Reactivation of a suspended assignment remains undecided; revoked assignments should be terminal by default. |
| Reviewer | Require an active authenticated membership in the resolved tenant, the applicable registered decision permission (`agent.approve` or `agent.reject`), Community Admin authority, and a reviewer distinct from the submitter. No eligible reviewer means the request stays pending. The backend has no role-name policy; Community Admin authority must be represented through trusted permission assignment, not a request field or a string comparison. |
| Assignment permissions | Existing codes cover submission (`agent.create`), profile read/update (`agent.read`, `agent.update`), review queue (`approval.read`), decisions (`agent.approve`, `agent.reject`), suspension (`agent.suspend`), and revocation (`agent.revoke`). `agent.create` is currently documented as appointment submission, not separate profile creation. A dedicated `agent.profile.create` code is proposed unless the product explicitly approves reusing `agent.create`. No dedicated reactivation code exists; proposed `agent.reactivate` is needed only if suspended assignments can be restored without a new appointment. |
| Region permissions | Existing codes cover create/read/update (`region.create`, `region.read`, `region.update`), management (`region.manage`), and assignment (`region.assign`). `region.assign` is currently described as membership-to-region assignment; its meaning must be updated or a dedicated permission proposed for assignment-to-approved-agent-assignment. The catalog has no distinct deactivation or unassignment code. Recommended mapping is `region.update` for soft deactivation and `region.assign` for assignment add/remove, subject to explicit product confirmation. |
| Community Admin provisioning | **Not established.** Tables exist for tenant roles, role-permission links, and membership role assignments, but there are no role/grant APIs or provisioning commands, no seeded roles/grants, and no documented trusted operations procedure. Test fixtures insert records directly and are not a provisioning mechanism. |
| Region model | The new policy says no existing feature needs hierarchy; use a flat tenant-owned region catalog and disable inheritance entirely. The approved policy allows multiple active regions per approved agent assignment, and region assignments attach to that assignment, not to an identity or membership. Region assignment constrains access but grants no permission. |
| Region deactivation | Product behavior is not specified. Recommended safe behavior: soft-deactivate; reject deactivation while active assignments reference the region; require explicit unassignment/reassignment first; preserve historical rows. An inactive region must never satisfy an access check. Confirm this before implementing lifecycle endpoints. |
| Reassignment/reactivation | Add/remove region assignments only for an approved, active assignment and preserve assignment history; do not mutate the approved role or create cross-tenant links. Recommended default: a revoked agent assignment is terminal and requires a new appointment; suspended-assignment reactivation needs an explicit permission and review rule. Confirm before implementing transitions. |
| Migration shape | If the gate passes, the next revision depends on `20261012_04`. Tenant-composite foreign keys should bind profiles to memberships, submitters/reviewers to memberships, and region assignments to both same-tenant regions and approved assignments. Unique profile-per-membership and partial uniqueness for duplicate active `(tenant, assignment, region)` links are required. Decision transitions need a transaction/conditional update or row lock so concurrent reviewers cannot both decide. No migration has been generated or applied. |

### Explicit decision required to pass the schema gate

Approve and specify the trusted, audited mechanism that creates the first authorized Community Admin membership/role grant for a tenant and provisions the Phase 6 permission set. The specification must name the responsible operator or system, how tenant and membership ownership are verified, which protected role template may be assigned, which exact permissions it contains, whether any grants are delegable, and how the change is audited and revoked. Existing code cannot perform this operation safely, and a direct database insert or a new self-service bootstrap endpoint is not an approved substitute.

**Recommended default:** keep this as a separately authenticated platform-operations process with tenant-scoped, least-privilege role templates, explicit permission allowlists, auditable independent review for the initial/high-risk grant, and no delegation by default. Do not let a community member self-create the first administrator or grant their own permissions. This recommendation is not implemented or treated as approved policy.

Before a migration, also confirm the permission-code semantics noted above (profile creation, region assignment/unassignment, and any suspended-assignment reactivation) and the region deactivation rule. The current permission catalog can express most operations, but it cannot make them usable without the provisioning decision. Until these decisions are recorded, authorization will correctly deny every Phase 6 operation and implementation must remain stopped before schema changes.

No Phase 6 tests were added or run because the required schema/authorization gate did not pass. The existing PostgreSQL integration status remains unchanged: a dedicated `TEST_DATABASE_URL` is not configured, so no live PostgreSQL migration or transaction validation is claimed.

## Community Admin provisioning policy review

**Gate result: still blocked before grants.** The approved provisioning rules establish that bootstrap is an explicit operational action, but they do not establish who or what is trusted to authorize and execute that action. Inspection found no `infrastructure/`, deployment workflow, operations runbook, database-operations process, or platform-operator identity model in this repository. The root contains `architecture/`, `backend/`, and `frontend/`. The existing README's tenant/domain provisioning paragraph describes an out-of-band process as future operational work; it does not identify an operator, authentication boundary, approval evidence, or audit system. I did not implement a command or write any grant.

### Existing trust and revocation mechanisms

- `User` and `TenantMembership` are community OTP identities. Memberships are tenant-bound. Revision `20261012_05` adds a separate external-subject `PlatformOperator` model; no OIDC login flow, operator rows, or platform grants are configured. Reusing a community session cannot establish platform-operator authority.
- `roles`, `role_permissions`, and `user_role_assignments` are tenant-scoped. Revision `20261012_04` seeds permission definitions only. Roles, grants, and privileged accounts are not seeded. Assignment and permission-link `is_delegable` defaults to false.
- `AuthorizationService` reloads active tenant membership, role assignments, and role-permission links for each permission evaluation; it has no authorization cache. Revoking an assignment therefore denies the next protected operation even if the caller's authentication session remains valid. The session can still authenticate until logout/expiry, but it cannot pass the next authorization check. This is sufficient for immediate permission revalidation; no role-specific session invalidation method exists.
- Phase 1 now adds a local audit/outbox source schema in revision `20261012_05`; whether it is applied to any database is unknown. Application logs are not a durable, transactional audit store, and the local audit table is not tamper-evident. No external sink adapter is configured, so the schema does not yet meet production provisioning guarantees.
- The RBAC tests create roles and grants as isolated fixtures. That proves authorization evaluation, not operator authorization or a production provisioning process.

### Permission inventory and proposed template

The current catalog (and revision `20261012_04` seed) is synchronized by `test_permission_catalog_and_migration_seed_match`. Current agent codes are `agent.create`, `agent.read`, `agent.update`, `agent.cancel`, `agent.approve`, `agent.reject`, `agent.suspend`, and `agent.revoke`. Current region codes are `region.create`, `region.read`, `region.update`, `region.manage`, and `region.assign`. `approval.read` exists for review queues. The current `region.assign` description is specifically `Assign a membership to a tenant region.`

This proposal supersedes the earlier Phase 6 permission mapping. Proposed immutable template identifier: `COMMUNITY_ADMIN:v1`. Proposed allowlist, to be reviewed and represented in version-controlled code before any grant:

- Existing: `agent.read`, `agent.update`, `agent.create`, `agent.cancel`, `agent.approve`, `agent.reject`, `agent.suspend`, `agent.revoke`, `approval.read`, `region.create`, `region.read`, and `region.update`.
- Proposed catalog additions: `agent.profile.create` for creating the non-authoritative profile, and `agent.region.assign` for adding/removing region assignments on an approved agent assignment. The current `agent.create` stays appointment submission. The existing `region.assign` retains its membership-to-region meaning and is excluded from this template.
- Exclude `role.*`, `role_permission.*`, and `permission.read` so this template cannot administer roles or permission grants. Also exclude `member.*`, `audit.read`, `notification.*`, generic `region.assign`, broad `region.manage`, and any platform authority. Do not add `agent.reactivate` unless suspended-assignment reactivation is separately approved; revoked assignments should require a new appointment by default.

This is a proposal, not a persisted role or grant. A versioned template should have a fixed permission allowlist and digest. A preexisting `COMMUNITY_ADMIN` role whose permissions do not exactly match the reviewed template should cause provisioning to fail as ambiguous; the command must not silently widen or repair it. The approved policy does not define an AGENT role's permission set or who may delegate it. Since the current authorization service requires explicit `role.assign` and delegable permissions for generic role assignment, the appointment workflow also needs a separately reviewed fixed AGENT role template and a narrowly scoped approval rule; it must not grant generic role-administration permission to Community Admins or accept an arbitrary role ID from the request.

### Audit and operator boundary required before implementation

Before a provisioning command can be safe, approve the actual operator trust source and where the command may run. The decision must identify:

1. The authenticated human or workload principal (for example, a named platform-operations identity from an approved external IAM/SSO system) and the control that restricts production command execution to it. A database URL or ability to open a shell is not by itself an auditable operator identity.
2. Whether a separate approver is required for initial Community Admin grants and later high-risk template changes; identify the approval evidence source and required immutable request/ticket identifier.
3. The production execution boundary (approved deployment/DB-operations job or equivalent), who holds its credentials, and emergency stop/revocation owner. No such workflow is present in this repository.
4. Audit retention duration, read access, and tamper protection. Architecture section 19 leaves retention unresolved; this repository has no audit store.

Required audit record proposal: operation ID; action and outcome; authenticated operator issuer/subject; separate approver issuer/subject and approval reference when applicable; tenant, membership, role, and template version/digest; exact permission codes granted or revoked; UTC timestamp; request/correlation ID; dry-run/live indicator; and safe failure code. Store no OTPs, passwords, session cookies/tokens, API keys, or database credentials. If an in-database audit intent/outbox is approved, it must commit transactionally with the grant/revocation and roll back if that write fails; that local write alone does not satisfy the independent audit-sink requirement. The external sink's durable acknowledgment and recovery protocol must be approved separately. Preserve append-only history and export to a tamper-resistant operational log if that is the approved audit-retention design.

### Decision required

Specify the trusted production operator principal/IAM source and approved execution workflow, the required independent approval evidence, and the audit retention/tamper-protection owner. The approved requirements do not identify these boundaries, and no compatible mechanism exists in the inspected repository. **Recommended default:** a dedicated platform-operations job authenticated with external IAM, production execution limited to that job, independent approval for first-admin/high-risk grants, and transactional append-only audit records exported to the organization's immutable log store. Do not use a public endpoint, local untracked script, community OTP session, direct unaudited SQL, or startup hook as a substitute.

Until that operational decision is documented and approved, no versioned grant command, migration, or permission assignment should be implemented or run. No Phase 6 agent/region schemas or later-phase work have started.

### Proposed trusted bootstrap execution contract (documentation only; not enabled)

This section records the proposed control flow for a future implementation. It does not assert that any external system is configured, and it does not authorize provisioning. Repository inspection found no deployment/cloud IAM configuration, MFA policy, protected CI/CD approval environment, operations runbook, secret-manager integration, or independent append-only audit sink. No vendor, URL, credential, or endpoint is established here. The gate remains **blocked** until the controls below exist and can be verified.

#### Required execution workflow

1. A requester authenticated by the organization's selected external deployment/cloud IAM submits a request for one exact existing tenant and one exact existing membership. The IAM must provide a stable issuer/subject identity and verifiable MFA evidence. The requester identity must not be inferred from a shell username, a community account, a database login, or a caller-supplied string.
2. An independent approver authenticates through the same or an explicitly federated approved IAM. The approval system must enforce requester/approver distinctness and create an immutable approval record with a unique request ID. Approval binds the exact tenant ID, membership ID, `COMMUNITY_ADMIN:v1` identifier and reviewed template digest. It must be single-use, time-bounded, and invalidated if any bound value or template digest changes.
3. A restricted, protected deployment/operations job verifies the requester and approver identity claims, MFA evidence, immutable approval record, request expiry/use status, target tenant and active membership relationship, and exact versioned permission allowlist. It obtains short-lived, least-privilege database credentials through the organization's selected secret manager or workload-identity database authentication. A developer shell, public application endpoint, community-admin session, generic direct database access, or startup hook is not an execution path.
4. The job supports a non-mutating dry run and requires an explicit live-execution confirmation. It rejects missing, malformed, expired, reused, ambiguous, cross-tenant, or mismatched requests. The permission manifest is version controlled and immutable per version; `COMMUNITY_ADMIN:v1` must resolve to one reviewed digest and exact allowlist, without implicit role repair or permission widening.
5. Before enabling the job, the platform/security owners must approve how the independent audit sink records the request, decision, result, and any partial outcome. Grant execution fails closed when identity, approval, target validation, database access, or required audit durability cannot be verified. The job reports only an opaque operation/request reference and safe outcome code; it must never print credentials, OTPs, session material, or tokens.

#### Approval evidence and audit ownership

The immutable approval record must contain at minimum: request ID; requester and approver IAM issuer/subject; evidence that MFA was satisfied; proof the two identities differ; exact tenant and membership identifiers; template identifier, version, and cryptographic digest; requested action; approval timestamp and expiry; one-time-use state; and a change/ticket reference. Approval must be checked against its authoritative system at execution time; a JSON file, command-line flag, or caller-provided approval string alone is not evidence.

The independent append-only/tamper-evident audit sink must be controlled by an owner separate from the operator who executes grants. Each event should include operation/request ID, action and outcome, requester/approver/job identities, target tenant and membership, role/template version and digest, exact permission codes, UTC timestamps, dry-run/live status, correlation reference, and a non-sensitive failure code. It must exclude secrets, credentials, OTP values, and session tokens. The sink's product, authentication integration, write/read permissions, tamper protection, and durable-acknowledgment behavior are not selected or present in this repository.

The designated security/platform-operations owner must decide and document the retention duration, legal-hold and deletion rules, audit-reader access, and who reviews audit events. Architecture section 19 leaves retention unresolved. Until a named accountable owner approves a duration and the sink enforces it, provisioning remains disabled.

The database grant and an external audit write cannot be assumed to be one atomic transaction. Source revision `20261012_05` now writes local audit rows and outbox rows in one caller-owned PostgreSQL transaction and tracks delivery attempts, leases, and acknowledgements. No external sink adapter or approved acknowledgement contract is configured. A future grant or activation must still wait for a verified durable acknowledgement and use a reconciliation procedure for uncertain outcomes. This phase adds no provisioning code or routes and no permission grants.

#### Revocation and failure behavior

Revocation must use the same restricted job, identity, approval, and audit boundary. It must disable the tenant-scoped role assignment while preserving the historical record, emit an independently durable revocation outcome, and never delete audit history. The current `AuthorizationService` reloads active assignments and permission links during each authorization check, so a disabled assignment is denied on the next protected request; the authentication session itself is not revoked. If this per-request revalidation guarantee changes, session invalidation must be added before provisioning is enabled. An emergency-revocation runbook must identify its authorized owner and define how revocation proceeds safely during an audit-sink outage; the repository has no such runbook or durable local audit buffer.

For grants, the safe default is no mutation if any required identity, approval, target, template, database, or audit control is unavailable or inconsistent. For ambiguous outcomes, stop retries until the immutable request and audit state are reconciled; idempotency must prevent duplicate active grants. Emergency access disablement, failed audit delivery, and partial database/audit completion require an approved recovery procedure. No such procedure is currently established.

#### Exact external decisions and evidence required to reopen the gate

The organization must provide and verify all of the following before provisioning is enabled or any operator/tenant permission grant:

- The selected cloud/deployment IAM or federated identity provider, its production MFA policy, stable human and workload identity claims, and the enforceable IAM policy that limits execution to the approved job and named operators.
- The selected protected CI/CD or operations-job system, environment protection settings, distinct requester/approver enforcement, immutable approval-record lookup, one-time approval consumption, emergency stop owner, and job execution permission evidence. The repository currently contains no such workflow.
- The selected secret manager or workload-identity database-authentication mechanism, with evidence that the job receives only short-lived, least-privilege credentials. No secret-manager integration is present.
- The selected independently controlled append-only/tamper-evident audit product and its authentication/write integration, durable acknowledgment semantics, audit owner/readers, and tested outage/reconciliation behavior. No audit sink integration is present.
- The named security/platform-operations retention owner and approved retention duration, including legal-hold and deletion rules.
- A reviewed, version-controlled `COMMUNITY_ADMIN:v1` manifest and digest, explicit approval of the allowlist already proposed above, and confirmation of any catalog additions before they are implemented. The external evidence must also settle the separate AGENT template/delegation policy before agent assignments can grant agent-specific access.

**Provisioning gate status: blocked.** Phase 1–3.1 add platform identity, audit/outbox, onboarding, review, and activation-attempt models through source migration `20261012_08`; the applied database head is unknown. Review and fail-closed activation routes exist, and tenant resolution waits for the exact activation-completion receipt. No external IAM login flow/resolver, operator grant, supported audit-sink adapter/receipt, verified worker heartbeat, or PostgreSQL integration validation is configured. Tenant activation and Community Admin provisioning remain unavailable.

## Phase 1 security foundation

The platform identity validator uses Authlib and joserfc to validate provider-issued OIDC ID-token signatures and claims against a configured issuer's discovery document and JWKS. It checks the configured issuer and client audience, expiry/issued-at, one-use-flow nonce input, recent `auth_time`, and an explicit `acr` or `amr` assurance policy. The application does not implement an authorization-code login/callback flow, choose an IdP, create operator accounts, or expose privileged routes. Platform permission evaluation reads only platform operator assignments and never tenant RBAC.

`AuditService.record_mandatory` writes a non-sensitive audit event and matching outbox payload in the caller's transaction. It rejects unregistered metadata and personal actor/target references. Operator authentication outcomes and platform permission denials are mandatory audited events in the implemented boundary. Future security-sensitive mutations, approval/activation decisions, role grants/revocations, domain verification/revocation, and audit reads must record mandatory events in their state-change transaction. The external outbox worker has no concrete adapter in this repository; absent or unsupported sink configuration leaves events queued and readiness false.

See [docs/security-foundations.md](docs/security-foundations.md) for schema details, mandatory event rules, readiness fields, and production controls still required.
