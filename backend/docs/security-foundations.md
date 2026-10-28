# Phase 1 Security Foundations

**Status:** implemented in source; not operationally enabled.  
**Scope:** Phase 1 identity, audit, outbox, and readiness foundations, extended by Phase 2 onboarding, Phase 3 review/activation, Phase 3.1 activation-outcome receipt gating, and the Phase 4.4 platform-operator OIDC session boundary. Tenant activation and employee operations remain unavailable under current configuration.

## Current implementation boundary

- Source Alembic head is `20261013_09`, parent `20261012_08` (`20261012_05` remains the Phase 1 foundation revision). No database was contacted. The applied revision and schema state remain unknown.
- `platform_operators` maps an external `(issuer, subject)` pair to a local operator reference. There are no seeded operator rows, local credentials, bootstrap bypasses, roles, or role assignments.
- Platform permissions, roles, links, and assignments use separate tables and a separate evaluator from tenant RBAC. The migration seeds permission definitions only. A tenant role such as `audit.read` cannot satisfy `platform.audit.read`.
- `PlatformOIDCLoginService` and `/api/v1/platform/auth/oidc/{start,callback,me,logout}` implement an operator-only Authorization Code + PKCE flow. State, browser binding, nonce, and verifier are random; the state/binding are hashed and nonce/verifier are encrypted in a short-lived server transaction. The callback exchanges one code, validates the ID token against the configured issuer/JWKS/audience/nonce/MFA policy, resolves only an existing active `PlatformOperator`, then issues an opaque, revocable local session. Session credentials are persisted only as a digest in `platform_operator_sessions`; no operator or grant is created. The older server-side token-authentication service remains an internal boundary, not an HTTP login route.
- OIDC configuration is absent in the checked-in examples. No identity provider has been selected. Missing settings make login and the default session resolver unavailable; partial or unsafe settings are invalid. No issuer or audience is assumed by application code. The OIDC session flow is therefore not live-verified or operational in the current environment.
- `AuditService.record_mandatory` inserts `audit_events` and `audit_outbox_events` with one `Session.flush()` in the caller's transaction. It does not commit or swallow errors. A failed flush raises to the caller so the caller's business transaction rolls back.
- The outbox stores a canonical event payload and SHA-256 digest, a unique idempotency key, state, next-attempt time, lease, attempt count, last error category, and acknowledgement reference/time. `audit_outbox_attempts` stores one durable row per delivery attempt. Expired leases are recovered on a later claim. PostgreSQL workers claim with `FOR UPDATE SKIP LOCKED`.
- `AuditSink` is provider-neutral. There is no concrete provider adapter because none is configured or approved. `UnavailableAuditSink` never reports success. An unsupported/missing adapter does not claim events or create acknowledgements. Worker delivery stays disabled by default.
- A healthy worker records a short-lived heartbeat only after its sink health check succeeds. Readiness and activation require a recent heartbeat for the configured adapter, no later than the check time, as well as verified sink health. The heartbeat write is internal and has no tenant-facing route; production must give the worker a separate service identity and restricted database rights because application-level DB access can otherwise forge the row.
- Phase 2 onboarding and Phase 3 review/activation routes are mounted under `/api/v1/platform/onboarding-requests`. They use the platform principal dependency and centralized platform permission evaluator. The production principal resolver, operator grants, supported sink adapter, verified acknowledgements, worker heartbeat, and PostgreSQL concurrency validation are absent, so activation remains blocked. The tenant resolver also denies onboarding domains until the exact activation-completion event is delivered with a verified receipt. No audit acknowledgement is fabricated.

The OIDC validation boundary uses Authlib's OIDC ID-token claim type and joserfc's JWT/JWK implementation. Those libraries support OIDC ID-token validation, restricted algorithms, and JWKS-based signature verification; the code supplies the project-specific issuer, audience, nonce, and assurance policy checks. [Authlib OIDC client validation guidance](https://docs.authlib.org/en/stable/oauth2/client/http/index.html), [joserfc JWT/JWK guidance](https://jose.authlib.org/en/guide/jwt/).

## OIDC settings

Settings are optional at process start so non-operator API behavior can remain available. The platform identity service itself fails closed when they are absent. If any OIDC setting is supplied, the issuer, client ID, and at least one explicit assurance allowlist are required.

- `PLATFORM_OIDC_ISSUER`: exact HTTPS issuer URL (HTTP is allowed only under `ENVIRONMENT=test`).
- `PLATFORM_OIDC_CLIENT_ID`: expected OIDC ID-token audience.
- `PLATFORM_OIDC_REQUIRED_ACR_VALUES` and/or `PLATFORM_OIDC_REQUIRED_AMR_VALUES`: explicit accepted authentication-strength values; at least one must be configured. If both are set, both policies must pass.
- `PLATFORM_OIDC_ALLOWED_ALGORITHMS`: restricted asymmetric allowlist. The code accepts only `RS256`, `PS256`, and `ES256`; `none` and symmetric algorithms are rejected.
- `PLATFORM_OIDC_CLIENT_SECRET`: confidential-client credential, supplied through the deployment secret manager.
- `PLATFORM_OIDC_REDIRECT_URI`: exact HTTPS public origin and `/backend-api/api/v1/platform/auth/oidc/callback` path registered with the selected provider.
- `PLATFORM_OIDC_TRANSACTION_ENCRYPTION_KEY`: Fernet key for short-lived nonce and PKCE verifier data; manage and rotate through the secret manager.
- `PLATFORM_OIDC_LOGIN_TTL_SECONDS`, `PLATFORM_OIDC_SESSION_TTL_SECONDS`, `PLATFORM_OIDC_SESSION_COOKIE_NAME`, `PLATFORM_OIDC_SESSION_COOKIE_PATH`, `PLATFORM_OIDC_SESSION_COOKIE_SAMESITE`: bounded state/session and proxy-cookie controls. Cookie scope is host-only and restricted to `/backend-api/api/v1/platform`; `Secure` follows the existing environment cookie policy.
- `PLATFORM_OIDC_DISCOVERY_TIMEOUT_SECONDS`, `PLATFORM_OIDC_JWKS_CACHE_SECONDS`, `PLATFORM_OIDC_CLOCK_SKEW_SECONDS`, `PLATFORM_OIDC_MAX_AUTH_AGE_SECONDS`: bounded validation controls.

The verifier checks discovery issuer equality, HTTPS discovery/JWKS endpoints outside tests, key ID and signing-key use, supported signing algorithm, signature, `iss`, `aud`, `azp` where applicable, `sub`, `exp`, `iat`, expected nonce, configured `acr`/`amr`, and a recent `auth_time`. It does not trust email domains, tenant roles, frontend state, embedded token keys, or remote `jku`/`x5u` values. Callback state is atomically claimed once and bound to a host-only browser cookie; failures cannot be replayed. Provider access tokens are not stored. Local logout revokes the server session and requires both the session's CSRF token and an allowed same-origin `Origin`/`Referer`. The ASGI app clears callback query data after route handling so the one-use code/state do not enter backend access logs. Production ingress and Next.js proxy access logs must also omit or redact query strings on this callback path; that deployment logging policy has not been verified here.

The platform session authenticates only platform operators. It does not establish a tenant `User`, membership, employee record, employee link, or employee permission. The legacy unsigned `ep_session` cookie is still ignored. The existing tenant OTP flow still proves only an existing active membership. Consequently no trusted employee principal is available and the registration-photo route remains fail-closed.

## Audit event contract

Audit rows contain event UUID/type/schema version, occurrence and database-recorded timestamps, actor category and stable reference, optional tenant, target type/reference, action, outcome, request ID, and a flat allowlisted metadata object. Outbox payloads mirror those safe fields and share the audit event UUID. The service rejects unknown metadata keys, free-form reasons, non-UUID human actor/target references, and unsafe timestamps. It never accepts token/password/OTP/challenge fields.

### Mandatory audit events

The following event classes are mandatory and must use `record_mandatory`; queue insertion is not acknowledgement of delivery:

1. Operator authentication accepted, denied because the external subject is unknown/inactive, rejected for invalid identity claims, or unavailable because the configured identity provider failed. No raw token, nonce, email, phone, or external subject is recorded.
2. Platform authorization denials. Successful privileged operations must also record their action/outcome inside the transaction that changes state.
3. Every future security-sensitive state change: tenant request decisions and activation, domain proof/revocation, operator and tenant role grants/revocations, membership or account status changes, suspension/reactivation, and audit-event access.
4. Future administrative reconciliation and migration/cutover actions that change security or tenant-owned data.

Business activity timelines are not security audit records. Delivery attempt state lives in the outbox/attempt tables. Future provisioning must not proceed until the specific mandatory event has an authenticated acknowledgement matching its event ID and payload digest.

Metadata currently permits only `reason_code`, `policy_version`, `auth_strength`, and `attempt_number`. Human actor and target references must be stable UUIDs. System actor references use a short `system:<service-code>` label. Avoid placing names, mobile numbers, email addresses, uploaded-document details, credentials, tokens, OTP values, session secrets, or domain challenges in event fields.

### Local table limitations and production controls

The application service exposes no audit update/delete operation, and restrictive foreign keys preserve referenced records. This is **not** tamper evidence: a database owner can still alter local rows. Before production provisioning, add and test controls outside the application layer:

- Give the application audit writer insert-only rights on `audit_events`; give the worker only the specific outbox/attempt/heartbeat rights it needs. Deny ordinary operators update/delete access to audit history.
- Deliver to an independently administered append-only/WORM sink with authenticated acknowledgements, retention lock, and a receipt tied to event ID and payload digest.
- Decide retention, legal hold, deletion, encryption/key ownership, audit-reader policy, and separation between platform operators and audit administrators.
- Alert on sink health failures, oldest queued-event age, retryable/terminal failures, missing worker heartbeat, and acknowledgement mismatches. Test restore and outage reconciliation.

## Outbox delivery behavior

Outbox states are `queued`, `delivering`, `retryable_failure`, `delivered`, and `terminal_failure`. Attempt rows distinguish in-progress, delivered, retryable, and terminal outcomes. A stable event UUID is the idempotency key. Exponential bounded backoff with stable per-event jitter is used. A worker lease expires after a crash; another worker can reclaim it and mark the prior attempt as lease-expired. Concurrent PostgreSQL workers use row locks with `SKIP LOCKED`.

An adapter must verify the remote acknowledgement contract before returning `verified=True`. The worker additionally checks event ID, payload digest, non-empty safe receipt reference, and timestamp. A `2xx`, a local queue write, or an unverified response is not success. No supported adapter is registered in this project, so no sink is configured or verified, no worker heartbeat can be recorded, and the worker cannot deliver events.

Typed sink fields are available for a future adapter name, HTTPS endpoint, credential, and acknowledgement-verification key. Supplying all fields does not make an unsupported adapter operational; health remains unverified and delivery remains unavailable. The selected provider, request/receipt format, secret manager, and outage contract remain unresolved.

## Readiness behavior

- `GET /health` and `GET /api/v1/health` remain liveness checks and return `{"status":"ok"}` without claiming provisioning readiness.
- `GET /health/ready` and `GET /api/v1/health/ready` return separate booleans for database verification, operator integration configuration and verification, external audit configuration and verification, worker readiness, and `privileged_provisioning_eligible`.
- Readiness is HTTP 503 unless the database, complete configured/verified operator login boundary, real configured/verified audit adapter, enabled worker with a recent verified heartbeat, and production/staging environment gate all pass. Responses distinguish OIDC validation configuration from login configuration and contain no endpoint, issuer, credential, key, or exception details.
- Privileged onboarding routes are mounted, but mounting a route and passing readiness do not create authority. Readiness requires approve/activate grant coverage and a distinct requester/reviewer/activation-operator combination. Each state-changing service still checks the individual operator's permission and activation gates.

## Migration and validation status

Revision `20261012_05_platform_security_foundations` is additive after source head `20261012_04`. Revisions `20261012_06` and `20261012_07` add onboarding/challenge history and review/activation-attempt records. Revision `20261012_08` additively links an activation attempt to its completion audit event so tenant resolution can wait for the exact receipt. Revision `20261013_09` adds one-use OIDC login transactions and revocable operator sessions after `20261012_08`. No migration seeds an operator, role, role-permission link, assignment, membership, or Community Admin. Existing migrations were not edited.

Migration tests apply these additive revisions to isolated in-memory SQLite databases with the existing schema shape. No production or development database was contacted. `TEST_DATABASE_URL` was not configured during implementation, so PostgreSQL migration/concurrency validation remains outstanding and the applied Alembic revision remains unknown.

## Blockers before tenant provisioning

1. Select and configure the external OIDC provider, client/audience, registered MFA assurance claims, redirect URI, client secret, transaction encryption key, and production cookie/session policy; then verify the flow against an explicitly configured test provider.
2. Approve a controlled operator bootstrap and role-assignment process with independent approval, external identity evidence, least-privilege IAM, and recovery; this implementation inserts no operators or grants.
3. Select an external audit product and supported adapter contract, authenticate and verify its acknowledgement format, configure its secrets through the approved secret manager, and test delivery/outage recovery.
4. Decide audit retention, legal hold, immutable storage, read access, monitoring owner, and emergency behavior during sink outage.
5. Deploy a worker process using the approved adapter and demonstrate fresh verified heartbeats, retries, terminal failure alerting, and restart recovery.
6. Establish a dedicated PostgreSQL test environment for dialect-specific migration and concurrency validation. Before any production upgrade, independently inspect the applied head and rehearse backup/restore.
7. Before production activation, configure and demonstrate the real OIDC principal resolver, explicit operator grants, supported durable audit adapter and verified receipt flow, running worker with a separate service identity, and PostgreSQL migration/concurrency behavior. The activation outcome audit is emitted after the activation transaction and recovered through the outbox; the tenant resolver remains closed until its exact receipt is verified. Community Admin invitation/provisioning remains separate and unavailable.
8. Before employee-authorized subscriber registration, define the canonical employee record and authoritative tenant ownership evidence, approve a target-acceptance and independent-review link workflow, define the permission/role policy, and implement/audit the link lifecycle. No employee link or employee principal is implemented here.
