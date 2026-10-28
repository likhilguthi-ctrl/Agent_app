from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AccountCredential, AuthSession
from app.modules.authentication.password import hash_password
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.models import Role, RolePermission, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.authorization.seeding import seed_tenant_role_permissions
from app.modules.identity.models import TenantMembership, User
from app.modules.relationships.models import (
    MemberRelationship,
    RelationshipStatus,
    RelationshipType,
)
from app.modules.tenants.models import Tenant


@pytest.fixture
def relationship_harness(tmp_path):
    database_path = tmp_path / "relationship-test.sqlite"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    tenant_a_id = uuid4()
    tenant_b_id = uuid4()
    admin_token = "admin_session_token_123"
    admin_digest = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()
    admin_csrf = hashlib.sha256(b"admin_csrf_123").hexdigest()

    agent1_token = "agent1_session_token_123"
    agent1_digest = hashlib.sha256(agent1_token.encode("utf-8")).hexdigest()
    agent1_csrf = hashlib.sha256(b"agent1_csrf_123").hexdigest()

    admin_user_id = uuid4()
    admin_mem_id = uuid4()
    agent1_user_id = uuid4()
    agent1_mem_id = uuid4()
    agent1_profile_id = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # Create Tenants
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="community-alpha",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        tenant_b = Tenant(
            id=tenant_b_id,
            slug="community-beta",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        session.add_all([tenant_a, tenant_b])
        session.flush()

        # Seed RBAC for tenant A
        admin_role_a, agent_role_a = seed_tenant_role_permissions(session, tenant_a_id)

        # Admin User & Membership
        admin_user = User(id=admin_user_id, status="active", created_at=now, updated_at=now)
        session.add(admin_user)
        session.flush()

        admin_mem = TenantMembership(
            id=admin_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_user_id,
            mobile_e164="+919811111111",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(admin_mem)
        session.flush()

        admin_role_assign = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=admin_mem_id,
            role_id=admin_role_a.id,
            assigned_by_membership_id=admin_mem_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        admin_session = AuthSession(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=admin_mem_id,
            token_digest=admin_digest,
            csrf_token=admin_csrf,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        session.add_all([admin_role_assign, admin_session])
        session.flush()

        # Agent 1 User & Membership
        agent1_user = User(id=agent1_user_id, status="active", created_at=now, updated_at=now)
        session.add(agent1_user)
        session.flush()

        agent1_mem = TenantMembership(
            id=agent1_mem_id,
            tenant_id=tenant_a_id,
            user_id=agent1_user_id,
            mobile_e164="+919822222222",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(agent1_mem)
        session.flush()

        agent1_role_assign = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=agent1_mem_id,
            role_id=agent_role_a.id,
            assigned_by_membership_id=admin_mem_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        agent1_profile = AgentProfile(
            id=agent1_profile_id,
            tenant_id=tenant_a_id,
            membership_id=agent1_mem_id,
            user_id=agent1_user_id,
            first_name="Agent",
            last_name="One",
            ref_id="AGT001",
            profile_photo_key="photo1",
            aadhaar_number_ciphertext="enc_aadhaar",
            aadhaar_number_masked="XXXX-XXXX-1234",
            aadhaar_document_key="doc1",
            pan_number_ciphertext="enc_pan",
            pan_number_masked="ABCDE1234F",
            pan_document_key="doc2",
            created_at=now,
            updated_at=now,
        )
        agent1_session = AuthSession(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=agent1_mem_id,
            token_digest=agent1_digest,
            csrf_token=agent1_csrf,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        session.add_all([agent1_role_assign, agent1_profile, agent1_session])
        session.flush()
        session.commit()

    encryption_key = "12345678901234567890123456789012"
    test_settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
        identity_encryption_key=encryption_key,
    )
    app = create_app(test_settings)

    def db_override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_authentication_repository] = lambda: SQLAlchemyAuthenticationRepository(
        factory
    )
    app.dependency_overrides[get_authorization_repository] = lambda: SQLAlchemyAuthorizationRepository(
        factory
    )

    return {
        "engine": engine,
        "factory": factory,
        "app": app,
        "tenant_a_id": tenant_a_id,
        "tenant_b_id": tenant_b_id,
        "admin_token": admin_token,
        "admin_csrf": admin_csrf,
        "admin_mem_id": admin_mem_id,
        "admin_user_id": admin_user_id,
        "agent1_token": agent1_token,
        "agent1_csrf": agent1_csrf,
        "agent1_mem_id": agent1_mem_id,
        "agent1_user_id": agent1_user_id,
        "agent1_profile_id": agent1_profile_id,
    }


@pytest.mark.anyio
async def test_self_link_prohibited_and_same_tenant_enforced(relationship_harness):
    h = relationship_harness
    app = h["app"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Self-link attempt
        res = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(h["agent1_mem_id"]),
                "child_member_id": str(h["agent1_mem_id"]),
                "relationship_type": "REPORTS_TO",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        # Should be rejected with 422 or 400
        assert res.status_code in (400, 422)

        # Cross-tenant attempt (child belongs to tenant B)
        with h["factory"]() as session:
            now = datetime.now(UTC)
            user_b = User(id=uuid4(), status="active", created_at=now, updated_at=now)
            session.add(user_b)
            session.flush()
            mem_b = TenantMembership(
                id=uuid4(),
                tenant_id=h["tenant_b_id"],
                user_id=user_b.id,
                mobile_e164="+919833333333",
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(mem_b)
            session.commit()
            mem_b_id = mem_b.id

        res_cross = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(h["agent1_mem_id"]),
                "child_member_id": str(mem_b_id),
                "relationship_type": "REPORTS_TO",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert res_cross.status_code in (400, 404)


@pytest.mark.anyio
async def test_single_parent_constraint_and_multiple_managers(relationship_harness):
    h = relationship_harness
    app = h["app"]

    # Create Member X and Parent 2 in Tenant A
    with h["factory"]() as session:
        now = datetime.now(UTC)
        user_x = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        user_p2 = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add_all([user_x, user_p2])
        session.flush()

        mem_x = TenantMembership(
            id=uuid4(),
            tenant_id=h["tenant_a_id"],
            user_id=user_x.id,
            mobile_e164="+919844444444",
            status="active",
            created_at=now,
            updated_at=now,
        )
        mem_p2 = TenantMembership(
            id=uuid4(),
            tenant_id=h["tenant_a_id"],
            user_id=user_p2.id,
            mobile_e164="+919855555555",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add_all([mem_x, mem_p2])
        session.commit()
        mem_x_id = mem_x.id
        mem_p2_id = mem_p2.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. First parent REPORTS_TO
        res1 = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(h["agent1_mem_id"]),
                "child_member_id": str(mem_x_id),
                "relationship_type": "REPORTS_TO",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert res1.status_code == 201

        # 2. Second parent REPORTS_TO on same child -> Conflict (409)
        res2 = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(mem_p2_id),
                "child_member_id": str(mem_x_id),
                "relationship_type": "REPORTS_TO",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert res2.status_code == 409

        # 3. Multiple concurrent MANAGES relationships -> Permitted!
        res_m1 = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(h["agent1_mem_id"]),
                "child_member_id": str(mem_x_id),
                "relationship_type": "MANAGES",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert res_m1.status_code == 201

        res_m2 = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(mem_p2_id),
                "child_member_id": str(mem_x_id),
                "relationship_type": "MANAGES",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert res_m2.status_code == 201


@pytest.mark.anyio
async def test_hierarchy_cycle_prevention(relationship_harness):
    h = relationship_harness
    app = h["app"]

    with h["factory"]() as session:
        now = datetime.now(UTC)
        # Node A = agent1_mem_id
        # Node B
        user_b = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        user_c = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add_all([user_b, user_c])
        session.flush()

        mem_b = TenantMembership(
            id=uuid4(),
            tenant_id=h["tenant_a_id"],
            user_id=user_b.id,
            mobile_e164="+919866666666",
            status="active",
            created_at=now,
            updated_at=now,
        )
        mem_c = TenantMembership(
            id=uuid4(),
            tenant_id=h["tenant_a_id"],
            user_id=user_c.id,
            mobile_e164="+919877777777",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add_all([mem_b, mem_c])
        session.commit()
        mem_b_id = mem_b.id
        mem_c_id = mem_c.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # A -> B
        r1 = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(h["agent1_mem_id"]),
                "child_member_id": str(mem_b_id),
                "relationship_type": "REPORTS_TO",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert r1.status_code == 201

        # B -> C
        r2 = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(mem_b_id),
                "child_member_id": str(mem_c_id),
                "relationship_type": "REPORTS_TO",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert r2.status_code == 201

        # Cycle attempt: C -> A
        r3 = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(mem_c_id),
                "child_member_id": str(h["agent1_mem_id"]),
                "relationship_type": "REPORTS_TO",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert r3.status_code == 400


@pytest.mark.anyio
async def test_soft_revocation_and_history_retention(relationship_harness):
    h = relationship_harness
    app = h["app"]

    with h["factory"]() as session:
        now = datetime.now(UTC)
        user_sub = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(user_sub)
        session.flush()

        mem_sub = TenantMembership(
            id=uuid4(),
            tenant_id=h["tenant_a_id"],
            user_id=user_sub.id,
            mobile_e164="+919888888888",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_sub)
        session.commit()
        mem_sub_id = mem_sub.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Create relationship
        create_res = await client.post(
            "/api/v1/members/relationships",
            json={
                "parent_member_id": str(h["agent1_mem_id"]),
                "child_member_id": str(mem_sub_id),
                "relationship_type": "REFERRED_BY",
            },
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert create_res.status_code == 201
        rel_id = create_res.json()["id"]

        # Revoke relationship
        revoke_res = await client.request(
            "DELETE",
            f"/api/v1/members/relationships/{rel_id}",
            json={"end_reason": "Transferred to another sponsor"},
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local", "X-CSRF-Token": h["admin_csrf"]},
        )
        assert revoke_res.status_code == 200
        data = revoke_res.json()
        assert data["status"] == "REVOKED"
        assert data["ended_at"] is not None
        assert data["end_reason"] == "Transferred to another sponsor"

        # Query active only -> should be empty
        list_active = await client.get(
            f"/api/v1/members/{mem_sub_id}/relationships",
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local"},
        )
        assert list_active.status_code == 200
        assert list_active.json()["total"] == 0

        # Query including revoked -> should return 1
        list_all = await client.get(
            f"/api/v1/members/{mem_sub_id}/relationships?include_revoked=true",
            cookies={"community_session": h["admin_token"]},
            headers={"Host": "community-alpha.local"},
        )
        assert list_all.status_code == 200
        assert list_all.json()["total"] == 1
        assert list_all.json()["relationships"][0]["status"] == "REVOKED"


@pytest.mark.anyio
async def test_agent_descendants_roster_depth_and_pii_masking(relationship_harness):
    h = relationship_harness
    app = h["app"]

    # Build 4-level chain: Agent1 (L0) -> L1 -> L2 -> L3 -> L4
    with h["factory"]() as session:
        now = datetime.now(UTC)
        mems = []
        for i in range(1, 5):
            u = User(id=uuid4(), status="active", created_at=now, updated_at=now)
            session.add(u)
            session.flush()
            m = TenantMembership(
                id=uuid4(),
                tenant_id=h["tenant_a_id"],
                user_id=u.id,
                mobile_e164=f"+91980000000{i}",
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(m)
            session.flush()
            mems.append(m)

        # Link them
        # Agent1 -> L1
        session.add(MemberRelationship(
            id=uuid4(), tenant_id=h["tenant_a_id"],
            parent_member_id=h["agent1_mem_id"], child_member_id=mems[0].id,
            relationship_type="REPORTS_TO", status="ACTIVE",
            created_by_user_id=h["admin_user_id"], created_at=now,
        ))
        # L1 -> L2
        session.add(MemberRelationship(
            id=uuid4(), tenant_id=h["tenant_a_id"],
            parent_member_id=mems[0].id, child_member_id=mems[1].id,
            relationship_type="REPORTS_TO", status="ACTIVE",
            created_by_user_id=h["admin_user_id"], created_at=now,
        ))
        # L2 -> L3
        session.add(MemberRelationship(
            id=uuid4(), tenant_id=h["tenant_a_id"],
            parent_member_id=mems[1].id, child_member_id=mems[2].id,
            relationship_type="REPORTS_TO", status="ACTIVE",
            created_by_user_id=h["admin_user_id"], created_at=now,
        ))
        # L3 -> L4
        session.add(MemberRelationship(
            id=uuid4(), tenant_id=h["tenant_a_id"],
            parent_member_id=mems[2].id, child_member_id=mems[3].id,
            relationship_type="REPORTS_TO", status="ACTIVE",
            created_by_user_id=h["admin_user_id"], created_at=now,
        ))
        session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Agent requests descendants with default max_depth=3
        res = await client.get(
            "/api/v1/agent/descendants",
            cookies={"community_session": h["agent1_token"]},
            headers={"Host": "community-alpha.local"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 3  # L1, L2, L3 (L4 excluded by bounded depth)
        assert data["max_depth_reached"] == 3

        # Verify PII masking: mobile is masked and no Aadhaar/PAN fields exist
        for d in data["descendants"]:
            assert "*" in d["mobile_masked"]
            assert "aadhaar" not in d
            assert "pan" not in d


@pytest.mark.anyio
async def test_traversal_stops_at_inactive_node(relationship_harness):
    h = relationship_harness
    app = h["app"]

    with h["factory"]() as session:
        now = datetime.now(UTC)
        u1 = User(id=uuid4(), status="inactive", created_at=now, updated_at=now)
        u2 = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add_all([u1, u2])
        session.flush()

        m1 = TenantMembership(
            id=uuid4(),
            tenant_id=h["tenant_a_id"],
            user_id=u1.id,
            mobile_e164="+919811112222",
            status="inactive",  # INACTIVE NODE
            created_at=now,
            updated_at=now,
        )
        m2 = TenantMembership(
            id=uuid4(),
            tenant_id=h["tenant_a_id"],
            user_id=u2.id,
            mobile_e164="+919811113333",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add_all([m1, m2])
        session.flush()

        # Agent1 -> m1 (inactive) -> m2 (active)
        session.add(MemberRelationship(
            id=uuid4(), tenant_id=h["tenant_a_id"],
            parent_member_id=h["agent1_mem_id"], child_member_id=m1.id,
            relationship_type="REPORTS_TO", status="ACTIVE",
            created_by_user_id=h["admin_user_id"], created_at=now,
        ))
        session.add(MemberRelationship(
            id=uuid4(), tenant_id=h["tenant_a_id"],
            parent_member_id=m1.id, child_member_id=m2.id,
            relationship_type="REPORTS_TO", status="ACTIVE",
            created_by_user_id=h["admin_user_id"], created_at=now,
        ))
        session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get(
            "/api/v1/agent/descendants",
            cookies={"community_session": h["agent1_token"]},
            headers={"Host": "community-alpha.local"},
        )
        assert res.status_code == 200
        data = res.json()
        # Because m1 is inactive, traversal must not pass through m1 to m2, nor include m1
        assert data["total"] == 0


@pytest.mark.anyio
async def test_agent_descendant_stats(relationship_harness):
    h = relationship_harness
    app = h["app"]

    with h["factory"]() as session:
        now = datetime.now(UTC)
        u_child = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(u_child)
        session.flush()

        m_child = TenantMembership(
            id=uuid4(),
            tenant_id=h["tenant_a_id"],
            user_id=u_child.id,
            mobile_e164="+919876500001",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_child)
        session.flush()

        session.add(MemberRelationship(
            id=uuid4(), tenant_id=h["tenant_a_id"],
            parent_member_id=h["agent1_mem_id"], child_member_id=m_child.id,
            relationship_type="REPORTS_TO", status="ACTIVE",
            created_by_user_id=h["admin_user_id"], created_at=now,
        ))
        session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get(
            "/api/v1/agent/descendants/stats",
            cookies={"community_session": h["agent1_token"]},
            headers={"Host": "community-alpha.local"},
        )
        assert res.status_code == 200
        stats = res.json()
        assert stats["total_descendants"] >= 1
        assert stats["direct_children_count"] >= 1
        assert "counts_by_depth" in stats
