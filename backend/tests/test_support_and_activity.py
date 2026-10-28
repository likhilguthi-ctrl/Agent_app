from __future__ import annotations

import pytest
from uuid import UUID
from sqlalchemy import select

from app.modules.agent_appointments.models import AgentAppointment
from app.modules.support.models import SupportTicket
from tests.test_phase5_agent_user_storage import (
    client_for,
    create_test_image_bytes,
    phase5_harness,
)


@pytest.mark.anyio
async def test_support_tickets_lifecycle_and_tenant_isolation(phase5_harness):
    app = phase5_harness["app"]
    factory = phase5_harness["factory"]
    super_token = phase5_harness["super_token"]
    super_csrf = phase5_harness["super_csrf"]
    admin_a_token = phase5_harness["admin_a_token"]
    admin_a_csrf = phase5_harness["admin_a_csrf"]
    admin_b_token = phase5_harness["admin_b_token"]
    admin_b_csrf = phase5_harness["admin_b_csrf"]
    tenant_a_id = phase5_harness["tenant_a_id"]

    image_bytes = create_test_image_bytes("JPEG")

    async with client_for(app) as client:
        # 1. Unauthenticated request to /api/v1/support fails closed
        unauth = await client.get("/api/v1/support")
        assert unauth.status_code == 401

        # 2. Register an Agent in Tenant A via Admin A
        r1 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        photo_key = r1.json()["object_key"]

        r2 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("aadhaar.jpg", image_bytes, "image/jpeg")},
            data={"category": "aadhaar-document"},
        )
        aadhaar_key = r2.json()["object_key"]

        r3 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("pan.jpg", image_bytes, "image/jpeg")},
            data={"category": "pan-document"},
        )
        pan_key = r3.json()["object_key"]

        agent_payload = {
            "first_name": "Ravi",
            "last_name": "Shankar",
            "username": "agent_ravi_supp",
            "password": "RaviSecurePassword123!",
            "mobile_e164": "+919876543288",
            "email": "ravi.supp@example.com",
            "address": "42 Market Street, Springfield",
            "aadhaar_number": "123456789012",
            "aadhaar_document_key": aadhaar_key,
            "pan_number": "ABCDE1234F",
            "pan_document_key": pan_key,
            "profile_photo_key": photo_key,
            "status": "active",
        }

        reg_res = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json=agent_payload,
        )
        assert reg_res.status_code == 201
        agent_data = reg_res.json()
        agent_ref_id = agent_data["ref_id"]

        # 3. Super Admin approves the pending appointment
        with factory() as session:
            appt = session.execute(
                select(AgentAppointment).where(
                    AgentAppointment.tenant_id == UUID(str(tenant_a_id)),
                    AgentAppointment.status == "PENDING",
                )
            ).scalars().first()
            assert appt is not None
            appt_id = appt.id

        appr_res = await client.post(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments/{appt_id}/approve",
            cookies={"community_platform_session": super_token},
            headers={"X-CSRF-Token": super_csrf, "Origin": "http://localhost:3000"},
        )
        assert appr_res.status_code == 200

        # Agent logs in
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username": "agent_ravi_supp", "password": "RaviSecurePassword123!"},
        )
        assert login_res.status_code == 200
        agent_token = login_res.cookies.get("community_session")
        assert agent_token is not None

        # 4. Agent creates a support ticket
        ticket_res = await client.post(
            "/api/v1/support",
            cookies={"community_session": agent_token},
            json={"message": "Need help with subscriber onboarding issue."},
        )
        assert ticket_res.status_code == 201
        ticket = ticket_res.json()["ticket"]
        ticket_id = ticket["id"]
        assert ticket["status"] == "open"
        assert ticket["employeeRefId"] == agent_ref_id
        assert ticket["employeeName"] == "Ravi Shankar"
        assert ticket["message"] == "Need help with subscriber onboarding issue."

        # 5. Agent lists support tickets and sees their own ticket
        agent_list = await client.get(
            "/api/v1/support",
            cookies={"community_session": agent_token},
        )
        assert agent_list.status_code == 200
        agent_tickets = agent_list.json()["tickets"]
        assert any(t["id"] == ticket_id for t in agent_tickets)

        # 6. Admin A (same tenant) sees the ticket
        admin_a_list = await client.get(
            "/api/v1/support",
            cookies={"community_session": admin_a_token},
        )
        assert admin_a_list.status_code == 200
        admin_a_tickets = admin_a_list.json()["tickets"]
        assert any(t["id"] == ticket_id for t in admin_a_tickets)

        # 7. Admin B (cross tenant) CANNOT see the ticket
        admin_b_list = await client.get(
            "/api/v1/support",
            cookies={"community_session": admin_b_token},
        )
        assert admin_b_list.status_code == 200
        admin_b_tickets = admin_b_list.json()["tickets"]
        assert not any(t["id"] == ticket_id for t in admin_b_tickets)

        # 8. Admin B CANNOT resolve the ticket (403 Forbidden)
        forbidden_patch = await client.patch(
            f"/api/v1/support/{ticket_id}/resolve",
            cookies={"community_session": admin_b_token},
        )
        assert forbidden_patch.status_code == 403

        # 9. Admin A resolves the ticket
        resolve_res = await client.patch(
            f"/api/v1/support/{ticket_id}/resolve",
            cookies={"community_session": admin_a_token},
        )
        assert resolve_res.status_code == 200
        resolved_ticket = resolve_res.json()["ticket"]
        assert resolved_ticket["status"] == "resolved"
        assert resolved_ticket["resolvedAt"] is not None

        # 10. Super Admin sees the ticket across all communities
        super_list = await client.get(
            "/api/v1/support",
            cookies={"community_platform_session": super_token},
        )
        assert super_list.status_code == 200
        super_tickets = super_list.json()["tickets"]
        assert any(t["id"] == ticket_id for t in super_tickets)


@pytest.mark.anyio
async def test_platform_activity_and_agent_me(phase5_harness):
    app = phase5_harness["app"]
    factory = phase5_harness["factory"]
    super_token = phase5_harness["super_token"]
    super_csrf = phase5_harness["super_csrf"]
    admin_a_token = phase5_harness["admin_a_token"]
    admin_a_csrf = phase5_harness["admin_a_csrf"]
    tenant_a_id = phase5_harness["tenant_a_id"]

    image_bytes = create_test_image_bytes("JPEG")

    async with client_for(app) as client:
        # Register an agent
        r1 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        photo_key = r1.json()["object_key"]

        r2 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("aadhaar.jpg", image_bytes, "image/jpeg")},
            data={"category": "aadhaar-document"},
        )
        aadhaar_key = r2.json()["object_key"]

        r3 = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("pan.jpg", image_bytes, "image/jpeg")},
            data={"category": "pan-document"},
        )
        pan_key = r3.json()["object_key"]

        reg_res = await client.post(
            "/api/v1/agents",
            cookies={"community_session": admin_a_token},
            headers={"X-CSRF-Token": admin_a_csrf, "Origin": "http://localhost:3000"},
            json={
                "first_name": "Pooja",
                "last_name": "Sharma",
                "username": "agent_pooja_act",
                "password": "PoojaPassword123!",
                "mobile_e164": "+919876543277",
                "email": "pooja.act@example.com",
                "address": "10 Hill Road, Springfield",
                "aadhaar_number": "987654321098",
                "aadhaar_document_key": aadhaar_key,
                "pan_number": "ABCDE9876G",
                "pan_document_key": pan_key,
                "profile_photo_key": photo_key,
                "status": "active",
            },
        )
        assert reg_res.status_code == 201

        # Super Admin approves Pooja's pending appointment
        with factory() as session:
            appt2 = session.execute(
                select(AgentAppointment).where(
                    AgentAppointment.tenant_id == UUID(str(tenant_a_id)),
                    AgentAppointment.status == "PENDING",
                )
            ).scalars().first()
            assert appt2 is not None
            appt2_id = appt2.id

        appr_res2 = await client.post(
            f"/api/v1/platform/communities/{tenant_a_id}/appointments/{appt2_id}/approve",
            cookies={"community_platform_session": super_token},
            headers={"X-CSRF-Token": super_csrf, "Origin": "http://localhost:3000"},
        )
        assert appr_res2.status_code == 200

        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username": "agent_pooja_act", "password": "PoojaPassword123!"},
        )
        assert login_res.status_code == 200
        agent_token = login_res.cookies.get("community_session")

        # 1. /agents/me test
        # Anonymous returns 401
        assert (await client.get("/api/v1/agents/me")).status_code == 401

        # Agent returns profile matching both agent and employee schemas
        agent_me = await client.get(
            "/api/v1/agents/me",
            cookies={"community_session": agent_token},
        )
        assert agent_me.status_code == 200
        body = agent_me.json()
        assert body["employee"]["firstName"] == "Pooja"
        assert body["employee"]["lastName"] == "Sharma"
        assert body["employee"]["refId"] is not None

        # 2. /platform/activity test
        # Anonymous returns 401
        assert (await client.get("/api/v1/platform/activity")).status_code == 401

        # Agent returns 403 (requires SUPER_ADMIN)
        assert (
            await client.get(
                "/api/v1/platform/activity",
                cookies={"community_session": agent_token},
            )
        ).status_code == 403

        # Super Admin returns 200 with activity list
        act_res = await client.get(
            "/api/v1/platform/activity",
            cookies={"community_platform_session": super_token},
        )
        assert act_res.status_code == 200
        act_body = act_res.json()
        assert "activity" in act_body
        assert isinstance(act_body["activity"], list)
        assert len(act_body["activity"]) > 0

