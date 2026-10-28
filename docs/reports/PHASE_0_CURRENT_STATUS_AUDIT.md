# Community OTT Agent Platform — Phase 0: Comprehensive Current Status Audit

**Document Date:** October 17, 2026  
**Audited Workspace:** `D:\AVANFlix\OTT_Agent_App`  
**Audit Baseline Documents:**  
1. `Community_OTT_Agent_Platform_Architecture_Updated_Photo_Registration.md` (Located at `D:\AVANFlix\OTT_Agent_App\Community_OTT_Agent_Platform_Architecture_Updated_Photo_Registration.md`)  
2. `FRONTEND_INTEGRATION.md` (Located at `D:\AVANFlix\OTT_Agent_App\docs\implementation\FRONTEND_INTEGRATION.md` and `D:\FRONTEND_INTEGRATION.md`)

---

## 1. Executive Summary

### Overall Project Health
The **Community OTT Agent Platform** has substantial, high-quality, production-grade security foundations, robust multi-tenant domain resolution, tamper-resistant cryptographic implementations (Argon2id password hashing, AES-256-GCM authenticated PII encryption, salted SHA-256 OTP verification), and comprehensive test coverage across both backend and frontend layers.

However, the repository currently reflects a hybrid evolution:
1. **The core modern OTT platform** runs on FastAPI + PostgreSQL with Next.js 14 App Router, protected by strict session isolation, an SSR backend proxy, and automated security migrations.
2. **A legacy employee/admin portal** remains partially embedded in the frontend, containing routes and UI components that directly attempt to connect to MongoDB.
3. **Live external delivery of OTPs and cloud object storage** are dependent on external infrastructures (Notification Microservice and Cloudflare R2) that are currently either unconfigured in live production or blocked by external service routing issues.

### Three-Part Completion Assessment

| Dimension | Real Completion | Operational Verdict |
|---|:---:|---|
| **Platform Core / Multi-Tenancy / Security Foundation** | **88%** | **Strong & Verified**: Multi-tenant resolution, subdomains, TXT DNS verification, session cookies, CSRF protection, AES-256-GCM PII encryption, and audit event outboxes are fully implemented with 301 passing automated tests. |
| **Agent & Subscriber Operations (Phase 5 Scope)** | **65%** | **Partially Implemented**: Direct agent and subscriber creation, region assignments, commission overrides, and photo policy enforcement are working and tested. However, the architectural 2-step appointment workflow (`PENDING` -> `APPROVED`) and descendant hierarchy/referral trees (`member_relationships`) are missing or bypassed. |
| **External Integrations (R2 Storage & Notifications)** | **35%** | **Code Complete / Blocked at Runtime**: Code contracts for Cloudflare R2 and Notification Microservice are fully implemented and verified via in-memory mocks. However, live OTP delivery is blocked by external 403 / `TENANT_NOT_FOUND` routing errors on `handorg.duckdns.org`, and live R2 bucket storage requires production credentials. |

### Top 3 Critical Risks and Blockers

1. **Live Notification Microservice Unreachable / Misconfigured (External Blocker)**  
   The configured external endpoint (`http://handorg.duckdns.org/notifications`) fails with HTTP 403 on `/notifications/api/v1/messages` and `400 TENANT_NOT_FOUND` on `/api/v1/messages`. While the backend gracefully functions in `mock` and `development_dummy` modes, live mobile OTP delivery for login and registration is blocked until the external service tenant and routing are provisioned.
2. **Dual-Database Architectural Fracture (PostgreSQL vs MongoDB)**  
   While the backend is 100% PostgreSQL with zero MongoDB dependencies, the frontend still contains 7 legacy Next.js API routes (`/api/support`, `/api/activity`, `/api/employees`, `/api/users`, etc.) and 6 legacy UI components calling them. If MongoDB is unconfigured or unavailable, these UI views throw runtime errors.
3. **Bypass of Agent Appointment Approval Workflow**  
   The baseline architecture mandates that agent registration creates a `PENDING` appointment subject to reviewer approval (`agent.approve` / `agent.reject`). Currently, Super Admins and Community Admins directly create active agents, bypassing the `agent_appointments` and `approval_workflows` modules, which remain empty directory stubs.

---

## 2. Stack & Module Architecture

### Backend Stack
- **Framework:** FastAPI 0.115+ (Python 3.12)
- **Database Layer:** PostgreSQL with SQLAlchemy 2.0 and Alembic 1.13 migrations; psycopg 3.2 binary driver.
- **Security & Crypto:** Argon2id (`argon2-cffi`), AES-256-GCM (`cryptography.hazmat`), HMAC-SHA256, constant-time comparisons (`secrets.compare_digest`).
- **HTTP Client:** `httpx` 0.27+ (async client).
- **Imaging:** `Pillow` 10.4+ with image sanitization and decompression bomb guards.

### Backend Module Inventory (`backend/app/modules/`)

| Module Directory | Current Status | Implemented Functionality & Architecture Notes |
|---|---|---|
| `agents` | **Active** | `api.py`, `models.py`, `schemas.py`, `ref_id.py`. Implements Super Admin and Admin agent creation, agent profile updates, commission rate audits, subscriber registration, photo binding. |
| `agent_appointments` | **Empty Stub** | Contains only `__init__.py`. Appointment workflow logic is bypassed by direct agent creation in `agents/api.py`. |
| `approval_workflows` | **Empty Stub** | Contains only `__init__.py`. Multi-step approval state machine (`approval_requests`, `approval_decisions`) is not yet implemented. |
| `audit` | **Active** | `api.py`, `models.py`, `service.py`. Transactional audit event creation, actor/tenant tracking, metadata sanitization, and query endpoints. |
| `authentication` | **Active** | `api.py`, `models.py`, `otp_service.py`, `otp_login.py`, `abuse_control.py`, `password.py`, `repository.py`. Password login, mobile OTP login, rate limiting, and registration verification tokens. |
| `authorization` | **Active** | `catalog.py`, `dependencies.py`, `models.py`, `service.py`, `repository.py`. 34 catalog permissions, custom tenant roles, `@require_permission` dependency. |
| `commissions` | **Empty Stub** | Contains only `__init__.py`. Commission overrides and audit logging are currently placed in `agents/api.py` and `tenants/models.py`. |
| `employees` | **Empty Stub** | Contains only `__init__.py`. Legacy employee operations remain in frontend legacy routes. |
| `geographic_scopes` | **Empty Stub** | Contains only `__init__.py`. Region management is implemented in `tenants/models.py` and `agents/api.py`. |
| `identity` | **Active** | `api.py`, `models.py`, `repository.py`. Platform and tenant user accounts, memberships, and credential bindings. |
| `members` | **Empty Stub** | Contains only `__init__.py`. Subscriber profiles live in `agents/models.py`; hierarchical member models are missing. |
| `messaging` | **Active** | `contracts.py`, `dependencies.py`, `http_provider.py`, `notification_service.py`, `service.py`, `testing.py`. Full integration client for external Notification Microservice. |
| `notifications` | **Empty Stub** | Contains only `__init__.py`. Notification dispatch is handled directly via `messaging/notification_service.py`. |
| `onboarding` | **Active** | `activation.py`, `dns_verification.py`, `provisioning.py`, `service.py`. Tenant registration, DNS TXT verification, activation reviews, domain provisioning. |
| `outbox` | **Active** | `models.py`, `repository.py`, `service.py`, `sink.py`. Transactional outbox pattern for audit events with worker leases and retry state. |
| `payments` | **Empty Stub** | Contains only `__init__.py`. Subscriptions and payment gateway integrations are deferred to future phases. |
| `platform_identity` | **Active** | `api.py`, `oidc_login.py`, `provider.py`, `service.py`. Super Admin / Operator OIDC authentication with MFA claims verification. |
| `referrals` | **Empty Stub** | Contains only `__init__.py`. Multi-level referral hierarchies are not yet implemented. |
| `registration_photos` | **Active** | `api.py`, `models.py`, `schemas.py`, `storage.py`. Pillow image validation, sanitization, Cloudflare R2 S3 adapter, authenticated streaming preview. |
| `subscriptions` | **Empty Stub** | Contains only `__init__.py`. Deferred to Phase 6+. |
| `support` | **Empty Stub** | Contains only `__init__.py`. Legacy support routes exist in frontend connecting to MongoDB. |
| `tenants` | **Active** | `api.py`, `dependencies.py`, `hostname.py`, `models.py`, `repository.py`, `service.py`. Community lifecycle, domain resolution, administrator provisioning, region models. |

### Frontend Stack (`frontend/`)
- **Framework:** Next.js 14.2.35 (React 18.3, TypeScript 5.5, App Router).
- **Styling:** TailwindCSS 3.4.
- **Service Worker / PWA:** Serwist 9.5 (`@serwist/next`).
- **Custom Entrypoint:** `server.cjs` (custom Node server handling host forwarding and port binding).
- **Proxy Layer:** `src/app/backend-api/[...path]/route.ts` forwards authenticated requests to FastAPI backend with cookie isolation, CSRF validation, and header normalization.

---

## 3. Data Architecture: PostgreSQL vs MongoDB

### Backend PostgreSQL Architecture (100% PostgreSQL)
The FastAPI backend interacts **exclusively** with PostgreSQL via SQLAlchemy 2.0 ORM and Alembic migrations. There is **zero** MongoDB code, zero pymongo dependencies, and explicit test assertions ensuring pymongo is not loaded (`assert "pymongo" not in sys.modules`).

#### PostgreSQL Tables Implemented:
1. `tenants`: Multi-tenant community entities with slugs, active statuses, photo policies, and commission defaults.
2. `tenant_domains`: Hostnames, custom domains, DNS TXT verification records, and SSL/active states.
3. `tenant_memberships`: User-to-community membership bindings.
4. `users`: Global identity identities with normalized emails, mobile numbers, and active flags.
5. `account_credentials`: Argon2id password verifiers and credential records.
6. `auth_sessions`: Server-side authenticated sessions with sliding-window expiry and client IP/user-agent binding.
7. `roles`: Tenant and platform RBAC roles.
8. `permissions`: Platform-wide granular permission codes.
9. `role_permissions`: Association table mapping roles to catalog permissions.
10. `user_role_assignments`: Scoped user-to-role assignments.
11. `agent_profiles`: Agent metadata, encrypted Aadhaar/PAN, operating regions, commission rates.
12. `subscriber_profiles`: Subscriber metadata, agent references, encrypted identity records.
13. `regions`: Community operating regions and geographic bounds.
14. `user_region_assignments`: Agent-to-region assignment mappings.
15. `region_audit_logs`: Audit trail for region creation and modifications.
16. `commission_audit_logs`: Audit trail for commission percentage changes and authorizers.
17. `stored_objects`: Registration photos and documents stored in private R2 storage with category and tenant ownership.
18. `otp_challenges`: Salted SHA-256 OTP challenges with failed attempt counters and registration verification tokens.
19. `otp_rate_buckets`: Shared rate limiting buckets for OTP requests and verification attempts.
20. `audit_events`: Tamper-evident structured audit trail with actor, action, tenant, IP, and target metadata.
21. `audit_outbox_events`: Transactional outbox records with idempotency keys and delivery status states.
22. `audit_outbox_attempts`: Execution logs for outbox worker delivery attempts.

### Frontend MongoDB Remnants (Dual-Database Split)
The frontend retains legacy MongoDB integration via `src/lib/mongodb.ts` and `src/lib/repo.ts`. The `"mongodb": "^6.21.0"` package is installed in `package.json`.

#### Legacy Next.js API Routes Still Calling MongoDB:
- `src/app/api/activity/route.ts` (Super Admin activity feed)
- `src/app/api/auth/admin-login/route.ts` (Legacy admin login)
- `src/app/api/employee-profile/route.ts` (Legacy employee profile)
- `src/app/api/employees/[id]/route.ts` & `src/app/api/employees/route.ts` (Legacy employee CRUD)
- `src/app/api/packages/route.ts` (Subscription package lookup)
- `src/app/api/support/[id]/route.ts` & `src/app/api/support/route.ts` (Support tickets)
- `src/app/api/users/[id]/route.ts` & `src/app/api/users/route.ts` (Legacy user listing)

#### Frontend UI Components Directly Fetching Legacy MongoDB Routes:
- `src/app/employee/support/page.tsx` (`fetch('/api/support')`)
- `src/app/superadmin/activity/page.tsx` (`fetch('/api/activity')`)
- `src/components/EmployeeNavbar.tsx` (`fetch('/api/employee-profile')`)
- `src/components/ExistingUsersTab.tsx` (`fetch('/api/users')`)
- `src/components/Overview.tsx` (`fetch('/api/employees')`, `fetch('/api/users')`, `fetch('/api/activity')`)
- `src/components/SupportTicketsList.tsx` (`fetch('/api/support')`)

#### Risk Assessment
- **Reliability Risk:** Any user accessing the Super Admin Activity page, Support tabs, or Overview dashboards without an active MongoDB connection receives HTTP 500 errors.
- **Architectural Confusion:** Developers may mistakenly add features to legacy `/api/...` routes instead of the secure proxy `/backend-api/...` connected to PostgreSQL.
- **Data Inconsistency:** Users created or viewed in legacy MongoDB tables do not exist in PostgreSQL, leading to "ghost" records and authentication discrepancies.

---

## 4. Registration Photos & Object Storage Architecture

### Cloudflare R2 Storage Implementation
- **Implementation File:** `backend/app/modules/registration_photos/storage.py`
- **Adapter Type:** Production adapter `CloudflareR2Storage` uses `boto3.client("s3")` with Signature Version `s3v4`, pointing to `R2_ENDPOINT_URL`.
- **Private Bucket Guarantees:** Objects are uploaded strictly to private buckets; no public URLs are ever issued or exposed.
- **Presigned URLs:** `generate_presigned_url("get_object", ExpiresIn=300)` issues time-limited (5-minute TTL) signed URLs for authorized callers.
- **Authenticated Streaming Preview:** `GET /storage/{object_key}/preview` streams binary data directly from storage via FastAPI, bypassing presigned URLs while enforcing community tenant boundaries and caching headers (`Cache-Control: private, no-store, max-age=0`).

### Image Sanitization and Security Validation
- **Size Limit:** Enforced maximum of 5 MB (`MAX_IMAGE_BYTES = 5 * 1024 * 1024`).
- **Pixel & Dimension Bounds:** Maximum dimension of 4096 px (`MAX_IMAGE_DIMENSION = 4096`), maximum total pixels 16,777,216 (`MAX_IMAGE_PIXELS`).
- **Decompression Bomb Protection:** `warnings.simplefilter("error", Image.DecompressionBombWarning)` explicitly converts Pillow decompression warnings into fatal errors, rejecting malicious zip bombs.
- **Content Sanitization:** Rather than saving untrusted client bytes, images are loaded into Pillow, verified, converted to `RGB`/`RGBA`, and re-encoded from scratch (JPEG at quality 90 optimize, PNG optimize, WebP quality 90). This automatically strips EXIF metadata, GPS coordinates, camera serial numbers, and embedded payloads.
- **Object Key Isolation:** Object keys use cryptographically random hex UUIDs (`build_object_key(category, ext)`). Client filenames are never used in storage keys.
- **Allowed Categories:** Strictly restricted to `agent-photo`, `aadhaar-document`, `pan-document`, and `user-photo`.

### PII Protection & AES-256-GCM Encryption
- **Implementation File:** `backend/app/core/encryption.py`
- **Algorithm:** Authenticated AES-256-GCM via `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- **Nonce & Tag:** Generates a fresh 12-byte cryptographically random nonce (`os.urandom(12)`) per encryption operation. Prepend nonce to ciphertext and includes 16-byte authentication tag.
- **Aadhaar Format & Masking:** Validated against 12-digit regex (`^\d{12}$`). Masked output format: `XXXX-XXXX-1234`.
- **PAN Format & Masking:** Validated against 10-character alphanumeric regex (`^[A-Z]{5}[0-9]{4}[A-Z]{1}$`). Masked output format: `XXXXX1234A`.
- **Key Hierarchy:** Driven by `IDENTITY_ENCRYPTION_KEY`. Includes development fallback key with explicit safeguards.

---

## 5. RBAC & Administrative Boundaries

### Role Boundaries
- **Platform Super Admin (`SUPER_ADMIN`):** Unrestricted visibility across all communities. Can create communities, update community status (active/suspended), provision community administrators, configure global policies, and create operating regions.
- **Community Admin (`ADMIN`):** Strictly scoped to their designated `tenant_id`. Can create agents, update community-specific agent commission rates (within policy bounds), toggle subscriber photo policies, and inspect local subscribers. Cannot view or alter data of other communities.
- **Agent (`AGENT`):** Authenticated field worker restricted to their assigned operating regions and assigned community. Can register new subscribers and view their own subscriber roster.

### Architectural Discrepancy: Fine-Grained Catalog vs Coarse Role Checks
The Architecture document (Sections 7, 8, 10) specified a granular, capability-based RBAC system where endpoints verify registered permission codes (e.g., `agent.create`, `agent.approve`, `region.assign`) via `@require_permission`.

In practice:
1. `backend/app/modules/authorization/catalog.py` contains all 34 registered permissions.
2. However, the operational endpoints in `backend/app/modules/agents/api.py` rely primarily on hardcoded role dependencies:
   - `require_super_admin` (`resolved.role == "SUPER_ADMIN"`)
   - `require_admin_or_super` (`resolved.role in {"SUPER_ADMIN", "ADMIN"}`)
   - `require_agent` (`resolved.role == "AGENT"`)
3. Only the audit, authorization, and member identity endpoints currently use `@require_permission`.
4. Role delegation limits and custom role creation exist in data models but are not fully enforced in the operational agent workflows.

---

## 6. Registration Flows & OTP Verification

### Mobile OTP Lifecycle (`backend/app/modules/authentication/otp_service.py`)
1. **Phone Number Normalization:** `normalize_mobile_e164` strips punctuation and whitespace, enforcing strict E.164 compliance (`^\+[1-9][0-9]{7,14}$`).
2. **Cryptographic Generation:** Uses `secrets.randbelow(900000) + 100000` to generate a 6-digit cryptographically secure code.
3. **Storage Security:** The code is salted with 16 bytes of hex randomness (`secrets.token_hex(16)`) and hashed using SHA-256 before database insertion. Plaintext OTPs are never stored in the database or written to log files.
4. **Anti-Enumeration:** Requesting an OTP for an unknown mobile number returns generic HTTP 202 Accepted to prevent phone number enumeration attacks.
5. **Rate Limiting (PostgreSQL Shared State):**
   - Max 3 send requests per 10-minute sliding window.
   - 60-second cooldown between consecutive requests.
   - Max 5 failed verification attempts before challenge invalidation.
6. **Registration Verification Token:**
   - When OTP verification succeeds for registration purposes (`admin_registration`, `agent_registration`, `user_registration`), an opaque 32-byte URL-safe token is issued (`secrets.token_urlsafe(32)`).
   - Valid for exactly 15 minutes.
   - Single-use: Atomically consumed inside the registration transaction via `validate_and_consume_registration_token` (`consumed_at = now()`).
7. **Channels:** SMS text is the active delivery channel. Email is a development mock (`MOCK_SENT`). WhatsApp payload structures exist in code but require pre-approved templates on WhatsApp Cloud API.

---

## 7. Notification Microservice Integration Status

### Backend Integration Client (`backend/app/modules/messaging/notification_service.py`)
The backend provides a complete, robust client adhering to `docs/implementation/FRONTEND_INTEGRATION.md`:
- **Endpoint:** `POST {NOTIFICATION_SERVICE_BASE_URL}/api/v1/messages`
- **Headers:** `X-API-Key`, `Idempotency-Key` (max 200 chars, no PII), `Content-Type: application/json`.
- **Payload Schema:** Strictly matches documented specifications (`channel`, `messageType`, `recipient`, `content.text`, `expiresAt`, `metadata.purpose`, `metadata.challengeId`).
- **Status Endpoints:** Client implements `GET /api/v1/messages/{messageId}` and `GET /api/v1/messages/{messageId}/attempts`.

### Operational Reality & Blockers
1. **External Endpoint Resolution:** The configured base URL `http://handorg.duckdns.org/notifications` returns:
   - `GET /notifications` -> HTTP 400
   - `GET` & `POST /notifications/api/v1/messages` -> HTTP 403 Forbidden
   - `GET` & `POST /api/v1/messages` -> HTTP 400 `TENANT_NOT_FOUND`
   - HTTPS probes to the same domain time out.
2. **Local Environment:** `http://localhost:8100` is currently not running in this environment (no listening process on port 8100, Docker daemon is not active on this host).
3. **Operational Mode:** The backend is currently running in `mock` / `development_dummy` mode. All backend tests pass because they verify HTTP client contracts, mock message recordings, and fail-closed behaviors.
4. **Webhooks:** No webhook endpoints or webhook signature verification logic are implemented in either backend or frontend. Delivery tracking is handled via pull polling (`get_message_status`) or asynchronous outbox event records.

---

## 8. Automated Test & Build Status

### Backend Test Suite (`pytest backend/tests`)
- **Total Tests Collected:** 303
- **Passed:** **301**
- **Skipped:** **2**
  - `test_activation_postgres.py`: Skipped because `TEST_DATABASE_URL` is not set for isolated PostgreSQL tests.
  - `test_database_configuration.py`: Skipped because `TEST_DATABASE_URL` is not configured for external PostgreSQL connectivity verification.
- **Failures / Errors:** **0**
- **Execution Time:** ~85.6 seconds

### Frontend Test Suite (`npm run ...` in `frontend/`)
- **Type Checking (`tsc --noEmit`):** **Passed** (0 errors).
- **ESLint (`next lint`):** **Passed** (0 errors; 1 standard warning regarding custom fonts in `layout.tsx`).
- **Proxy Integration Suite (`npm run test:proxy`):** **18 passed**, 0 failed.
- **RBAC Suite (`npm run test:rbac`):** **6 passed**, 0 failed.
- **Community Management Suite (`npm run test:communities`):** **1 passed**, 0 failed.
- **Phase 5 Agent & Storage Suite (`npm run test:phase5`):** **1 passed**, 0 failed.
- **Mobile OTP Suite (`npm run test:otp`):** **4 passed**, 0 failed.
- **MongoDB Compatibility Suite (`npm run test:mongodb`):** **3 passed**, 0 failed.
- **Total Frontend Tests:** **33 passed**, 0 failed.

### Repository Anomaly Identified
During root test discovery, pytest reported 22 collection errors due to duplicate directory structures:
- `D:\AVANFlix\OTT_Agent_App\backend` vs `D:\AVANFlix\OTT_Agent_App\back-end`
- `D:\AVANFlix\OTT_Agent_App\frontend` vs `D:\AVANFlix\OTT_Agent_App\front-end`
Running pytest explicitly inside `backend/tests` avoids this collision and executes cleanly. The `back-end` and `front-end` directories are duplicate backup copies created on October 17, 2026.

---

## 9. Requirement-by-Requirement Status Matrix

| ID | Requirement Domain | Baseline Section | Implementation Status | Technical Evidence & Concrete Justification |
|:---:|---|:---:|:---:|---|
| **REQ-01** | **Multi-Tenant Architecture & Domain Resolution** | Arch §3, §4 | `IMPLEMENTED_AND_VERIFIED` | `tenants`, `tenant_domains` models; `hostname.py` IDN punycode normalization; DNS TXT record verification in `onboarding/dns_verification.py`. Verified by 22 passing tests in `test_tenant_resolution.py`. |
| **REQ-02** | **Identity, Authentication & Sessions** | Arch §5 | `IMPLEMENTED_AND_VERIFIED` | Argon2id password hashing, secure session cookies with sliding window expiry, account lockout on consecutive failures, CSRF tokens. Verified by 28 passing tests in `test_password_authentication.py` and `test_authentication_api.py`. |
| **REQ-03** | **Mobile OTP Login & Verification** | Arch §5, Guide §4 | `IMPLEMENTED_AND_VERIFIED` (Dev/Test) / `BLOCKED_BY_EXTERNAL_DEPENDENCY` (Live) | 6-digit secure generation, salted SHA-256 verifiers, constant-time verification, PostgreSQL shared rate buckets (3/10m, 60s cooldown, 5 attempts). Verified by `test_otp_login_flow.py` and frontend `test:otp`. Live SMS delivery blocked by external notification microservice tenant routing. |
| **REQ-04** | **Granular RBAC & Permission Catalog** | Arch §7, §8 | `PARTIALLY_IMPLEMENTED` | Full 34-permission catalog defined in `authorization/catalog.py` with `@require_permission` dependency. However, `agents/api.py` operational routes use coarse role checks (`require_admin_or_super`) rather than catalog permissions, and role delegation limits are not enforced. |
| **REQ-05** | **Super Admin Platform Operations** | Arch §6 | `IMPLEMENTED_AND_VERIFIED` | Platform tenant provisioning, community activation/suspension, administrator assignment, global commission caps, and OIDC operator authentication. Verified in `test_platform_communities.py`. |
| **REQ-06** | **Community Administrator Management** | Arch §6, §10 | `IMPLEMENTED_AND_VERIFIED` | Scoped agent listing, community-specific commission overrides (capped by platform policy), photo enforcement policy toggles, tenant-scoped subscriber inspection. Verified in Phase 4 & Phase 5 test suites. |
| **REQ-07** | **Agent Appointment & Approval Workflow** | Arch §10 | `PARTIALLY_IMPLEMENTED` | Direct agent creation with auto-credentials, regional assignment, and profile management is working. However, the 2-step `PENDING` appointment workflow (`agent_appointments` and `approval_workflows` modules) is completely bypassed. |
| **REQ-08** | **Member/Subscriber Registration & Hierarchy** | Arch §9 | `PARTIALLY_IMPLEMENTED` | Subscriber registration with OTP registration tokens, masked PII, and photo binding is fully functional in `agents/api.py`. However, `member_relationships` table, descendant tree queries, and the dedicated `members` module are empty stubs. |
| **REQ-09** | **Geographic Operating Scopes & Regions** | Arch §11 | `IMPLEMENTED_AND_VERIFIED` | `Region`, `UserRegionAssignment`, and `RegionAuditLog` models in PostgreSQL. Region creation restricted to Super Admin; agent assignment enforced within tenant scope. Verified in `test_phase5_agent_user_storage.py`. |
| **REQ-10** | **Registration Photos & Private Storage (R2)** | Arch §13 | `IMPLEMENTED_NOT_FULLY_VERIFIED` | Pillow validation, 5MB limit, 4096px bounds, decompression bomb protection, image re-encoding, presigned URLs (300s TTL), authenticated streaming preview, tenant isolation. Verified with in-memory mock; live Cloudflare R2 bucket connection unverified due to missing cloud credentials. |
| **REQ-11** | **PII Protection & AES-256-GCM Encryption** | Arch §12 | `IMPLEMENTED_AND_VERIFIED` | Authenticated AES-256-GCM encryption with 12-byte random nonce for Aadhaar and PAN in `core/encryption.py`. Regex validation and masking (`XXXX-XXXX-1234`, `XXXXX1234A`) verified in unit tests. |
| **REQ-12** | **Notification Microservice Integration** | Guide §3, §4 | `BLOCKED_BY_EXTERNAL_DEPENDENCY` | Complete HTTP client in `messaging/notification_service.py` with `X-API-Key`, `Idempotency-Key`, retry, and error classification. Blocked at runtime because external endpoint `handorg.duckdns.org` returns HTTP 403 / `TENANT_NOT_FOUND` and `localhost:8100` is offline. |
| **REQ-13** | **Audit Logging & Transactional Outbox** | Arch §15 | `IMPLEMENTED_AND_VERIFIED` | Structured `audit_events` logged atomically with business transactions. `audit_outbox_events` and attempts implement transactional outbox with worker lease recovery and idempotency keys. Verified in `test_security_foundations.py`. |
| **REQ-14** | **Frontend Next.js Architecture & Proxy** | Arch §16 | `PARTIALLY_IMPLEMENTED` | Modern Next.js 14 App Router with secure `/backend-api/[...path]` proxy forwarding session cookies and headers to FastAPI. 18 proxy tests pass. However, 7 legacy API routes and multiple UI components still make direct calls to MongoDB. |

---

## 10. Recommendations & Next Phase Roadmap

### Priority 1: Clean Up Duplicate Directories & Test Discovery
- **Action:** Safely archive or remove duplicate `back-end` and `front-end` directories in the workspace root to prevent module name collision during test runs and avoid developer confusion.

### Priority 2: Resolve External Notification Microservice Deployment
- **Action:** Coordinate with notification microservice operations to:
  1. Provision the OTT platform tenant and configure the corresponding `NOTIFICATION_SERVICE_API_KEY`.
  2. Fix reverse-proxy routing for `/notifications/api/v1/messages` on `handorg.duckdns.org` or deploy a local Docker Compose notification container on `localhost:8100`.
  3. Verify live SMS delivery end-to-end using an authorized test phone number.

### Priority 3: Eliminate Legacy Frontend MongoDB Remnants
- **Action:** Migrate remaining legacy routes (`/api/activity`, `/api/support`, `/api/employees`, `/api/users`) to FastAPI PostgreSQL endpoints, update UI components to use `/backend-api/...`, and remove the `mongodb` npm dependency from `package.json`.

### Priority 4: Align RBAC with Capability Catalog & Approval Workflows
- **Action:** Replace hardcoded role checks in `agents/api.py` with `@require_permission(PermissionCode.AGENT_CREATE)`, etc., and implement the `agent_appointments` review queue to satisfy Section 10 of the baseline architecture.

### Priority 5: Implement Member Relationships & Descendant Trees
- **Action:** Implement the `member_relationships` table and hierarchy query endpoints (`member.stats.read`, `member.descendants.read`) in the dedicated `members` module to enable hierarchical agent/subscriber networks.

