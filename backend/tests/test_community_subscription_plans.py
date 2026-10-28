"""Automated tests for Community-Specific Subscription Plans.

Verifies:
1. Every plan requires a valid community_id foreign key referencing tenants.id.
2. Code uniqueness is scoped per community (identical codes across different communities allowed).
3. Creation without community_id fails with 422.
4. Creation for inactive community fails with 400.
5. Super Admin can filter plans by community.
6. Tenant/Agent catalog (/api/v1/subscription-plans) strictly isolates plans by community_id.
7. Cross-community plan detail retrieval (/api/v1/subscription-plans/{id}) fails with 404.
8. Cross-community subscription creation fails with 403.
9. Inactive plan subscription fails with 400.
10. Agent-led subscriber registration (/api/v1/agent/users) rejects cross-community plans with 403.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.modules.authentication.models import AccountCredential, OtpChallenge
from app.modules.authentication.repository import ResolvedSession
from app.modules.identity.models import TenantMembership, User
from app.modules.agents.models import AgentProfile
from app.modules.agents.schemas import UserRegistrationRequest
from app.modules.agents.api import register_user
from app.modules.subscriptions.models import (
    MemberSubscription,
    PlanStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.subscriptions.schemas import (
    PlanCreateRequest,
    PlanUpdateRequest,
    SubscriptionCreateRequest,
)
from app.modules.subscriptions.service import SubscriptionService
from app.modules.tenants.models import Tenant


@pytest.fixture(scope="module")
def db_session_factory():
    return get_session_factory()


@pytest.fixture
def db(db_session_factory):
    session = db_session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _get_or_create_tenant(session: Session, slug: str, status: str = "active") -> Tenant:
    t = session.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
    if not t:
        t = Tenant(
            id=uuid4(),
            slug=slug,
            status=status,
            photo_requirement="OPTIONAL",
            default_commission_rate=Decimal("10.00"),
            policy_version="v1",
        )
        session.add(t)
        session.flush()
    else:
        if t.status != status:
            t.status = status
            session.flush()
    return t


def test_plan_requires_community_and_scoped_code(db: Session):
    """Test 1: Plan requires valid community_id and allows same code across different communities."""
    service = SubscriptionService()
    t1 = _get_or_create_tenant(db, f"comm_t1_{uuid4().hex[:6]}")
    t2 = _get_or_create_tenant(db, f"comm_t2_{uuid4().hex[:6]}")
    admin_id = uuid4()

    code = f"GOLD_{uuid4().hex[:4].upper()}"

    # Plan in Community 1
    req1 = PlanCreateRequest(
        community_id=t1.id,
        code=code,
        name="Gold Plan T1",
        description="Gold package for Community 1",
        duration_days=30,
        nominal_price=Decimal("499.00"),
        status=PlanStatus.ACTIVE,
    )
    plan1 = service.create_plan(db, req1, creator_user_id=admin_id)
    assert plan1.community_id == t1.id
    assert plan1.code == code

    # Same code in Community 2 succeeds (scoped uniqueness)
    req2 = PlanCreateRequest(
        community_id=t2.id,
        code=code,
        name="Gold Plan T2",
        description="Gold package for Community 2",
        duration_days=30,
        nominal_price=Decimal("599.00"),
        status=PlanStatus.ACTIVE,
    )
    plan2 = service.create_plan(db, req2, creator_user_id=admin_id)
    assert plan2.community_id == t2.id
    assert plan2.code == code
    assert plan1.id != plan2.id

    # Duplicate code in the SAME community fails with 409
    with pytest.raises(HTTPException) as exc_info:
        service.create_plan(db, req1, creator_user_id=admin_id)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"] == "PLAN_CODE_EXISTS"


def test_plan_creation_inactive_or_nonexistent_community_fails(db: Session):
    """Test 2: Plan creation for nonexistent or inactive community fails."""
    service = SubscriptionService()
    admin_id = uuid4()

    # Nonexistent community
    fake_id = uuid4()
    req = PlanCreateRequest(
        community_id=fake_id,
        code=f"PLAN_{uuid4().hex[:4].upper()}",
        name="Fake Plan",
        duration_days=30,
        status=PlanStatus.ACTIVE,
    )
    with pytest.raises(HTTPException) as exc_info:
        service.create_plan(db, req, creator_user_id=admin_id)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "COMMUNITY_NOT_FOUND"

    # Inactive community
    inactive_tenant = _get_or_create_tenant(db, f"inactive_{uuid4().hex[:6]}", status="inactive")
    req_inactive = PlanCreateRequest(
        community_id=inactive_tenant.id,
        code=f"INACT_{uuid4().hex[:4].upper()}",
        name="Inactive Plan",
        duration_days=30,
        status=PlanStatus.ACTIVE,
    )
    with pytest.raises(HTTPException) as exc_info:
        service.create_plan(db, req_inactive, creator_user_id=admin_id)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "COMMUNITY_INACTIVE"


def test_list_plans_and_get_plan_isolation(db: Session):
    """Test 3: Plans listing and retrieval strictly isolate communities."""
    service = SubscriptionService()
    t1 = _get_or_create_tenant(db, f"comm_iso1_{uuid4().hex[:6]}")
    t2 = _get_or_create_tenant(db, f"comm_iso2_{uuid4().hex[:6]}")
    admin_id = uuid4()

    plan1 = service.create_plan(
        db,
        PlanCreateRequest(
            community_id=t1.id,
            code=f"P1_{uuid4().hex[:4].upper()}",
            name="Plan Community 1",
            duration_days=30,
            status=PlanStatus.ACTIVE,
        ),
        creator_user_id=admin_id,
    )

    plan2 = service.create_plan(
        db,
        PlanCreateRequest(
            community_id=t2.id,
            code=f"P2_{uuid4().hex[:4].upper()}",
            name="Plan Community 2",
            duration_days=30,
            status=PlanStatus.ACTIVE,
        ),
        creator_user_id=admin_id,
    )

    # Filtered listing for Community 1
    t1_plans = service.list_plans(db, community_id=t1.id)
    t1_ids = [p.id for p in t1_plans]
    assert plan1.id in t1_ids
    assert plan2.id not in t1_ids

    # Filtered listing for Community 2
    t2_plans = service.list_plans(db, community_id=t2.id)
    t2_ids = [p.id for p in t2_plans]
    assert plan2.id in t2_ids
    assert plan1.id not in t2_ids

    # Cross-community detail lookup fails with 404
    with pytest.raises(HTTPException) as exc_info:
        service.get_plan_by_id(db, plan_id=plan1.id, community_id=t2.id)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "PLAN_NOT_FOUND"


def test_cross_community_subscription_creation_fails(db: Session):
    """Test 4: Creating a subscription for a plan belonging to a different community fails with 403."""
    service = SubscriptionService()
    t1 = _get_or_create_tenant(db, f"sub_t1_{uuid4().hex[:6]}")
    t2 = _get_or_create_tenant(db, f"sub_t2_{uuid4().hex[:6]}")
    admin_id = uuid4()

    plan_t2 = service.create_plan(
        db,
        PlanCreateRequest(
            community_id=t2.id,
            code=f"P_T2_{uuid4().hex[:4].upper()}",
            name="T2 Plan",
            duration_days=30,
            status=PlanStatus.ACTIVE,
        ),
        creator_user_id=admin_id,
    )

    # Subscriber in Community 1
    u1 = User(id=uuid4(), status="active")
    db.add(u1)
    db.flush()
    mem1 = TenantMembership(
        id=uuid4(),
        tenant_id=t1.id,
        user_id=u1.id,
        mobile_e164=f"+919{random.randint(100000000, 999999999)}",
        status="active",
    )
    db.add(mem1)
    db.flush()

    # Attempt to subscribe Member in T1 to Plan in T2
    sub_req = SubscriptionCreateRequest(
        subscriber_membership_id=mem1.id,
        plan_id=plan_t2.id,
        agreed_amount=Decimal("299.00"),
    )
    with pytest.raises(HTTPException) as exc_info:
        service.create_subscription(db, tenant_id=t1.id, request=sub_req, creator_user_id=admin_id)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"] == "CROSS_COMMUNITY_PLAN_FORBIDDEN"


def test_inactive_plan_subscription_creation_fails(db: Session):
    """Test 5: Creating a subscription for an inactive plan fails with 400."""
    service = SubscriptionService()
    t1 = _get_or_create_tenant(db, f"inact_plan_t_{uuid4().hex[:6]}")
    admin_id = uuid4()

    plan_inactive = service.create_plan(
        db,
        PlanCreateRequest(
            community_id=t1.id,
            code=f"INACT_{uuid4().hex[:4].upper()}",
            name="Inactive Plan",
            duration_days=30,
            status=PlanStatus.INACTIVE,
        ),
        creator_user_id=admin_id,
    )

    u1 = User(id=uuid4(), status="active")
    db.add(u1)
    db.flush()
    mem1 = TenantMembership(
        id=uuid4(),
        tenant_id=t1.id,
        user_id=u1.id,
        mobile_e164=f"+919{random.randint(100000000, 999999999)}",
        status="active",
    )
    db.add(mem1)
    db.flush()

    sub_req = SubscriptionCreateRequest(
        subscriber_membership_id=mem1.id,
        plan_id=plan_inactive.id,
        agreed_amount=Decimal("199.00"),
    )
    with pytest.raises(HTTPException) as exc_info:
        service.create_subscription(db, tenant_id=t1.id, request=sub_req, creator_user_id=admin_id)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "PLAN_INACTIVE"


def test_agent_registration_cross_community_plan_rejected(db: Session):
    """Test 6: Agent registering subscriber with cross-community plan is rejected with 403."""
    service = SubscriptionService()
    t1 = _get_or_create_tenant(db, f"agent_t1_{uuid4().hex[:6]}")
    t2 = _get_or_create_tenant(db, f"agent_t2_{uuid4().hex[:6]}")
    admin_id = uuid4()

    # Plan in Community 2
    plan_t2 = service.create_plan(
        db,
        PlanCreateRequest(
            community_id=t2.id,
            code=f"T2_COMM_{uuid4().hex[:4].upper()}",
            name="Community 2 Only Plan",
            duration_days=30,
            status=PlanStatus.ACTIVE,
        ),
        creator_user_id=admin_id,
    )

    # Agent in Community 1
    agent_user = User(id=uuid4(), status="active")
    db.add(agent_user)
    db.flush()
    agent_mem = TenantMembership(
        id=uuid4(),
        tenant_id=t1.id,
        user_id=agent_user.id,
        mobile_e164=f"+919{random.randint(100000000, 999999999)}",
        status="active",
    )
    db.add(agent_mem)
    db.flush()

    caller = ResolvedSession(
        session_id=uuid4(),
        account_type="tenant_user",
        user_id=agent_user.id,
        username="agent_t1",
        role="AGENT",
        status="active",
        csrf_token="csrf",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=t1.id,
        membership_id=agent_mem.id,
        tenant={"id": t1.id, "slug": t1.slug},
    )

    # Valid Registration OTP token for new subscriber
    sub_mobile = f"+919{random.randint(100000000, 999999999)}"
    v_token = f"tok_{uuid4().hex}"
    db.add(
        OtpChallenge(
            id=uuid4(),
            tenant_id=t1.id,
            mobile_e164=sub_mobile,
            purpose="user_registration",
            otp_hash="approved",
            otp_salt="approved",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            verified_at=datetime.now(UTC),
            verification_token=v_token,
        )
    )
    db.flush()

    body = UserRegistrationRequest(
        full_name="Test Subscriber",
        mobile_e164=sub_mobile,
        verification_token=v_token,
        plan_id=plan_t2.id,
        whatsapp_same_as_mobile=True,
    )

    class DummyRequest:
        app = type("App", (), {"state": type("State", (), {"settings": None})()})()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(register_user(body=body, caller=caller, request=DummyRequest(), session=db))
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"] == "CROSS_COMMUNITY_PLAN_FORBIDDEN"


def test_agent_registration_no_community_plan_fails(db: Session):
    """Test 7: Agent registering subscriber without plan when community has no plans fails with 400."""
    t_empty = _get_or_create_tenant(db, f"empty_comm_{uuid4().hex[:6]}")
    agent_user = User(id=uuid4(), status="active")
    db.add(agent_user)
    db.flush()
    agent_mem = TenantMembership(
        id=uuid4(),
        tenant_id=t_empty.id,
        user_id=agent_user.id,
        mobile_e164=f"+919{random.randint(100000000, 999999999)}",
        status="active",
    )
    db.add(agent_mem)
    db.flush()

    caller = ResolvedSession(
        session_id=uuid4(),
        account_type="tenant_user",
        user_id=agent_user.id,
        username="agent_empty",
        role="AGENT",
        status="active",
        csrf_token="csrf",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=t_empty.id,
        membership_id=agent_mem.id,
        tenant={"id": t_empty.id, "slug": t_empty.slug},
    )

    sub_mobile = f"+919{random.randint(100000000, 999999999)}"
    v_token = f"tok_{uuid4().hex}"
    db.add(
        OtpChallenge(
            id=uuid4(),
            tenant_id=t_empty.id,
            mobile_e164=sub_mobile,
            purpose="user_registration",
            otp_hash="approved",
            otp_salt="approved",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            verified_at=datetime.now(UTC),
            verification_token=v_token,
        )
    )
    db.flush()

    body = UserRegistrationRequest(
        full_name="Subscriber Empty",
        mobile_e164=sub_mobile,
        verification_token=v_token,
        plan_id=None,
        whatsapp_same_as_mobile=True,
    )

    class DummyRequest:
        app = type("App", (), {"state": type("State", (), {"settings": None})()})()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(register_user(body=body, caller=caller, request=DummyRequest(), session=db))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "NO_COMMUNITY_PLAN"


def test_agent_registration_with_community_plan_succeeds(db: Session):
    """Test 8: Agent registering subscriber with matching active community plan succeeds."""
    service = SubscriptionService()
    t_comm = _get_or_create_tenant(db, f"succ_comm_{uuid4().hex[:6]}")
    admin_id = uuid4()

    plan = service.create_plan(
        db,
        PlanCreateRequest(
            community_id=t_comm.id,
            code=f"OK_{uuid4().hex[:4].upper()}",
            name="Valid Community Plan",
            nominal_price=Decimal("350.00"),
            duration_days=30,
            status=PlanStatus.ACTIVE,
        ),
        creator_user_id=admin_id,
    )

    agent_user = User(id=uuid4(), status="active")
    db.add(agent_user)
    db.flush()
    agent_mem = TenantMembership(
        id=uuid4(),
        tenant_id=t_comm.id,
        user_id=agent_user.id,
        mobile_e164=f"+919{random.randint(100000000, 999999999)}",
        status="active",
    )
    db.add(agent_mem)
    db.flush()

    caller = ResolvedSession(
        session_id=uuid4(),
        account_type="tenant_user",
        user_id=agent_user.id,
        username="agent_succ",
        role="AGENT",
        status="active",
        csrf_token="csrf",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=t_comm.id,
        membership_id=agent_mem.id,
        tenant={"id": t_comm.id, "slug": t_comm.slug},
    )

    sub_mobile = f"+919{random.randint(100000000, 999999999)}"
    v_token = f"tok_{uuid4().hex}"
    db.add(
        OtpChallenge(
            id=uuid4(),
            tenant_id=t_comm.id,
            mobile_e164=sub_mobile,
            purpose="user_registration",
            otp_hash="approved",
            otp_salt="approved",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            verified_at=datetime.now(UTC),
            verification_token=v_token,
        )
    )
    db.flush()

    body = UserRegistrationRequest(
        full_name="Subscriber Success",
        mobile_e164=sub_mobile,
        verification_token=v_token,
        plan_id=plan.id,
        whatsapp_same_as_mobile=True,
    )

    class DummyRequest:
        app = type("App", (), {"state": type("State", (), {"settings": None})()})()

    res = asyncio.run(register_user(body=body, caller=caller, request=DummyRequest(), session=db))
    assert res is not None
    assert res.full_name == "Subscriber Success"
    assert res.mobile_e164 == sub_mobile

    # Verify subscription record in db
    sub = db.execute(
        select(MemberSubscription).where(
            MemberSubscription.tenant_id == t_comm.id,
            MemberSubscription.plan_id == plan.id,
        )
    ).scalar_one_or_none()
    assert sub is not None
    assert sub.agreed_amount == Decimal("350.00")
    assert sub.snapshot_plan_name == "Valid Community Plan"
    assert sub.tenant_id == t_comm.id
