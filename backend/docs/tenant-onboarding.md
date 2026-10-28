# Operator mediated tenant onboarding

## Scope and current availability

The onboarding API records an inactive tenant, a request, and proof that an operator selected domain publishes an exact DNS TXT challenge. Independent review, a separate activation intent, and an audit-gated activation boundary are implemented. The activation-completion event must also receive a verified external receipt before tenant resolution admits traffic. Approval by itself never activates a tenant or domain. No community identity, membership, Community Admin, or role assignment is created by review or activation.

Every route requires a server-resolved platform operator principal and a platform permission. The OIDC validation boundary and platform permission evaluator exist, but this workspace has no configured production OIDC provider, request-to-principal resolver, provisioned platform operator grants, supported external audit-sink adapter, or verified external acknowledgement. Privileged API calls therefore fail closed with `PLATFORM_AUTHENTICATION_UNAVAILABLE` (503), authorization denials, or `TENANT_ACTIVATION_BLOCKED`; no route can activate a tenant under the current configuration. Tenant OTP sessions and tenant roles cannot satisfy platform checks. Readiness is not proof of eligibility at the activation boundary.

The API router is mounted under the configured `api_v1_prefix` (default `/api/v1`). There are no frontend onboarding routes in this phase.

## State transitions

```text
PENDING_DOMAIN_PROOF ── exact DNS TXT proof + local audit/outbox commit ──> PENDING_ACTIVATION_REVIEW
       │                                  │
       ├── operator rejection ────────────┴──────────────────────────────> REJECTED
       ├── operator cancellation ────────────────────────────────────────> CANCELLED
       └── request expiry ───────────────────────────────────────────────> EXPIRED
```

After domain proof, one independent reviewer can record an immutable approve or reject decision. The request creator cannot review their request. Approval leaves the request in `PENDING_ACTIVATION_REVIEW`; rejection changes it to terminal `REJECTED`. Approval must not be interpreted as activation. Activation intent is a distinct operation. It creates a durable attempt in `PENDING_AUDIT_ACK` and an audit/outbox event; activation requires verified receipts for the exact approval and intent events. Activation atomically marks the tenant and verified domain active and the request `ACTIVE`, and records the completion event in the same transaction. The completion event ID is linked to the attempt. The tenant resolver additionally requires that exact event to be delivered with a verified receipt before it admits traffic. The activation executor must differ from the reviewer. `PENDING_DOMAIN_PROOF` and `PENDING_ACTIVATION_REVIEW` can also be cancelled or expire; rejected, cancelled, and expired requests cannot activate. Request transitions are service controlled, versioned, and use row locks where supported. A partial unique index protects each domain from simultaneous active onboarding claims; a global unique index on `tenant_domains.hostname` prevents a verified domain row from being reused.

Requests create a tenant record with `status=inactive`. Successful proof inserts a `TenantDomain` with `verification_status=verified`, `verified_at` set, and `is_active=false`, links it to the request, and changes only the request to `PENDING_ACTIVATION_REVIEW`. Existing tenant resolution still requires both an active tenant and an active verified domain.

## API contract

| Method and path | Permission | Request | Result |
|---|---|---|---|
| `POST /platform/onboarding-requests` | `platform.onboarding.create` | JSON `{ "tenant_slug": "north-coast", "domain": "community.example" }`; `Idempotency-Key` header | `201`; request summary and `replayed` flag |
| `GET /platform/onboarding-requests` | `platform.onboarding.read` | Optional `state`, `limit` (1–100), `offset` | Safe summaries only |
| `GET /platform/onboarding-requests/{request_id}` | `platform.onboarding.read` | UUID request identifier | Safe summary and bounded audit history |
| `POST /platform/onboarding-requests/{request_id}/domains/challenge` | `platform.domain.challenge` | `Idempotency-Key` header | TXT owner, one-time TXT value, expiry |
| `POST /platform/onboarding-requests/{request_id}/domains/verify` | `platform.domain.verify` | No body; server checks DNS | Safe request summary |
| `POST /platform/onboarding-requests/{request_id}/cancel` | `platform.onboarding.cancel` | No body | Updated safe summary |
| `POST /platform/onboarding-requests/{request_id}/reject` | `platform.tenant.reject` | JSON reason code; `Idempotency-Key` | Review result |
| `POST /platform/onboarding-requests/{request_id}/review` | `platform.tenant.approve` or `platform.tenant.reject` | JSON decision and reason code; `Idempotency-Key` | Immutable review result; approval leaves request pending |
| `GET /platform/onboarding-requests/{request_id}/activation-eligibility` | `platform.onboarding.read` | No body | Safe eligibility boolean and blocker codes |
| `POST /platform/onboarding-requests/{request_id}/activation-intents` | `platform.tenant.activate` | `Idempotency-Key` | `202`; attempt id and `PENDING_AUDIT_ACK` state |
| `POST /platform/onboarding-requests/{request_id}/activation-attempts/{attempt_id}/activate` | `platform.tenant.activate` | Attempt UUID in path | Safe activation outcome; only with all current gates satisfied |

Review request and response:

```json
{
  "decision": "approve",
  "reason_code": "organization_verified"
}
```

```json
{
  "request_id": "3c62f026-a424-4f23-9f25-b2bfcae4a4bc",
  "decision": "approve",
  "request_state": "PENDING_ACTIVATION_REVIEW",
  "decision_at": "2026-10-12T10:00:00Z",
  "replayed": false
}
```

Eligibility response and activation-intent response:

```json
{
  "request_id": "3c62f026-a424-4f23-9f25-b2bfcae4a4bc",
  "eligible": false,
  "blockers": ["AUDIT_SINK_UNAVAILABLE", "OPERATOR_IDENTITY_UNAVAILABLE"]
}
```

```json
{
  "request_id": "3c62f026-a424-4f23-9f25-b2bfcae4a4bc",
  "activation_attempt_id": "a4856de3-6b14-4b6d-967b-cdd7b0fac1a5",
  "state": "PENDING_AUDIT_ACK",
  "created_at": "2026-10-12T10:00:00Z",
  "replayed": false
}
```

The final activation response reports tenant/domain state, `tenant_resolution_enabled`, and `replayed`. Immediately after activation commits, `tenant_resolution_enabled` is false until the completion event receipt is stored; an idempotent retry reports the current value with `replayed: true`. A blocked response uses the standard envelope with code `TENANT_ACTIVATION_BLOCKED` and an allowlisted `blockers` array; for example, a missing external receipt returns `APPROVAL_AUDIT_NOT_ACKNOWLEDGED` or `ACTIVATION_INTENT_NOT_ACKNOWLEDGED`. The API never returns receipt credentials, audit payloads, raw challenges, or internal database errors.

All identifiers are opaque UUIDs. Request creation accepts a tenant slug and requested hostname, never a client supplied tenant ID, status, role, permission, or verification result. The backend creates and binds the tenant ID and normalized hostname. List and detail calls are platform operations; there is no tenant-member view of this administrative queue.

Supported rejection reason codes are `evidence_incomplete`, `request_ineligible`, and `policy_review_failed`. The request creator cannot reject its own request. An authorized reviewer must have a different operator identity. Cancellation is separately permissioned and audited.

Error responses use the standard API envelope. In addition to the Phase 2 codes, review errors include `ONBOARDING_CREATOR_CANNOT_REVIEW` (403) and `ONBOARDING_REVIEW_CONFLICT` (409). Activation errors use `TENANT_ACTIVATION_BLOCKED` with safe blocker codes such as `OPERATOR_IDENTITY_UNAVAILABLE`, `AUDIT_SINK_UNAVAILABLE`, `POSTGRESQL_REQUIRED`, `OUTBOX_WORKER_UNAVAILABLE`, `REVIEW_NOT_APPROVED`, `APPROVAL_AUDIT_NOT_ACKNOWLEDGED`, `ACTIVATION_INTENT_NOT_ACKNOWLEDGED`, `DOMAIN_NOT_VERIFIED`, and `TENANT_DOMAIN_MISMATCH`. State conflicts return 409; missing operational/security gates return 503. Missing operator integration fails closed with 503; an authenticated operator without the required platform permission receives 403. No credentials, provider tokens, database details, or sink configuration are returned.

## Domain challenge and DNS rules

The server generates a 32-byte cryptographically random URL safe token. Only a SHA-256 digest bound to the challenge purpose, onboarding request UUID, normalized domain, and raw token is stored. The API returns the raw value once as `community-ott-domain-verification=<token>` with `Cache-Control: no-store`. The challenge response is not replayed on an `Idempotency-Key` retry: the replay response has `dns_txt_value: null`; issue a new challenge with a new idempotency key if the first response was lost.

Publish the exact TXT value at:

```text
_community-ott-verification.<normalized-domain>.
```

Verification queries that exact DNS owner through the system resolver with a configured timeout, answer-count cap, and byte cap. Only a complete exact TXT string whose token matches the stored request/domain-bound digest proves ownership. Unrelated values, partial values, resolver failures, timeouts, and oversized responses do not prove ownership. Each request permits a bounded number of challenge issuances and verification attempts. Active attempts use a persisted lease so concurrent requests cannot consume one challenge twice; stale attempts can be recovered after the lease expires.

Hostname handling uses the existing hostname normalization: lower case, trailing dot removal, IDNA A-label conversion, DNS label validation, and rejection of IP addresses, ports, URLs, and single-label names. The proof owner is formed from the exact requested hostname. An apex name and a subdomain are separate claims; proof for a parent does not establish ownership of a child or vice versa. There is no wildcard or HTTP URL verification path.

Domain reassignment is intentionally unavailable. Existing verified or revoked `TenantDomain` rows keep the hostname unique. A later policy and reviewed revocation/reassignment workflow must define when such a claim can be reused; cancellation, rejection, or expiry does not delete evidence or release a hostname already recorded as verified.

## Audit, outbox, and transaction behavior

Mandatory audit events cover request creation and reads, authorization denials, challenge issue/rotation/expiry, verification start and result, request rejection/cancellation, and request expiry. Audit metadata contains only controlled reason codes; it excludes the raw TXT challenge and other credentials. For every state change, the local `AuditEvent`, its outbox row, and the onboarding/domain mutation are flushed in the same SQLAlchemy transaction. A local audit/outbox failure rolls the operation back. Queue insertion is not external delivery acknowledgement.

The outbox has no configured production sink in this workspace. No acknowledgement is fabricated. Domain proof and review can be recorded locally with matching outbox rows, but neither queue insertion nor local audit persistence counts as an external acknowledgement. External durable retention, access control, and delivery monitoring remain production requirements.

## Expiry and retries

Request and challenge TTLs, attempt limits, DNS resolver limits, and expiry batch size are typed settings documented in `.env.example`. Expiry is enforced when onboarding operations inspect or process a request. `expire_due_requests` is an internal maintenance operation, but no scheduler/worker is registered in this phase; production must schedule a trusted caller before relying on prompt bulk expiry. A request's own expiry is checked on each operation even when it is not included in an expiry batch.

Request and challenge creation use operator scoped idempotency keys. Retrying the same key with the same request input returns the original request; using the key for different input is a conflict. Persistent audit events also enforce aggregate per-operator windows: 20 challenge issuances per hour and 60 verification DNS queries per 15 minutes by default. This prevents a caller from bypassing request level limits by creating many requests. These typed settings can be tuned to operational volume.

A domain can have only one pending proof/review claim. Tenant slug and tenant ID are unique, so cancellation, rejection, or expiry does not make a slug reusable. A supported reapplication or deletion policy is a later decision; the service does not silently remove retained records.

## Migration and tests

Alembic revision `20261012_06` adds onboarding and challenge history after `20261012_05`. Revision `20261012_07` adds immutable review and activation-attempt records after `20261012_06`. Revision `20261012_08` adds the optional unique completion-audit-event reference after `20261012_07`. It remains nullable so pre-existing rows migrate safely; an active onboarding record with no correlated completion receipt is denied by tenant resolution. The source chain is linear through `20261012_08`; no migration has been applied to a database and the actual applied revision remains unknown. These revisions create no operators, grants, memberships, or Community Admins.

Focused tests use isolated temporary SQLite databases, mocked platform principals, and fake DNS resolvers. They never contact an OIDC provider or public DNS service. SQLite does not implement PostgreSQL row locking. A separate PostgreSQL concurrency test is opt-in through an explicitly named isolated `TEST_DATABASE_URL`; it must not be pointed at production or `bunny_ott`.

## Independent review, activation, and recovery

An approve/reject review is immutable and unique per onboarding request. It records the server-resolved reviewer UUID, requester UUID, decision, bounded reason code, policy version, timestamp, idempotency key, and mandatory audit event reference. The requester cannot review. A review conflict or replay with different content is rejected. This implementation requires the activation executor to be a different operator from the reviewer; whether policy also requires two distinct human reviewers is still an owner decision.

Immediately before activation intent and again before activation, the centralized policy checks the live trusted principal and MFA claims, configured and healthy OIDC provider, configured and healthy supported audit sink, PostgreSQL dialect, recent non-future verified outbox-worker heartbeat, request state and expiry, reviewer separation, acknowledged approval audit event, tenant/domain binding, exact verified domain, and inactive tenant/domain state. Receipt lookups require a delivered outbox row with acknowledgement reference/time and a matching audit event for the exact tenant and onboarding request, event type, action, and outcome. The outbox worker validates the adapter's cryptographically verified receipt flag, event ID, payload digest, reference shape, and timestamp before setting delivered. The same request/tenant/domain rows are locked in the state-changing transaction. Safe blocker codes do not reveal secrets or internal configuration. Existing database transaction semantics must be validated against an isolated PostgreSQL deployment before production.

The recovery sequence is:

1. The review transaction commits the immutable decision and its local audit/outbox event together. Delivery retries use the existing outbox idempotency event ID; activation remains blocked until a verified sink receipt is stored for the same event ID and payload digest.
2. An authorized activation operator creates an idempotent activation attempt and a mandatory `tenant.activation.intent` audit/outbox event in one transaction. The attempt remains `PENDING_AUDIT_ACK` until the outbox worker records the sink's verified receipt. A timeout or ambiguous delivery remains unacknowledged; the outbox retries the same event idempotently, and activation stays blocked.
3. A distinct authorized operator retries the activation endpoint with the attempt ID. The service rechecks every gate and matching receipt in the activation transaction. It atomically sets tenant/domain active, request `ACTIVE`, attempt `ACTIVATED`, and the completion event reference, while enqueueing the outcome audit event. A crash before transaction commit leaves the state inactive and the same intent can be retried. A crash after commit is reconciled from the committed request/attempt and outbox rows; duplicate calls return the existing outcome rather than creating another tenant, membership, or role assignment.
4. Tenant resolution denies an active onboarding domain until its linked completion event is delivered with the verified receipt. A timeout, rejection, or terminal outbox failure therefore leaves the tenant unreachable; operator intervention cannot fabricate acknowledgement. Retryable outcomes reuse the same immutable event ID/idempotency key. There is currently no operator API to redrive a terminal failure; an audited remediation procedure must be approved before production. If an approval or intent receipt is persisted and a later activation transaction fails, that receipt remains durable and the same operation can retry. If the activation transaction's commit result is ambiguous, retry reads the committed request/attempt state; it cannot create a second activation. The outcome event is in the activation transaction, so a rollback leaves both activation state and its outcome event absent. The database and external sink do not share a distributed transaction; the outbox provides idempotent delivery and the resolver gate closes the post-commit visibility gap.

No member provisioning is attached to this workflow. `CommunityAdminProvisioner` is an interface only; its current implementation always fails closed. Nominee identity verification, invitation delivery, acceptance proof, and role assignment policy remain unresolved and must be designed as a separate authorized, auditable workflow.
