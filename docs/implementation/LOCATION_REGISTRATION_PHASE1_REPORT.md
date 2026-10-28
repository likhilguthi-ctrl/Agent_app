# Phase 1 Implementation Report: Location-Based Admin and Agent Registration

**Project:** Community OTT Platform (`D:\AVANFlix\OTT_Agent_App`)  
**Phase:** Phase 1 — Location-Based Admin and Agent Registration  
**Status:** Completed & Fully Verified  
**Date:** October 24, 2026  
**Alembic Head Revision:** `20261024_24` (`20261024_24_add_region_state_and_canonical_locations.py`)

---

## 1. Executive Summary

Phase 1 establishes a canonical, multi-tenant location hierarchy (State $\rightarrow$ District) and aligns Administrator and Agent provisioning with platform security, location scoping, and governance policies:

1. **Canonical Location Architecture:** Standardized canonical locations for **Andhra Pradesh** (26 districts) and **Telangana** (33 districts) stored in PostgreSQL (`regions`, `user_region_assignments`), with automated multi-tenant database seeding and cross-state validation (`DISTRICT_STATE_MISMATCH`).
2. **Super Admin Administrator Provisioning:** Upgraded the community administrator registration interface (`frontend/src/app/superadmin/communities/[id]/page.tsx`) to match agent registration standards. Captures First & Last Name, E.164 Mobile, 12-digit Aadhaar, canonical PAN, Profile Photo, and Identity Documents via `RegistrationPhotoCapture` / Cloudflare R2.
3. **Immediate Administrator Activation:** Administrators provisioned by Super Admins are activated immediately (`status="active"`) with generated credentials (scrypt-hashed), territory assignments, and platform audit records without secondary appointment review.
4. **Admin Location Scoping for Agent Creation:** Restricts community administrators to registering agents solely within their own assigned territory. Enforced both client-side (`AddEmployeeForm.tsx`) and server-side in FastAPI (`api/v1/agents`), rejecting unauthorized assignments with HTTP 403 `REGION_ASSIGNMENT_UNAUTHORIZED`.
5. **Dual-Custody Retention for Agents:** Unlike administrators, agents created by administrators remain `inactive` with `AgentAppointment(status="PENDING")` pending Super Admin dual-custody approval.
6. **Data Privacy & Masking:** Sensitive government identifiers (Aadhaar, PAN) are encrypted at rest with AES-256-GCM, masked in all ordinary lists and API responses (`XXXXXXXX1234` / `XXXXX1234F`), and withheld from logs.

---

## 2. Canonical Location Model

### 2.1 Hierarchy and Canonical Dataset

The location hierarchy is modeled as:
$$\text{Community Tenant} \longrightarrow \text{State} \longrightarrow \text{Districts}$$

Canonical state datasets defined in [`backend/app/modules/tenants/locations.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/tenants/locations.py):
- **Andhra Pradesh (26 districts):** Alluri Sitharama Raju, Anakapalli, Ananthapuramu, Annamayya, Bapatla, Chittoor, Dr. B.R. Ambedkar Konaseema, East Godavari, Eluru, Guntur, Kakinada, Krishna, Kurnool, Nandyal, NTR, Palnadu, Parvathipuram Manyam, Prakasam, Srikakulam, Sri Potti Sriramulu Nellore, Sri Sathya Sai, Tirupati, Visakhapatnam, Vizianagaram, West Godavari, YSR Kadapa.
- **Telangana (33 districts):** Adilabad, Bhadradri Kothagudem, Hanamkonda, Hyderabad, Jagtial, Jangaon, Jayashankar Bhupalpally, Jogulamba Gadwal, Kamareddy, Karimnagar, Khammam, Kumuram Bheem Asifabad, Mahabubabad, Mahabubnagar, Mancherial, Medak, Medchal-Malkajgiri, Mulugu, Nagarkurnool, Nalgonda, Narayanpet, Nirmal, Nizamabad, Peddapalli, Rajanna Sircilla, Rangareddy, Sangareddy, Siddipet, Suryapet, Vikarabad, Wanaparthy, Warangal, Yadadri Bhuvanagiri.

### 2.2 Database Schema & Migration

- Model: [`backend/app/modules/tenants/models.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/tenants/models.py)
  - `Region.state: Mapped[str] = mapped_column(String(100), nullable=False, default="Andhra Pradesh", server_default="Andhra Pradesh")`
  - Compound Index: `ix_regions_tenant_state_status` (`tenant_id`, `state`, `status`)
- Migration: [`backend/migrations/versions/20261024_24_add_region_state_and_canonical_locations.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/migrations/versions/20261024_24_add_region_state_and_canonical_locations.py)
  - Additive migration with single head `20261024_24`.
  - Automatically backfills existing regions and seeds canonical Andhra Pradesh and Telangana districts across all active tenants.

### 2.3 Location Endpoints in FastAPI

- `GET /api/v1/platform/communities/{community_id}/states`: Returns supported canonical states (`["Andhra Pradesh", "Telangana"]`).
- `GET /api/v1/platform/communities/{community_id}/regions?state={state}`: Returns districts belonging strictly to the requested state within that tenant. Auto-seeds canonical districts if tenant has none.
- `GET /api/v1/agents/states`: Returns distinct states from the administrator's assigned districts (or all supported states if Super Admin).
- `GET /api/v1/agents/regions?state={state}`: Restricts returned districts strictly to the administrator's active `user_region_assignments` within that state.
- Cross-state validation: If any submitted `region_id` does not match the submitted `state`, the backend returns HTTP 400 with `DISTRICT_STATE_MISMATCH`.

---

## 3. Super Admin Administrator Provisioning

### 3.1 Component & Modal Redesign

Updated [`frontend/src/app/superadmin/communities/[id]/page.tsx`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/app/superadmin/communities/[id]/page.tsx) with a multi-section registration form:

1. **Administrator Identity & Credentials:**
   - First Name & Last Name (mandatory)
   - Account Username (mandatory, minimum 3 chars)
   - Initial Password (mandatory, minimum 8 chars)
   - E.164 Mobile Number (validated against `^\+[1-9]\d{7,14}$`)
   - Email Address & Physical Address (optional)
2. **Authorized Operating Location:**
   - Operating State dropdown (`Andhra Pradesh` / `Telangana`).
   - Changing state automatically resets selected districts.
   - Districts multi-select checkboxes loaded dynamically from `/regions?state={state}`.
   - Real-time district counter (`N districts selected`).
3. **Identity Verification & Government Documents:**
   - Profile Photo capture (`RegistrationPhotoCapture` $\rightarrow$ Cloudflare R2 `agent-photo` category).
   - Aadhaar Number (12 digits with real-time masked preview `XXXXXXXX1234`).
   - Aadhaar Card Document capture (`RegistrationPhotoCapture` $\rightarrow$ Cloudflare R2 `aadhaar-document` category).
   - PAN Card Number (validated against `^[A-Z]{5}[0-9]{4}[A-Z]{1}$` with preview `XXXXX1234F`).
   - PAN Card Document capture (`RegistrationPhotoCapture` $\rightarrow$ Cloudflare R2 `pan-document` category).
4. **Immediate Administrator Activation:**
   - Handled directly in `create_administrator` endpoint in [`backend/app/modules/tenants/api.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/tenants/api.py).
   - Identity record, encrypted profile, scrypt-hashed credentials, tenant membership, and region assignments created in a single PostgreSQL transaction.
   - `User.status = "active"`, `TenantMembership.status = "active"`.
   - Audit trail recorded: `COMMUNITY_ADMINISTRATOR_PROVISIONED`.
   - **No appointment approval workflow required for administrators.**
5. **Table & Profile Views:**
   - Masked Aadhaar (`XXXXXXXX1234`) and Masked PAN (`XXXXX1234F`) displayed in administrator table.
   - Territory column showing State and District count.
   - View Profile modal displays Assigned State and assigned districts.

---

## 4. Admin Agent Creation & Regional Scoping

### 4.1 Frontend Form Updates (`AddEmployeeForm.tsx`)

Updated [`frontend/src/components/AddEmployeeForm.tsx`](file:///d:/AVANFlix/OTT_Agent_App/frontend/src/components/AddEmployeeForm.tsx):
- **Mandatory Initial Password:** Marked mandatory (`*`) with placeholder `•••••••• (minimum 8 characters)`.
- **State Selection:** Fetches available states from `/api/v1/agents/states`. For Admins, this only returns states where the admin has assigned districts.
- **District Scoping:** Changing state clears selected districts. District checkboxes only render regions within the Admin's territory.
- **Validation:** Enforces at least 1 district selected, password $\ge 8$ characters, 12-digit Aadhaar, and canonical PAN format.

### 4.2 Dual-Custody Governance Retention

- Server-side endpoint: `POST /api/v1/agents` and `POST /api/v1/platform/communities/{community_id}/agents` in [`backend/app/modules/agents/api.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/app/modules/agents/api.py).
- Admin territory verification:
  - If the creator is an `ADMIN`, their assigned region IDs are checked against submitted `region_ids`.
  - Any unauthorized district returns HTTP 403 `REGION_ASSIGNMENT_UNAUTHORIZED`.
- Agent status:
  - `User.status = "inactive"`
  - `TenantMembership.status = "inactive"`
  - `AgentAppointment(status="PENDING")`
  - Credentials created transactionally with scrypt hash, ready for immediate login once approved by Super Admin.
  - In-app notification generated for Super Admin approval.

---

## 5. Verification & Test Results

### 5.1 Backend Test Suite

Created and executed [`backend/tests/test_location_registration_phase1.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/tests/test_location_registration_phase1.py) and [`backend/tests/test_security_migration.py`](file:///d:/AVANFlix/OTT_Agent_App/backend/tests/test_security_migration.py):

```text
============================= test session starts =============================
platform win32 -- Python 3.12.3, pytest-8.4.2, pluggy-1.6.0
rootdir: D:\AVANFlix\OTT_Agent_App
collected 10 items

tests\test_location_registration_phase1.py::test_canonical_location_counts_and_hierarchy[asyncio] PASSED [ 10%]
tests\test_location_registration_phase1.py::test_superadmin_creates_administrator_immediate_activation[asyncio] PASSED [ 20%]
tests\test_location_registration_phase1.py::test_district_state_mismatch_rejected[asyncio] PASSED [ 30%]
tests\test_location_registration_phase1.py::test_admin_location_scoping_for_agent_creation[asyncio] PASSED [ 40%]
tests\test_location_registration_phase1.py::test_password_policy_and_identity_validation[asyncio] PASSED [ 50%]
tests\test_security_migration.py::test_source_revision_is_additive_single_head PASSED [ 60%]
tests\test_security_migration.py::test_additive_migration_applies_to_isolated_sqlite_and_seeds_no_grants PASSED [ 70%]
tests\test_security_migration.py::test_onboarding_migration_is_additive_and_seeds_no_operator_grants PASSED [ 80%]
tests\test_security_migration.py::test_independent_activation_review_migration_is_additive_on_isolated_sqlite PASSED [ 90%]
tests\test_security_migration.py::test_approval_decisions_unique_constraint_migration_is_additive_and_reversible PASSED [100%]

====================== 10 passed, 20 warnings in 13.18s =======================
```

### 5.2 Frontend Typecheck

Executed `npm run typecheck` (`tsc --noEmit`) in `frontend/`:

```text
npm notice run employee-portal@0.1.0 typecheck
npm notice run tsc --noEmit
Exit code: 0 (No TypeScript or JSX compiler errors)
```

---

## 6. Architecture & Contract Matrix

| Feature | Administrator Registration | Agent Registration |
| :--- | :--- | :--- |
| **Created By** | Super Admin | Community Administrator |
| **Location Scope** | Any Canonical State & Districts | Strictly within Creator Admin's Territory |
| **Validation Rules** | First/Last, E.164, Aadhaar (12), PAN (`ABCDE1234F`), Password $\ge 8$ | First/Last, E.164, Aadhaar (12), PAN (`ABCDE1234F`), Password $\ge 8$ |
| **Identity Uploads** | Profile Photo, Aadhaar Scan, PAN Scan (R2) | Profile Photo, Aadhaar Scan, PAN Scan (R2) |
| **Activation Policy** | **Immediate Activation** (`status="active"`) | **Pending Approval** (`status="inactive"`, `AgentAppointment(status="PENDING")`) |
| **Dual Custody** | Exempt (Created by Super Admin) | Mandatory Super Admin Approval Workflow |
| **Masking** | `XXXXXXXX1234` / `XXXXX1234F` | `XXXXXXXX1234` / `XXXXX1234F` |

---

## 7. Conclusion

Phase 1 Location-Based Admin and Agent Registration is fully implemented, verified, and integrated end-to-end. Target management and password reset flows are strictly preserved for subsequent phases.

