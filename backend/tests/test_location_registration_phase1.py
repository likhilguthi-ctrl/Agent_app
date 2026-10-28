"""Tests for Phase 1: Location-Based Admin and Agent Registration.

Validates:
1. Canonical location model (26 districts in AP, 33 districts in TS)
2. State -> District hierarchy and mismatch rejection
3. Super Admin creates Administrator with immediate activation & location assignment
4. Masked Aadhaar and PAN in responses
5. Admin location scoping: Admin can only view and assign their authorized states & districts
6. Agent registration creates credentials in pending state with AgentAppointment
7. Many-to-many district assignments
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.modules.agents.models import AgentProfile
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.authentication.models import AccountCredential, OtpChallenge, EmailOtpChallenge
from app.modules.authorization.models import UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.tenants.locations import CANONICAL_LOCATIONS, SUPPORTED_STATES, seed_canonical_regions_for_tenant
from app.modules.tenants.models import AdminProfile, Region, Tenant, UserRegionAssignment
from tests.test_agent_appointments_and_rbac import appointment_harness, client_for, upload_test_object


@pytest.mark.anyio
async def test_canonical_location_counts_and_hierarchy(appointment_harness):
    """Verify canonical states and district counts in PostgreSQL on a clean tenant."""
    app = appointment_harness["app"]
    tenant_id = appointment_harness["tenant_b_id"]
    cookies = {"community_session": appointment_harness["super_token"]}

    # Seed canonical regions on tenant_b (which has no existing regions)
    with appointment_harness["factory"]() as s:
        seed_canonical_regions_for_tenant(s, UUID(tenant_id))
        s.commit()

    async with client_for(app) as client:
        # 1. Query states for community
        states_res = await client.get(
            f"/api/v1/platform/communities/{tenant_id}/states",
            cookies=cookies,
        )
        assert states_res.status_code == 200
        states_data = states_res.json()
        assert set(states_data["items"]) == {"Andhra Pradesh", "Telangana"}
        assert states_data["total"] == 2

        # 2. Query AP regions (exactly 26 districts)
        ap_res = await client.get(
            f"/api/v1/platform/communities/{tenant_id}/regions?state=Andhra+Pradesh",
            cookies=cookies,
        )
        assert ap_res.status_code == 200
        ap_data = ap_res.json()
        assert ap_data["total"] == 26
        for item in ap_data["items"]:
            assert item["state"] == "Andhra Pradesh"
            assert item["code"].startswith("AP-")

        # 3. Query TS regions (exactly 33 districts)
        ts_res = await client.get(
            f"/api/v1/platform/communities/{tenant_id}/regions?state=Telangana",
            cookies=cookies,
        )
        assert ts_res.status_code == 200
        ts_data = ts_res.json()
        assert ts_data["total"] == 33
        for item in ts_data["items"]:
            assert item["state"] == "Telangana"
            assert item["code"].startswith("TS-")


@pytest.mark.anyio
async def test_superadmin_creates_administrator_immediate_activation(appointment_harness):
    """Super Admin creates an administrator with state, districts, password; immediately active."""
    app = appointment_harness["app"]
    tenant_id = appointment_harness["tenant_a_id"]
    cookies = {"community_session": appointment_harness["super_token"]}
    headers = {
        "Origin": "http://localhost:3000",
        "X-CSRF-Token": appointment_harness["super_csrf"],
    }

    now = datetime.now(UTC)
    mobile_token = "satish-mobile-token"
    email_token = "satish-email-token"

    with appointment_harness["factory"]() as s:
        seed_canonical_regions_for_tenant(s, UUID(tenant_id))
        ap_regions = s.execute(
            select(Region).where(Region.tenant_id == UUID(tenant_id), Region.state == "Andhra Pradesh")
        ).scalars().all()
        selected_region_ids = [str(ap_regions[0].id), str(ap_regions[1].id)]
        s.add(
            OtpChallenge(
                id=uuid4(),
                tenant_id=UUID(tenant_id),
                mobile_e164="+919876500001",
                purpose="admin_registration",
                otp_hash="hash",
                otp_salt="salt",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                verified_at=now,
                verification_token=mobile_token,
            )
        )
        s.add(
            EmailOtpChallenge(
                id=uuid4(),
                tenant_id=UUID(tenant_id),
                email="satish@avanflix.test",
                purpose="email_verification",
                otp_hash="hash",
                otp_salt="salt",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                verified_at=now,
                verification_token=email_token,
            )
        )
        s.commit()

    admin_payload = {
        "first_name": "Satish",
        "last_name": "Kumar",
        "username": "satish_admin",
        "password": "AdminPassword123!",
        "mobile_e164": "+919876500001",
        "email": "satish@avanflix.test",
        "verification_token": mobile_token,
        "email_verification_token": email_token,
        "address": "Vijayawada Central",
        "state": "Andhra Pradesh",
        "region_ids": selected_region_ids,
        "aadhaar_number": "999988887777",
        "pan_number": "ABCDE1234F",
        "status": "active",
    }

    async with client_for(app) as client:
        res = await client.post(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            json=admin_payload,
            cookies=cookies,
            headers=headers,
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["username"] == "satish_admin"
        assert data["status"] == "active"
        assert data["role"] == "ADMIN"
        assert data["state"] == "Andhra Pradesh"
        assert data["aadhaar_number_masked"] == "XXXX-XXXX-7777"
        assert data["pan_number_masked"] == "XXXXX1234F"
        assert len(data["assigned_region_ids"]) == 2

    # Check database records are all active
    admin_user_id = UUID(data["user_id"])
    with appointment_harness["factory"]() as s:
        user = s.execute(select(User).where(User.id == admin_user_id)).scalar_one()
        assert user.status == "active"

        cred = s.execute(select(AccountCredential).where(AccountCredential.user_id == admin_user_id)).scalar_one()
        assert cred.status == "active"
        assert cred.password_hash != "AdminPassword123!"

        mem = s.execute(select(TenantMembership).where(TenantMembership.user_id == admin_user_id)).scalar_one()
        assert mem.status == "active"

        prof = s.execute(select(AdminProfile).where(AdminProfile.user_id == admin_user_id)).scalar_one()
        assert prof.status == "active"
        assert prof.first_name == "Satish"
        assert prof.last_name == "Kumar"

        # UserRegionAssignment must be active
        assignments = s.execute(
            select(UserRegionAssignment).where(
                UserRegionAssignment.user_id == admin_user_id,
                UserRegionAssignment.status == "active",
            )
        ).scalars().all()
        assert len(assignments) == 2

        # Verify NO AgentAppointment was created for administrator
        apt = s.execute(
            select(AgentAppointment).where(AgentAppointment.user_id == admin_user_id)
        ).scalar_one_or_none()
        assert apt is None


@pytest.mark.anyio
async def test_district_state_mismatch_rejected(appointment_harness):
    """Submitting a district from Telangana when state is Andhra Pradesh must be rejected."""
    app = appointment_harness["app"]
    tenant_id = appointment_harness["tenant_a_id"]
    cookies = {"community_session": appointment_harness["super_token"]}
    headers = {
        "Origin": "http://localhost:3000",
        "X-CSRF-Token": appointment_harness["super_csrf"],
    }

    with appointment_harness["factory"]() as s:
        seed_canonical_regions_for_tenant(s, UUID(tenant_id))
        ts_region = s.execute(
            select(Region).where(Region.tenant_id == UUID(tenant_id), Region.state == "Telangana")
        ).scalars().first()
        ts_region_id = str(ts_region.id)
        s.commit()

    # Try creating Admin with state=Andhra Pradesh but TS district
    mismatch_payload = {
        "username": "mismatch_admin",
        "password": "Password123!",
        "mobile_e164": "+919876500002",
        "email": "mismatch@avanflix.test",
        "verification_token": "dummy-token",
        "email_verification_token": "dummy-token",
        "state": "Andhra Pradesh",
        "region_ids": [ts_region_id],
    }

    async with client_for(app) as client:
        res = await client.post(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            json=mismatch_payload,
            cookies=cookies,
            headers=headers,
        )
        assert res.status_code == 400
        err = res.json()
        assert "DISTRICT_STATE_MISMATCH" in str(err) or "does not belong" in str(err)


@pytest.mark.anyio
async def test_admin_location_scoping_for_agent_creation(appointment_harness):
    """Admin can only view assigned states/districts, and assign only authorized districts to agents."""
    app = appointment_harness["app"]
    tenant_id = appointment_harness["tenant_a_id"]
    admin_token = appointment_harness["admin_a1_token"]
    admin_user_id = UUID(appointment_harness["admin_a1_user_id"])
    admin_cookies = {"community_session": admin_token}
    admin_headers = {
        "Origin": "http://localhost:3000",
        "X-CSRF-Token": appointment_harness["admin_a1_csrf"],
    }

    now = datetime.now(UTC)
    unauth_email_token = "ramu-unauth-email-token"
    valid_email_token = "ramu-valid-email-token"

    with appointment_harness["factory"]() as s:
        seed_canonical_regions_for_tenant(s, UUID(tenant_id))
        ap_regions = s.execute(
            select(Region).where(Region.tenant_id == UUID(tenant_id), Region.state == "Andhra Pradesh")
        ).scalars().all()
        ts_regions = s.execute(
            select(Region).where(Region.tenant_id == UUID(tenant_id), Region.state == "Telangana")
        ).scalars().all()

        # Assign Admin A1 ONLY to AP district 0
        s.execute(
            delete(UserRegionAssignment).where(
                UserRegionAssignment.user_id == admin_user_id,
                UserRegionAssignment.tenant_id == UUID(tenant_id),
            )
        )
        s.add(
            UserRegionAssignment(
                id=uuid4(),
                user_id=admin_user_id,
                tenant_id=UUID(tenant_id),
                region_id=ap_regions[0].id,
                status="active",
                assigned_by_user_id=UUID(appointment_harness["admin_a2_user_id"]),
            )
        )
        s.add(
            EmailOtpChallenge(
                id=uuid4(),
                tenant_id=UUID(tenant_id),
                email="ramu@avanflix.test",
                purpose="email_verification",
                otp_hash="hash",
                otp_salt="salt",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                verified_at=now,
                verification_token=unauth_email_token,
            )
        )
        s.add(
            EmailOtpChallenge(
                id=uuid4(),
                tenant_id=UUID(tenant_id),
                email="ramu_valid@avanflix.test",
                purpose="email_verification",
                otp_hash="hash",
                otp_salt="salt",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                verified_at=now,
                verification_token=valid_email_token,
            )
        )
        s.commit()
        assigned_ap_id = str(ap_regions[0].id)
        assigned_ap_name = ap_regions[0].name
        unassigned_ap_id = str(ap_regions[1].id)
        unassigned_ap_name = ap_regions[1].name

    async with client_for(app) as client:
        # 1. Admin GET /api/v1/agents/states -> returns only ["Andhra Pradesh"]
        states_res = await client.get("/api/v1/agents/states", cookies=admin_cookies)
        assert states_res.status_code == 200
        states_data = states_res.json()
        assert states_data["items"] == ["Andhra Pradesh"]

        # 2. Admin GET /api/v1/agents/regions -> returns only assigned region
        reg_res = await client.get("/api/v1/agents/regions", cookies=admin_cookies)
        assert reg_res.status_code == 200
        reg_data = reg_res.json()
        assert reg_data["total"] == 1
        assert reg_data["items"][0]["id"] == assigned_ap_id

        # 3. Admin attempts to assign unassigned AP region -> 403 REGION_ASSIGNMENT_UNAUTHORIZED
        photo_key = await upload_test_object(client, admin_cookies, "profile_photo")
        aadhaar_key = await upload_test_object(client, admin_cookies, "aadhaar")
        pan_key = await upload_test_object(client, admin_cookies, "pan")

        unauth_payload = {
            "first_name": "Ramu",
            "last_name": "Agent",
            "username": "ramu_agent_unauth",
            "password": "AgentPassword123!",
            "mobile_e164": "+919876500003",
            "email": "ramu@avanflix.test",
            "email_verification_token": unauth_email_token,
            "state": "Andhra Pradesh",
            "district": unassigned_ap_name,
            "region_ids": [unassigned_ap_id],
            "aadhaar_number": "123412341234",
            "aadhaar_document_key": aadhaar_key,
            "pan_number": "ABCDE1234F",
            "pan_document_key": pan_key,
            "profile_photo_key": photo_key,
        }
        unauth_res = await client.post("/api/v1/agents", json=unauth_payload, cookies=admin_cookies, headers=admin_headers)
        assert unauth_res.status_code == 403
        assert "REGION_ASSIGNMENT_UNAUTHORIZED" in str(unauth_res.json()) or "cannot assign" in str(unauth_res.json())

        # 4. Admin assigns their authorized district -> 201 Created in pending state
        valid_payload = dict(unauth_payload)
        valid_payload["username"] = "ramu_agent_valid"
        valid_payload["mobile_e164"] = "+919876500004"
        valid_payload["email"] = "ramu_valid@avanflix.test"
        valid_payload["email_verification_token"] = valid_email_token
        valid_payload["district"] = assigned_ap_name
        valid_payload["region_ids"] = [assigned_ap_id]

        valid_res = await client.post("/api/v1/agents", json=valid_payload, cookies=admin_cookies, headers=admin_headers)
        assert valid_res.status_code == 201, valid_res.text
        agent_data = valid_res.json()
        assert agent_data["username"] == "ramu_agent_valid"
        assert agent_data["status"] == "pending"
        assert agent_data["state"] == "Andhra Pradesh"
        assert agent_data["assigned_region_ids"] == [assigned_ap_id]

    # Verify agent credentials created in inactive state awaiting dual-custody approval
    agent_user_id = UUID(agent_data["user_id"])
    with appointment_harness["factory"]() as s:
        cred = s.execute(select(AccountCredential).where(AccountCredential.user_id == agent_user_id)).scalar_one()
        assert cred.status == "inactive"

        user = s.execute(select(User).where(User.id == agent_user_id)).scalar_one()
        assert user.status == "inactive"

        apt = s.execute(
            select(AgentAppointment).where(AgentAppointment.user_id == agent_user_id)
        ).scalar_one()
        assert apt.status == "PENDING"


@pytest.mark.anyio
async def test_password_policy_and_identity_validation(appointment_harness):
    """Verify password length and identity validation errors."""
    app = appointment_harness["app"]
    tenant_id = appointment_harness["tenant_a_id"]
    cookies = {"community_session": appointment_harness["super_token"]}
    headers = {
        "Origin": "http://localhost:3000",
        "X-CSRF-Token": appointment_harness["super_csrf"],
    }

    async with client_for(app) as client:
        # Short password (< 8 chars)
        res = await client.post(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            json={
                "username": "short_pwd_admin",
                "password": "short",
                "mobile_e164": "+919876500005",
                "email": "short_pwd@avanflix.test",
                "state": "Andhra Pradesh",
            },
            cookies=cookies,
            headers=headers,
        )
        assert res.status_code == 422
        assert "Password must be at least 8 characters long" in str(res.json())

        # Invalid Aadhaar format (not 12 digits)
        res = await client.post(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            json={
                "username": "bad_aadhaar_admin",
                "password": "ValidPassword123!",
                "mobile_e164": "+919876500006",
                "email": "bad_aadhaar@avanflix.test",
                "state": "Andhra Pradesh",
                "aadhaar_number": "123",
            },
            cookies=cookies,
            headers=headers,
        )
        assert res.status_code == 422

        # Invalid PAN format
        res = await client.post(
            f"/api/v1/platform/communities/{tenant_id}/administrators",
            json={
                "username": "bad_pan_admin",
                "password": "ValidPassword123!",
                "mobile_e164": "+919876500007",
                "email": "bad_pan@avanflix.test",
                "state": "Andhra Pradesh",
                "aadhaar_number": "123456789012",
                "pan_number": "INVALIDPAN",
            },
            cookies=cookies,
            headers=headers,
        )
        assert res.status_code == 422
