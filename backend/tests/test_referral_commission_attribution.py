"""Tests for Stage 4.3 Multi-Tier Referral Commission Attribution & Double-Entry Ledger Integration."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
import tempfile
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.modules.agents.models import AgentProfile
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.seeding import seed_tenant_role_permissions
from app.modules.commissions.models import (
    AccountType,
    CommissionAccount,
    CommissionJournal,
    CommissionLedgerEntry,
    JournalReferenceType,
    JournalStatus,
)
from app.modules.commissions.service import (
    CommissionAttributionService,
    FinancialLedgerService,
    get_commission_attribution_service,
)
from app.modules.identity.models import TenantMembership, User
from app.modules.payments.models import (
    PaymentOrder,
    PaymentOrderStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
)
from app.modules.payments.service import PaymentService
from app.modules.relationships.models import (
    MemberRelationship,
    RelationshipStatus,
    RelationshipType,
)
from app.modules.subscriptions.models import (
    MemberSubscription,
    SubscriptionPaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.tenants.models import Tenant


@pytest.fixture
def db_session_factory():
    fd, path = tempfile.mkstemp(suffix="_test_comm_attrib.db")
    os.close(fd)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def create_agent(
    session: Session,
    *,
    tenant_id: UUID,
    ref_id: str,
    override_rate: Decimal | None = None,
    membership_status: str = "active",
) -> tuple[User, TenantMembership, AgentProfile]:
    now = datetime.now(UTC)
    user = User(id=uuid4(), status="active", created_at=now, updated_at=now)
    session.add(user)
    session.flush()

    digits = "".join(c for c in ref_id if c.isdigit())
    if len(digits) < 8:
        digits = f"{abs(hash(ref_id)) % 100000000:08d}"
    mobile = f"+9198{digits[-8:]}"

    mem = TenantMembership(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=user.id,
        mobile_e164=mobile,
        status=membership_status,
        created_at=now,
        updated_at=now,
    )
    session.add(mem)
    session.flush()

    prof = AgentProfile(
        id=uuid4(),
        tenant_id=tenant_id,
        membership_id=mem.id,
        user_id=user.id,
        first_name="Agent",
        last_name=ref_id,
        email=f"{ref_id}@example.com",
        ref_id=ref_id,
        commission_rate_override=override_rate,
        profile_photo_key="photo_key",
        aadhaar_number_ciphertext="enc_aadhaar",
        aadhaar_number_masked="XXXX-XXXX-1234",
        aadhaar_document_key="doc_aadhaar",
        pan_number_ciphertext="enc_pan",
        pan_number_masked="ABCDE1234F",
        pan_document_key="doc_pan",
        created_at=now,
        updated_at=now,
    )
    session.add(prof)
    session.flush()
    return user, mem, prof


def link_referral(
    session: Session,
    *,
    tenant_id: UUID,
    parent_member_id: UUID,
    child_member_id: UUID,
    created_by_user_id: UUID,
    status: str = "ACTIVE",
) -> MemberRelationship:
    now = datetime.now(UTC)
    rel = MemberRelationship(
        id=uuid4(),
        tenant_id=tenant_id,
        parent_member_id=parent_member_id,
        child_member_id=child_member_id,
        relationship_type=RelationshipType.REFERRED_BY.value,
        status=status,
        created_by_user_id=created_by_user_id,
        created_at=now,
    )
    session.add(rel)
    session.flush()
    return rel


def test_tier1_direct_agent_override_attribution(db_session_factory):
    """Test direct Tier 1 agent attribution using agent profile override rate."""
    tenant_id = uuid4()
    now = datetime.now(UTC)

    with db_session_factory() as session:
        tenant = Tenant(
            id=tenant_id,
            slug="comm-attrib-1",
            status="active",
            default_commission_rate=Decimal("10.00"),
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()

        # Create Agent 1 with 15% override
        u1, m1, p1 = create_agent(session, tenant_id=tenant_id, ref_id="AGT001", override_rate=Decimal("15.00"))

        # Create Subscriber
        u_sub = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(u_sub)
        session.flush()
        m_sub = TenantMembership(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=u_sub.id,
            mobile_e164="+919800000001",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_sub)
        session.flush()

        # Link Subscriber referred by Agent 1
        link_referral(session, tenant_id=tenant_id, parent_member_id=m1.id, child_member_id=m_sub.id, created_by_user_id=u1.id)

        # Create Plan & Subscription with agreed_amount = ₹2,000.00
        plan = SubscriptionPlan(
            id=uuid4(),
            code="gold-plan",
            name="Gold Plan",
            duration_days=30,
            nominal_price=Decimal("2000.00"),
            currency="INR",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(plan)
        session.flush()

        sub = MemberSubscription(
            id=uuid4(),
            tenant_id=tenant_id,
            subscriber_membership_id=m_sub.id,
            plan_id=plan.id,
            snapshot_plan_name=plan.name,
            snapshot_duration_days=30,
            agreed_amount=Decimal("2000.00"),
            currency="INR",
            status=SubscriptionStatus.PENDING_PAYMENT.value,
            payment_status=SubscriptionPaymentStatus.UNPAID.value,
            starts_at=now,
            expires_at=now + timedelta(days=30),
            created_by_user_id=u_sub.id,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        session.flush()

        # Create Payment Order & Transaction
        order = PaymentOrder(
            id=uuid4(),
            tenant_id=tenant_id,
            subscription_id=sub.id,
            subscriber_membership_id=m_sub.id,
            gateway="razorpay",
            gateway_order_id="order_attrib_001",
            amount_paise=200000,
            amount=Decimal("2000.00"),
            currency="INR",
            status=PaymentOrderStatus.PAID.value,
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()

        tx = PaymentTransaction(
            id=uuid4(),
            tenant_id=tenant_id,
            payment_order_id=order.id,
            subscription_id=sub.id,
            gateway="razorpay",
            gateway_payment_id="pay_attrib_001",
            amount_paise=200000,
            amount=Decimal("2000.00"),
            currency="INR",
            status=PaymentTransactionStatus.CAPTURED.value,
            gateway_signature="sig_001",
            captured_at=now,
            processed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(tx)
        session.flush()

        # Attribute Commissions
        attrib_service = CommissionAttributionService()
        result = attrib_service.attribute_subscription_commissions(
            session=session,
            tenant_id=tenant_id,
            subscription=sub,
            payment_transaction=tx,
        )
        session.commit()

        # Assertions
        assert len(result.attributions) == 1
        attr1 = result.attributions[0]
        assert attr1.tier == 1
        assert attr1.agent_membership_id == m1.id
        assert attr1.rate_percent == Decimal("15.00")
        assert attr1.commission_amount == Decimal("300.00")  # 15% of 2000
        assert result.total_commission_distributed == Decimal("300.00")
        assert result.journal is not None
        assert result.journal.status == JournalStatus.POSTED.value
        assert result.journal.total_amount == Decimal("300.00")

        # Check Agent Account balance
        agent_acct = session.execute(
            select(CommissionAccount).where(
                CommissionAccount.tenant_id == tenant_id,
                CommissionAccount.agent_membership_id == m1.id,
            )
        ).scalar_one()
        assert agent_acct.current_balance == Decimal("300.00")

        # Check System Expense Account balance
        expense_acct = session.execute(
            select(CommissionAccount).where(
                CommissionAccount.tenant_id == tenant_id,
                CommissionAccount.account_type == AccountType.COMMUNITY_EXPENSE.value,
            )
        ).scalar_one()
        # In double-entry, expense account debited increases positive or negative depending on chart of accounts
        # Here sum of debits = 300.00
        ledger_entries = session.execute(
            select(CommissionLedgerEntry).where(
                CommissionLedgerEntry.journal_id == result.journal.id
            )
        ).scalars().all()
        assert len(ledger_entries) == 2
        debits = [e for e in ledger_entries if e.entry_type == "DEBIT"]
        credits = [e for e in ledger_entries if e.entry_type == "CREDIT"]
        assert len(debits) == 1 and debits[0].amount == Decimal("300.00")
        assert len(credits) == 1 and credits[0].amount == Decimal("300.00")


def test_multitier_three_level_lineage_attribution(db_session_factory):
    """Test full 3-tier hierarchy: Tier 1 (10%), Tier 2 (3%), Tier 3 (1%)."""
    tenant_id = uuid4()
    now = datetime.now(UTC)

    with db_session_factory() as session:
        tenant = Tenant(
            id=tenant_id,
            slug="comm-attrib-multi",
            status="active",
            default_commission_rate=Decimal("10.00"),
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()

        # Agents 1, 2, 3
        u3, m3, p3 = create_agent(session, tenant_id=tenant_id, ref_id="AGT003")  # Grandparent (Tier 3)
        u2, m2, p2 = create_agent(session, tenant_id=tenant_id, ref_id="AGT002")  # Parent (Tier 2)
        u1, m1, p1 = create_agent(session, tenant_id=tenant_id, ref_id="AGT001")  # Direct (Tier 1)

        # Hierarchy: Agent 3 -> referred -> Agent 2 -> referred -> Agent 1 -> referred -> Subscriber
        link_referral(session, tenant_id=tenant_id, parent_member_id=m3.id, child_member_id=m2.id, created_by_user_id=u3.id)
        link_referral(session, tenant_id=tenant_id, parent_member_id=m2.id, child_member_id=m1.id, created_by_user_id=u2.id)

        # Subscriber
        u_sub = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(u_sub)
        session.flush()
        m_sub = TenantMembership(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=u_sub.id,
            mobile_e164="+919800000002",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_sub)
        session.flush()
        link_referral(session, tenant_id=tenant_id, parent_member_id=m1.id, child_member_id=m_sub.id, created_by_user_id=u1.id)

        # Subscription agreed_amount = ₹10,000.00
        plan = SubscriptionPlan(
            id=uuid4(),
            code="platinum-plan",
            name="Platinum",
            duration_days=365,
            nominal_price=Decimal("10000.00"),
            currency="INR",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(plan)
        session.flush()

        sub = MemberSubscription(
            id=uuid4(),
            tenant_id=tenant_id,
            subscriber_membership_id=m_sub.id,
            plan_id=plan.id,
            snapshot_plan_name=plan.name,
            snapshot_duration_days=365,
            agreed_amount=Decimal("10000.00"),
            currency="INR",
            status=SubscriptionStatus.PENDING_PAYMENT.value,
            payment_status=SubscriptionPaymentStatus.UNPAID.value,
            starts_at=now,
            expires_at=now + timedelta(days=365),
            created_by_user_id=u_sub.id,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        session.flush()

        order = PaymentOrder(
            id=uuid4(),
            tenant_id=tenant_id,
            subscription_id=sub.id,
            subscriber_membership_id=m_sub.id,
            gateway="razorpay",
            gateway_order_id="order_attrib_002",
            amount_paise=1000000,
            amount=Decimal("10000.00"),
            currency="INR",
            status=PaymentOrderStatus.PAID.value,
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()

        tx = PaymentTransaction(
            id=uuid4(),
            tenant_id=tenant_id,
            payment_order_id=order.id,
            subscription_id=sub.id,
            gateway="razorpay",
            gateway_payment_id="pay_attrib_002",
            amount_paise=1000000,
            amount=Decimal("10000.00"),
            currency="INR",
            status=PaymentTransactionStatus.CAPTURED.value,
            gateway_signature="sig_002",
            captured_at=now,
            processed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(tx)
        session.flush()

        # Attribute
        attrib_service = CommissionAttributionService()
        result = attrib_service.attribute_subscription_commissions(
            session=session,
            tenant_id=tenant_id,
            subscription=sub,
            payment_transaction=tx,
        )
        session.commit()

        # Verifications
        assert len(result.attributions) == 3

        # Tier 1: 10% of 10000 = 1000.00
        assert result.attributions[0].tier == 1
        assert result.attributions[0].agent_membership_id == m1.id
        assert result.attributions[0].rate_percent == Decimal("10.00")
        assert result.attributions[0].commission_amount == Decimal("1000.00")

        # Tier 2: 3% of 10000 = 300.00
        assert result.attributions[1].tier == 2
        assert result.attributions[1].agent_membership_id == m2.id
        assert result.attributions[1].rate_percent == Decimal("3.00")
        assert result.attributions[1].commission_amount == Decimal("300.00")

        # Tier 3: 1% of 10000 = 100.00
        assert result.attributions[2].tier == 3
        assert result.attributions[2].agent_membership_id == m3.id
        assert result.attributions[2].rate_percent == Decimal("1.00")
        assert result.attributions[2].commission_amount == Decimal("100.00")

        # Total distributed = 1400.00
        assert result.total_commission_distributed == Decimal("1400.00")

        # Check ledger balances
        a1 = session.execute(select(CommissionAccount).where(CommissionAccount.agent_membership_id == m1.id)).scalar_one()
        a2 = session.execute(select(CommissionAccount).where(CommissionAccount.agent_membership_id == m2.id)).scalar_one()
        a3 = session.execute(select(CommissionAccount).where(CommissionAccount.agent_membership_id == m3.id)).scalar_one()
        assert a1.current_balance == Decimal("1000.00")
        assert a2.current_balance == Decimal("300.00")
        assert a3.current_balance == Decimal("100.00")


def test_missing_ancestors_retained_by_tenant(db_session_factory):
    """Test that missing Tier 2 and Tier 3 ancestors do not roll up and are retained by tenant."""
    tenant_id = uuid4()
    now = datetime.now(UTC)

    with db_session_factory() as session:
        tenant = Tenant(
            id=tenant_id,
            slug="comm-attrib-missing",
            status="active",
            default_commission_rate=Decimal("10.00"),
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()

        # Only Direct Agent 1 exists
        u1, m1, p1 = create_agent(session, tenant_id=tenant_id, ref_id="AGT_SOLO")

        u_sub = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(u_sub)
        session.flush()
        m_sub = TenantMembership(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=u_sub.id,
            mobile_e164="+919800000003",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_sub)
        session.flush()
        link_referral(session, tenant_id=tenant_id, parent_member_id=m1.id, child_member_id=m_sub.id, created_by_user_id=u1.id)

        plan = SubscriptionPlan(
            id=uuid4(),
            code="plan-solo",
            name="Plan Solo",
            duration_days=30,
            nominal_price=Decimal("1000.00"),
            currency="INR",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(plan)
        session.flush()

        sub = MemberSubscription(
            id=uuid4(),
            tenant_id=tenant_id,
            subscriber_membership_id=m_sub.id,
            plan_id=plan.id,
            snapshot_plan_name=plan.name,
            snapshot_duration_days=30,
            agreed_amount=Decimal("1000.00"),
            currency="INR",
            status=SubscriptionStatus.PENDING_PAYMENT.value,
            payment_status=SubscriptionPaymentStatus.UNPAID.value,
            starts_at=now,
            expires_at=now + timedelta(days=30),
            created_by_user_id=u_sub.id,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        session.flush()

        tx = PaymentTransaction(
            id=uuid4(),
            tenant_id=tenant_id,
            payment_order_id=uuid4(),
            subscription_id=sub.id,
            gateway="razorpay",
            gateway_payment_id="pay_solo_001",
            amount_paise=100000,
            amount=Decimal("1000.00"),
            currency="INR",
            status=PaymentTransactionStatus.CAPTURED.value,
            gateway_signature="sig_solo",
            captured_at=now,
            processed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(tx)
        session.flush()

        attrib_service = CommissionAttributionService()
        result = attrib_service.attribute_subscription_commissions(
            session=session,
            tenant_id=tenant_id,
            subscription=sub,
            payment_transaction=tx,
        )
        session.commit()

        # Only Tier 1 attributed; unearned 3% + 1% retained by tenant
        assert len(result.attributions) == 1
        assert result.attributions[0].tier == 1
        assert result.attributions[0].commission_amount == Decimal("100.00")
        assert result.total_commission_distributed == Decimal("100.00")

        # Agent 1 receives exactly 100.00, NOT 140.00
        a1 = session.execute(select(CommissionAccount).where(CommissionAccount.agent_membership_id == m1.id)).scalar_one()
        assert a1.current_balance == Decimal("100.00")


def test_inactive_ancestor_withheld_and_retained(db_session_factory):
    """Test that inactive upstream ancestors are withheld and retained without blocking active ancestors."""
    tenant_id = uuid4()
    now = datetime.now(UTC)

    with db_session_factory() as session:
        tenant = Tenant(
            id=tenant_id,
            slug="comm-attrib-inactive",
            status="active",
            default_commission_rate=Decimal("10.00"),
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()

        # Tier 3 Agent is active
        u3, m3, p3 = create_agent(session, tenant_id=tenant_id, ref_id="AGT_ACTIVE_3", membership_status="active")
        # Tier 2 Agent is INACTIVE
        u2, m2, p2 = create_agent(session, tenant_id=tenant_id, ref_id="AGT_INACTIVE_2", membership_status="inactive")
        # Tier 1 Agent is active
        u1, m1, p1 = create_agent(session, tenant_id=tenant_id, ref_id="AGT_ACTIVE_1", membership_status="active")

        link_referral(session, tenant_id=tenant_id, parent_member_id=m3.id, child_member_id=m2.id, created_by_user_id=u3.id)
        link_referral(session, tenant_id=tenant_id, parent_member_id=m2.id, child_member_id=m1.id, created_by_user_id=u2.id)

        u_sub = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(u_sub)
        session.flush()
        m_sub = TenantMembership(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=u_sub.id,
            mobile_e164="+919800000004",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_sub)
        session.flush()
        link_referral(session, tenant_id=tenant_id, parent_member_id=m1.id, child_member_id=m_sub.id, created_by_user_id=u1.id)

        plan = SubscriptionPlan(
            id=uuid4(),
            code="plan-inact",
            name="Plan Inactive Test",
            duration_days=30,
            nominal_price=Decimal("1000.00"),
            currency="INR",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(plan)
        session.flush()

        sub = MemberSubscription(
            id=uuid4(),
            tenant_id=tenant_id,
            subscriber_membership_id=m_sub.id,
            plan_id=plan.id,
            snapshot_plan_name=plan.name,
            snapshot_duration_days=30,
            agreed_amount=Decimal("1000.00"),
            currency="INR",
            status=SubscriptionStatus.PENDING_PAYMENT.value,
            payment_status=SubscriptionPaymentStatus.UNPAID.value,
            starts_at=now,
            expires_at=now + timedelta(days=30),
            created_by_user_id=u_sub.id,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        session.flush()

        tx = PaymentTransaction(
            id=uuid4(),
            tenant_id=tenant_id,
            payment_order_id=uuid4(),
            subscription_id=sub.id,
            gateway="razorpay",
            gateway_payment_id="pay_inact_001",
            amount_paise=100000,
            amount=Decimal("1000.00"),
            currency="INR",
            status=PaymentTransactionStatus.CAPTURED.value,
            gateway_signature="sig_inact",
            captured_at=now,
            processed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(tx)
        session.flush()

        attrib_service = CommissionAttributionService()
        result = attrib_service.attribute_subscription_commissions(
            session=session,
            tenant_id=tenant_id,
            subscription=sub,
            payment_transaction=tx,
        )
        session.commit()

        # Attributions: Tier 1 (100.00) + Tier 3 (10.00) = 110.00. Tier 2 (30.00) is withheld & retained!
        assert len(result.attributions) == 2
        tiers = [a.tier for a in result.attributions]
        assert tiers == [1, 3]
        assert result.attributions[0].commission_amount == Decimal("100.00")
        assert result.attributions[1].commission_amount == Decimal("10.00")
        assert result.total_commission_distributed == Decimal("110.00")

        # Inactive Agent 2 has NO commission account created or credited
        a2 = session.execute(
            select(CommissionAccount).where(CommissionAccount.agent_membership_id == m2.id)
        ).scalar_one_or_none()
        assert a2 is None or a2.current_balance == Decimal("0.00")


def test_direct_signup_without_referrer_produces_zero_journals(db_session_factory):
    """Test that a subscriber without any referral link produces 0 journal postings."""
    tenant_id = uuid4()
    now = datetime.now(UTC)

    with db_session_factory() as session:
        tenant = Tenant(
            id=tenant_id,
            slug="comm-attrib-direct",
            status="active",
            default_commission_rate=Decimal("10.00"),
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()

        u_sub = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(u_sub)
        session.flush()
        m_sub = TenantMembership(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=u_sub.id,
            mobile_e164="+919800000005",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_sub)
        session.flush()

        plan = SubscriptionPlan(
            id=uuid4(),
            code="plan-direct",
            name="Plan Direct",
            duration_days=30,
            nominal_price=Decimal("1000.00"),
            currency="INR",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(plan)
        session.flush()

        sub = MemberSubscription(
            id=uuid4(),
            tenant_id=tenant_id,
            subscriber_membership_id=m_sub.id,
            plan_id=plan.id,
            snapshot_plan_name=plan.name,
            snapshot_duration_days=30,
            agreed_amount=Decimal("1000.00"),
            currency="INR",
            status=SubscriptionStatus.PENDING_PAYMENT.value,
            payment_status=SubscriptionPaymentStatus.UNPAID.value,
            starts_at=now,
            expires_at=now + timedelta(days=30),
            created_by_user_id=u_sub.id,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        session.flush()

        tx = PaymentTransaction(
            id=uuid4(),
            tenant_id=tenant_id,
            payment_order_id=uuid4(),
            subscription_id=sub.id,
            gateway="razorpay",
            gateway_payment_id="pay_direct_001",
            amount_paise=100000,
            amount=Decimal("1000.00"),
            currency="INR",
            status=PaymentTransactionStatus.CAPTURED.value,
            gateway_signature="sig_direct",
            captured_at=now,
            processed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(tx)
        session.flush()

        attrib_service = CommissionAttributionService()
        result = attrib_service.attribute_subscription_commissions(
            session=session,
            tenant_id=tenant_id,
            subscription=sub,
            payment_transaction=tx,
        )
        session.commit()

        assert len(result.attributions) == 0
        assert result.total_commission_distributed == Decimal("0.00")
        assert result.journal is None

        # Verify 0 journals exist in DB
        journal_count = session.execute(select(func.count(CommissionJournal.id))).scalar_one()
        assert journal_count == 0


def test_idempotent_re_attribution_safety(db_session_factory):
    """Test that calling attribution twice for the same subscription & payment does not duplicate ledger entries."""
    tenant_id = uuid4()
    now = datetime.now(UTC)

    with db_session_factory() as session:
        tenant = Tenant(
            id=tenant_id,
            slug="comm-attrib-idemp",
            status="active",
            default_commission_rate=Decimal("10.00"),
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()

        u1, m1, p1 = create_agent(session, tenant_id=tenant_id, ref_id="AGT_IDEMP")

        u_sub = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(u_sub)
        session.flush()
        m_sub = TenantMembership(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=u_sub.id,
            mobile_e164="+919800000006",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_sub)
        session.flush()
        link_referral(session, tenant_id=tenant_id, parent_member_id=m1.id, child_member_id=m_sub.id, created_by_user_id=u1.id)

        plan = SubscriptionPlan(
            id=uuid4(),
            code="plan-idemp",
            name="Plan Idemp",
            duration_days=30,
            nominal_price=Decimal("1000.00"),
            currency="INR",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(plan)
        session.flush()

        sub = MemberSubscription(
            id=uuid4(),
            tenant_id=tenant_id,
            subscriber_membership_id=m_sub.id,
            plan_id=plan.id,
            snapshot_plan_name=plan.name,
            snapshot_duration_days=30,
            agreed_amount=Decimal("1000.00"),
            currency="INR",
            status=SubscriptionStatus.PENDING_PAYMENT.value,
            payment_status=SubscriptionPaymentStatus.UNPAID.value,
            starts_at=now,
            expires_at=now + timedelta(days=30),
            created_by_user_id=u_sub.id,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        session.flush()

        tx = PaymentTransaction(
            id=uuid4(),
            tenant_id=tenant_id,
            payment_order_id=uuid4(),
            subscription_id=sub.id,
            gateway="razorpay",
            gateway_payment_id="pay_idemp_001",
            amount_paise=100000,
            amount=Decimal("1000.00"),
            currency="INR",
            status=PaymentTransactionStatus.CAPTURED.value,
            gateway_signature="sig_idemp",
            captured_at=now,
            processed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(tx)
        session.flush()

        attrib_service = CommissionAttributionService()

        # Run 1
        res1 = attrib_service.attribute_subscription_commissions(
            session=session,
            tenant_id=tenant_id,
            subscription=sub,
            payment_transaction=tx,
        )
        session.commit()

        # Run 2
        res2 = attrib_service.attribute_subscription_commissions(
            session=session,
            tenant_id=tenant_id,
            subscription=sub,
            payment_transaction=tx,
        )
        session.commit()

        # Both return same journal ID
        assert res1.journal.id == res2.journal.id

        # Total journals in DB is exactly 1
        journal_count = session.execute(select(func.count(CommissionJournal.id))).scalar_one()
        assert journal_count == 1

        # Agent balance is exactly 100.00 (not 200.00)
        a1 = session.execute(select(CommissionAccount).where(CommissionAccount.agent_membership_id == m1.id)).scalar_one()
        assert a1.current_balance == Decimal("100.00")


def test_payment_service_process_captured_payment_triggers_attribution(db_session_factory):
    """Test that PaymentService.process_captured_payment automatically executes commission attribution."""
    tenant_id = uuid4()
    now = datetime.now(UTC)

    with db_session_factory() as session:
        tenant = Tenant(
            id=tenant_id,
            slug="comm-attrib-flow",
            status="active",
            default_commission_rate=Decimal("10.00"),
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()

        u1, m1, p1 = create_agent(session, tenant_id=tenant_id, ref_id="AGT_FLOW", override_rate=Decimal("12.00"))

        u_sub = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(u_sub)
        session.flush()
        m_sub = TenantMembership(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=u_sub.id,
            mobile_e164="+919800000007",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_sub)
        session.flush()
        link_referral(session, tenant_id=tenant_id, parent_member_id=m1.id, child_member_id=m_sub.id, created_by_user_id=u1.id)

        plan = SubscriptionPlan(
            id=uuid4(),
            code="plan-flow",
            name="Plan Flow",
            duration_days=30,
            nominal_price=Decimal("1000.00"),
            currency="INR",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(plan)
        session.flush()

        sub = MemberSubscription(
            id=uuid4(),
            tenant_id=tenant_id,
            subscriber_membership_id=m_sub.id,
            plan_id=plan.id,
            snapshot_plan_name=plan.name,
            snapshot_duration_days=30,
            agreed_amount=Decimal("1000.00"),
            currency="INR",
            status=SubscriptionStatus.PENDING_PAYMENT.value,
            payment_status=SubscriptionPaymentStatus.UNPAID.value,
            starts_at=now,
            expires_at=now + timedelta(days=30),
            created_by_user_id=u_sub.id,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        session.flush()

        order = PaymentOrder(
            id=uuid4(),
            tenant_id=tenant_id,
            subscription_id=sub.id,
            subscriber_membership_id=m_sub.id,
            gateway="razorpay",
            gateway_order_id="order_flow_001",
            amount_paise=100000,
            amount=Decimal("1000.00"),
            currency="INR",
            status=PaymentOrderStatus.CREATED.value,
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.commit()

        # Execute payment settlement via PaymentService
        pay_service = PaymentService()
        verify_resp = pay_service.process_captured_payment(
            session=session,
            order=order,
            payment_id="pay_flow_001",
            signature="sig_flow_001",
            actor=f"membership:{m_sub.id}",
        )

        assert verify_resp.success is True
        assert verify_resp.subscription_status == SubscriptionStatus.ACTIVE.value
        assert verify_resp.payment_status == SubscriptionPaymentStatus.SETTLED.value

        # Confirm subscription was updated
        updated_sub = session.get(MemberSubscription, sub.id)
        assert updated_sub.status == SubscriptionStatus.ACTIVE.value
        assert updated_sub.payment_status == SubscriptionPaymentStatus.SETTLED.value

        # Confirm Commission was credited to Agent 1 (12% of 1000 = 120.00)
        agent_acct = session.execute(
            select(CommissionAccount).where(
                CommissionAccount.tenant_id == tenant_id,
                CommissionAccount.agent_membership_id == m1.id,
            )
        ).scalar_one()
        assert agent_acct.current_balance == Decimal("120.00")

        # Confirm Journal exists
        journal = session.execute(
            select(CommissionJournal).where(
                CommissionJournal.tenant_id == tenant_id,
                CommissionJournal.reference_id == str(sub.id),
            )
        ).scalar_one()
        assert journal.total_amount == Decimal("120.00")
        assert journal.status == JournalStatus.POSTED.value

