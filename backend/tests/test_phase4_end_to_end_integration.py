"""Phase 4 Stage 4.5 End-to-End Financial, Billing & Payout Integration Tests.

Comprehensive integration test covering:
1. Super Admin platform subscription plan creation and management.
2. Tenant subscription creation with manually agreed amount.
3. Razorpay payment order generation and exact integer paise conversion.
4. Cryptographic HMAC-SHA256 signature verification and captured status checks.
5. Subscription activation and multi-tier referral commission attribution (Tier 1/2/3).
6. Double-entry financial ledger journal posting (debits equal credits).
7. Agent eligible withdrawable balance calculation.
8. Payout request creation with double-entry PAYOUT_HOLD escrow journal.
9. Overdraft prevention when requested amount exceeds available balance.
10. Strict anti-self-approval enforcement for Community Admins.
11. Independent Community Admin review and approval workflow.
12. Platform Super Admin disbursement with mandatory external banking reference.
13. Double-entry settlement journal posting (PAYOUT_DISBURSEMENT).
14. Terminal state conflict prevention on completed payouts.
15. Compensating double-entry reversals on rejection, cancellation, and failure.
16. Cross-tenant isolation boundaries.
17. Full ledger integrity reconciliation across the entire database.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
import os
import tempfile
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.agents.models import AgentProfile
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AuthSession
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.dependencies import get_authorization_repository
from app.modules.authorization.models import UserRoleAssignment
from app.modules.authorization.repository import SQLAlchemyAuthorizationRepository
from app.modules.authorization.seeding import seed_tenant_role_permissions
from app.modules.commissions.models import (
    AccountType,
    CommissionJournal,
    CommissionLedgerEntry,
    EntryType,
    JournalReferenceType,
    JournalStatus,
)
from app.modules.commissions.schemas import JournalCreateRequest, JournalEntryLineSpec
from app.modules.commissions.service import FinancialLedgerService
from app.modules.identity.models import TenantMembership, User
from app.modules.payments.client import RazorpayClientAdapter, get_razorpay_adapter
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.relationships.models import (
    MemberRelationship,
    RelationshipStatus,
    RelationshipType,
)
from app.modules.tenants.models import Tenant


@pytest.fixture
def e2e_harness():
    fd, path = tempfile.mkstemp(suffix="_test_phase4_e2e.db")
    os.close(fd)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    tenant_a_id = uuid4()
    tenant_b_id = uuid4()

    # Platform Super Admin
    super_user_id = uuid4()
    super_token = "super_platform_e2e_token"
    super_digest = hashlib.sha256(super_token.encode("utf-8")).hexdigest()
    super_csrf = hashlib.sha256(b"super_platform_e2e_csrf").hexdigest()

    # Tenant A Independent Admin (Reviewer)
    admin_a_token = "admin_a_e2e_token"
    admin_a_digest = hashlib.sha256(admin_a_token.encode("utf-8")).hexdigest()
    admin_a_csrf = hashlib.sha256(b"admin_a_e2e_csrf").hexdigest()
    admin_a_user_id = uuid4()
    admin_a_mem_id = uuid4()

    # Tenant A Direct Agent (Tier 1) - also has Admin role to test anti-self-approval
    agent1_token = "agent1_e2e_token"
    agent1_digest = hashlib.sha256(agent1_token.encode("utf-8")).hexdigest()
    agent1_csrf = hashlib.sha256(b"agent1_e2e_csrf").hexdigest()
    agent1_user_id = uuid4()
    agent1_mem_id = uuid4()
    agent1_profile_id = uuid4()

    # Tenant A Parent Agent (Tier 2)
    agent2_token = "agent2_e2e_token"
    agent2_digest = hashlib.sha256(agent2_token.encode("utf-8")).hexdigest()
    agent2_csrf = hashlib.sha256(b"agent2_e2e_csrf").hexdigest()
    agent2_user_id = uuid4()
    agent2_mem_id = uuid4()
    agent2_profile_id = uuid4()

    # Tenant A Grandparent Agent (Tier 3)
    agent3_token = "agent3_e2e_token"
    agent3_digest = hashlib.sha256(agent3_token.encode("utf-8")).hexdigest()
    agent3_csrf = hashlib.sha256(b"agent3_e2e_csrf").hexdigest()
    agent3_user_id = uuid4()
    agent3_mem_id = uuid4()
    agent3_profile_id = uuid4()

    # Tenant A Subscriber (Customer)
    sub_user_id = uuid4()
    sub_mem_id = uuid4()

    # Tenant B Agent
    agent_b_token = "agent_b_e2e_token"
    agent_b_digest = hashlib.sha256(agent_b_token.encode("utf-8")).hexdigest()
    agent_b_csrf = hashlib.sha256(b"agent_b_e2e_csrf").hexdigest()
    agent_b_user_id = uuid4()
    agent_b_mem_id = uuid4()
    agent_b_profile_id = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # Tenants
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="tenant-e2e-a",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        tenant_b = Tenant(
            id=tenant_b_id,
            slug="tenant-e2e-b",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        session.add_all([tenant_a, tenant_b])
        session.flush()

        # Platform Super Admin
        root_op = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject="system:root",
            status="active",
        )
        super_op = PlatformOperator(
            id=super_user_id,
            issuer="https://identity.community-ott.local",
            subject="operator:superadmin",
            status="active",
        )
        super_role = PlatformRole(
            id=uuid4(),
            code="super_admin",
            display_name="Super Administrator",
            status="active",
        )
        session.add_all([root_op, super_op, super_role])
        session.flush()

        session.add(
            PlatformOperatorRoleAssignment(
                id=uuid4(),
                operator_id=super_user_id,
                role_id=super_role.id,
                assigned_by_operator_id=root_op.id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            PlatformOperatorSession(
                id=uuid4(),
                operator_id=super_user_id,
                token_digest=super_digest,
                csrf_token=super_csrf,
                authenticated_at=now,
                acr="urn:community:pwd",
                amr=["pwd"],
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        # Seed roles for Tenant A and Tenant B
        admin_role_a, agent_role_a = seed_tenant_role_permissions(session, tenant_a_id)
        admin_role_b, agent_role_b = seed_tenant_role_permissions(session, tenant_b_id)
        session.flush()

        # Tenant A Independent Admin
        u_admin_a = User(id=admin_a_user_id, status="active", created_at=now, updated_at=now)
        session.add(u_admin_a)
        session.flush()
        m_admin_a = TenantMembership(
            id=admin_a_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_a_user_id,
            mobile_e164="+919810000001",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_admin_a)
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a_mem_id,
                role_id=admin_role_a.id,
                assigned_by_membership_id=admin_a_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a_mem_id,
                token_digest=admin_a_digest,
                csrf_token=admin_a_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        # Helper to create agent in Tenant A
        def add_tenant_a_agent(user_id, mem_id, prof_id, mobile, ref_code, token_digest, csrf_tok, is_dual_admin=False):
            u = User(id=user_id, status="active", created_at=now, updated_at=now)
            session.add(u)
            session.flush()
            m = TenantMembership(
                id=mem_id,
                tenant_id=tenant_a_id,
                user_id=user_id,
                mobile_e164=mobile,
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(m)
            session.flush()
            session.add(
                UserRoleAssignment(
                    id=uuid4(),
                    tenant_id=tenant_a_id,
                    membership_id=mem_id,
                    role_id=agent_role_a.id,
                    assigned_by_membership_id=admin_a_mem_id,
                    status="active",
                    valid_from=now - timedelta(days=1),
                )
            )
            if is_dual_admin:
                session.add(
                    UserRoleAssignment(
                        id=uuid4(),
                        tenant_id=tenant_a_id,
                        membership_id=mem_id,
                        role_id=admin_role_a.id,
                        assigned_by_membership_id=admin_a_mem_id,
                        status="active",
                        valid_from=now - timedelta(days=1),
                    )
                )
            session.add(
                AgentProfile(
                    id=prof_id,
                    tenant_id=tenant_a_id,
                    membership_id=mem_id,
                    user_id=user_id,
                    first_name="Agent",
                    last_name=ref_code,
                    ref_id=ref_code,
                    profile_photo_key="photo",
                    aadhaar_number_ciphertext="enc",
                    aadhaar_number_masked="XXXX-XXXX-1111",
                    aadhaar_document_key="doc",
                    pan_number_ciphertext="enc",
                    pan_number_masked="ABCDE1111F",
                    pan_document_key="doc",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AuthSession(
                    id=uuid4(),
                    tenant_id=tenant_a_id,
                    membership_id=mem_id,
                    token_digest=token_digest,
                    csrf_token=csrf_tok,
                    created_at=now,
                    expires_at=now + timedelta(hours=2),
                )
            )

        # Agent 3: Grandparent (Tier 3)
        add_tenant_a_agent(agent3_user_id, agent3_mem_id, agent3_profile_id, "+919810000003", "AGT-TIER-3", agent3_digest, agent3_csrf)
        # Agent 2: Parent (Tier 2)
        add_tenant_a_agent(agent2_user_id, agent2_mem_id, agent2_profile_id, "+919810000002", "AGT-TIER-2", agent2_digest, agent2_csrf)
        # Agent 1: Direct Agent (Tier 1) - dual admin to test anti-self-approval
        add_tenant_a_agent(agent1_user_id, agent1_mem_id, agent1_profile_id, "+919810000011", "AGT-TIER-1", agent1_digest, agent1_csrf, is_dual_admin=True)

        # Tenant A Subscriber
        u_sub = User(id=sub_user_id, status="active", created_at=now, updated_at=now)
        session.add(u_sub)
        session.flush()
        m_sub = TenantMembership(
            id=sub_mem_id,
            tenant_id=tenant_a_id,
            user_id=sub_user_id,
            mobile_e164="+919899999999",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_sub)
        session.flush()

        # Multi-Tier Referral Lineage:
        # Subscriber REFERRED_BY Agent 1 (Tier 1)
        session.add(
            MemberRelationship(
                id=uuid4(),
                tenant_id=tenant_a_id,
                parent_member_id=agent1_mem_id,
                child_member_id=sub_mem_id,
                relationship_type=RelationshipType.REFERRED_BY.value,
                status=RelationshipStatus.ACTIVE.value,
                created_by_user_id=admin_a_user_id,
                created_at=now,
            )
        )
        # Agent 1 REFERRED_BY Agent 2 (Tier 2)
        session.add(
            MemberRelationship(
                id=uuid4(),
                tenant_id=tenant_a_id,
                parent_member_id=agent2_mem_id,
                child_member_id=agent1_mem_id,
                relationship_type=RelationshipType.REFERRED_BY.value,
                status=RelationshipStatus.ACTIVE.value,
                created_by_user_id=admin_a_user_id,
                created_at=now,
            )
        )
        # Agent 2 REFERRED_BY Agent 3 (Tier 3)
        session.add(
            MemberRelationship(
                id=uuid4(),
                tenant_id=tenant_a_id,
                parent_member_id=agent3_mem_id,
                child_member_id=agent2_mem_id,
                relationship_type=RelationshipType.REFERRED_BY.value,
                status=RelationshipStatus.ACTIVE.value,
                created_by_user_id=admin_a_user_id,
                created_at=now,
            )
        )

        # Tenant B Agent
        u_b = User(id=agent_b_user_id, status="active", created_at=now, updated_at=now)
        session.add(u_b)
        session.flush()
        m_b = TenantMembership(
            id=agent_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=agent_b_user_id,
            mobile_e164="+919840000000",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(m_b)
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=agent_b_mem_id,
                role_id=agent_role_b.id,
                assigned_by_membership_id=agent_b_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AgentProfile(
                id=agent_b_profile_id,
                tenant_id=tenant_b_id,
                membership_id=agent_b_mem_id,
                user_id=agent_b_user_id,
                first_name="Agent",
                last_name="B",
                ref_id="AGT-TENANT-B",
                profile_photo_key="photo_b",
                aadhaar_number_ciphertext="enc",
                aadhaar_number_masked="XXXX-XXXX-4444",
                aadhaar_document_key="doc",
                pan_number_ciphertext="enc",
                pan_number_masked="ABCDE4444F",
                pan_document_key="doc",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=agent_b_mem_id,
                token_digest=agent_b_digest,
                csrf_token=agent_b_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        session.commit()

    settings = Settings(
        app_env="testing",
        jwt_secret_key="test-secret-key-phase4-e2e",
        csrf_secret_key="test-csrf-secret-key-phase4-e2e",
        backend_api_base_url="http://testserver",
    )
    app = create_app(settings=settings)

    def override_get_db():
        with factory() as s:
            yield s

    def override_auth_repo():
        return SQLAlchemyAuthenticationRepository(factory)

    def override_authz_repo():
        return SQLAlchemyAuthorizationRepository(factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_authentication_repository] = override_auth_repo
    app.dependency_overrides[get_authorization_repository] = override_authz_repo

    from app.modules.payments import client as payments_client_mod
    original_adapter = payments_client_mod._default_adapter
    mock_adapter = RazorpayClientAdapter(
        key_id="rzp_test_e2e_key",
        key_secret="e2e_secret_key_123456",
        webhook_secret="e2e_webhook_secret_987654",
        is_mock=True,
    )
    payments_client_mod._default_adapter = mock_adapter

    yield {
        "engine": engine,
        "factory": factory,
        "app": app,
        "tenant_a_id": tenant_a_id,
        "tenant_b_id": tenant_b_id,
        "super_token": super_token,
        "super_csrf": super_csrf,
        "admin_a_token": admin_a_token,
        "admin_a_csrf": admin_a_csrf,
        "admin_a_user_id": admin_a_user_id,
        "admin_a_mem_id": admin_a_mem_id,
        "agent1_token": agent1_token,
        "agent1_csrf": agent1_csrf,
        "agent1_user_id": agent1_user_id,
        "agent1_mem_id": agent1_mem_id,
        "agent2_user_id": agent2_user_id,
        "agent3_user_id": agent3_user_id,
        "sub_mem_id": sub_mem_id,
        "agent_b_token": agent_b_token,
        "agent_b_csrf": agent_b_csrf,
    }

    payments_client_mod._default_adapter = original_adapter
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def fund_agent_commission(factory, tenant_id: UUID, agent_mem_id: UUID, amount: Decimal):
    """Utility to seed balanced commission entries into an agent's account."""
    ledger = FinancialLedgerService()
    with factory() as session:
        prof = session.execute(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.membership_id == agent_mem_id,
            )
        ).scalar_one()
        agent_acct = ledger.get_or_create_agent_account(
            session,
            tenant_id=tenant_id,
            agent_membership_id=agent_mem_id,
            agent_profile_id=prof.id,
        )
        community_revenue_acct = ledger.get_or_create_system_account(
            session,
            tenant_id=tenant_id,
            account_type=AccountType.COMMUNITY_CLEARING,
        )
        session.commit()

        ref_id = uuid4()
        ledger.post_journal(
            session,
            tenant_id=tenant_id,
            created_by_user_id=prof.user_id,
            request=JournalCreateRequest(
                idempotency_key=f"fund-{ref_id}",
                reference_type=JournalReferenceType.REFERRAL_COMMISSION.value,
                reference_id=str(ref_id),
                description="Funding agent for payout tests",
                entries=[
                    JournalEntryLineSpec(
                        account_id=community_revenue_acct.id,
                        entry_type=EntryType.DEBIT.value,
                        amount=amount,
                    ),
                    JournalEntryLineSpec(
                        account_id=agent_acct.id,
                        entry_type=EntryType.CREDIT.value,
                        amount=amount,
                    ),
                ],
            ),
        )
        session.commit()


@pytest.mark.anyio
async def test_full_phase4_end_to_end_financial_workflow(e2e_harness):
    """Complete end-to-end integration of Phase 4 financial engine:
    1. Super Admin creates Subscription Plan.
    2. Agent creates Subscription with manual agreed amount (₹1,200.00).
    3. Razorpay Order created (120,000 paise).
    4. Razorpay payment verified via HMAC-SHA256 signature.
    5. Subscription activates and Multi-Tier Commission Attribution posts balanced journals:
       - Tier 1 (10%): ₹120.00
       - Tier 2 (3%): ₹36.00
       - Tier 3 (1%): ₹12.00
    6. Agent queries eligible balance (₹120.00 available).
    7. Overdraft prevention (requesting ₹150.00 fails with HTTP 422).
    8. Agent creates payout request for ₹100.00 -> balanced PAYOUT_HOLD journal posted.
    9. Anti-self-approval enforcement: Agent cannot review own request (HTTP 403).
    10. Independent Community Admin approves payout.
    11. Unauthorized tenant admin cannot disburse (HTTP 401/403).
    12. Platform Super Admin disburses payout with UTR -> balanced PAYOUT_DISBURSEMENT journal posted.
    13. Completed payout transitions to terminal PAID state and resists modification (HTTP 409).
    14. System-wide ledger reconciliation: Debits == Credits across all journals.
    """
    app = e2e_harness["app"]
    factory = e2e_harness["factory"]
    tenant_a_id = e2e_harness["tenant_a_id"]
    sub_mem_id = e2e_harness["sub_mem_id"]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:

        # ---------------------------------------------------------------------
        # Step 1: Super Admin creates Subscription Plan
        # ---------------------------------------------------------------------
        plan_res = await client.post(
            "/api/v1/platform/subscription-plans",
            cookies={"community_platform_session": e2e_harness["super_token"]},
            headers={"X-CSRF-Token": e2e_harness["super_csrf"]},
            json={
                "code": "GOLD_ANNUAL_E2E",
                "name": "Gold Annual Plan",
                "description": "Full access to community OTT services for 1 year.",
                "duration_days": 365,
                "nominal_price": "1200.00",
                "currency": "INR",
                "benefits": ["Live TV", "HD Streaming", "On-Demand Library"],
            },
        )
        assert plan_res.status_code == 201, plan_res.text
        plan_id = plan_res.json()["id"]

        # ---------------------------------------------------------------------
        # Step 2: Agent creates MemberSubscription with manual agreed amount
        # ---------------------------------------------------------------------
        sub_res = await client.post(
            "/api/v1/subscriptions",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={
                "X-CSRF-Token": e2e_harness["agent1_csrf"],
                "Host": "tenant-e2e-a.local",
            },
            json={
                "plan_id": plan_id,
                "subscriber_membership_id": str(sub_mem_id),
                "agreed_amount": "1200.00",
                "currency": "INR",
                "notes": "E2E Subscription Test Agreement",
            },
        )
        assert sub_res.status_code == 201, sub_res.text
        sub_data = sub_res.json()
        sub_id = sub_data["id"]
        assert sub_data["status"] == "PENDING_PAYMENT"
        assert sub_data["payment_status"] == "UNPAID"

        # Verify zero financial ledger movements prior to payment
        with factory() as session:
            journals = session.execute(
                select(CommissionJournal).where(CommissionJournal.tenant_id == tenant_a_id)
            ).scalars().all()
            assert len(journals) == 0, "No journals should exist before payment"

        # ---------------------------------------------------------------------
        # Step 3: Create Razorpay Payment Order (Paise Conversion)
        # ---------------------------------------------------------------------
        order_res = await client.post(
            "/api/v1/payments/orders",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={
                "X-CSRF-Token": e2e_harness["agent1_csrf"],
                "Host": "tenant-e2e-a.local",
            },
            json={
                "subscription_id": sub_id,
                "idempotency_key": f"order_idempotency_{uuid4().hex[:8]}",
            },
        )
        assert order_res.status_code == 201, order_res.text
        order_data = order_res.json()
        gateway_order_id = order_data["gateway_order_id"]
        assert order_data["amount_paise"] == 120000
        assert order_data["amount"] == "1200.00"

        # ---------------------------------------------------------------------
        # Step 4: Verify Payment via HMAC-SHA256 signature & Activate Subscription
        # ---------------------------------------------------------------------
        fake_payment_id = f"pay_mock_{uuid4().hex[:12]}"
        adapter = get_razorpay_adapter()
        secret = adapter.key_secret or "mock_secret_key"
        msg = f"{gateway_order_id}|{fake_payment_id}"
        valid_signature = hmac.new(
            secret.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        verify_res = await client.post(
            "/api/v1/payments/verify",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={
                "X-CSRF-Token": e2e_harness["agent1_csrf"],
                "Host": "tenant-e2e-a.local",
            },
            json={
                "razorpay_order_id": gateway_order_id,
                "razorpay_payment_id": fake_payment_id,
                "razorpay_signature": valid_signature,
            },
        )
        assert verify_res.status_code == 200, verify_res.text
        verify_data = verify_res.json()
        assert verify_data["subscription_status"] == "ACTIVE"
        assert verify_data["payment_status"] == "SETTLED"

        # ---------------------------------------------------------------------
        # Step 5: Verify Multi-Tier Referral Commission Attribution
        # Tier 1 (Agent 1): 10% = ₹120.00
        # Tier 2 (Agent 2): 3% = ₹36.00
        # Tier 3 (Agent 3): 1% = ₹12.00
        # Total Commission: ₹168.00
        # ---------------------------------------------------------------------
        with factory() as session:
            journals = session.execute(
                select(CommissionJournal).where(
                    CommissionJournal.tenant_id == tenant_a_id,
                    CommissionJournal.reference_type == JournalReferenceType.REFERRAL_COMMISSION.value,
                )
            ).scalars().all()
            assert len(journals) == 1, f"Expected 1 referral commission journal, found {len(journals)}"

            j = journals[0]
            assert j.status == JournalStatus.POSTED
            lines = session.execute(
                select(CommissionLedgerEntry).where(CommissionLedgerEntry.journal_id == j.id)
            ).scalars().all()
            assert len(lines) == 4  # 1 debit (expense) + 3 credits (Tier 1, Tier 2, Tier 3)
            debits = sum(line.amount for line in lines if line.entry_type == EntryType.DEBIT)
            credits = sum(line.amount for line in lines if line.entry_type == EntryType.CREDIT)
            assert debits == credits == Decimal("168.00"), f"Journal {j.id} unbalanced: debits={debits}, credits={credits}"

        # ---------------------------------------------------------------------
        # Step 6: Agent 1 queries Eligible Balance
        # ---------------------------------------------------------------------
        bal_res = await client.get(
            "/api/v1/payouts/eligible-balance",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={"Host": "tenant-e2e-a.local"},
        )
        assert bal_res.status_code == 200, bal_res.text
        bal_data = bal_res.json()
        assert Decimal(str(bal_data["current_balance"])) == Decimal("120.00")
        assert Decimal(str(bal_data["pending_payout_amount"])) == Decimal("0.00")
        assert Decimal(str(bal_data["available_to_withdraw"])) == Decimal("120.00")

        # ---------------------------------------------------------------------
        # Step 7: Overdraft Protection (Requesting > Available Fails)
        # ---------------------------------------------------------------------
        overdraft_res = await client.post(
            "/api/v1/payouts/requests",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={
                "X-CSRF-Token": e2e_harness["agent1_csrf"],
                "Host": "tenant-e2e-a.local",
            },
            json={
                "amount": 150.00,
                "idempotency_key": "overdraft_attempt_key",
                "request_note": "Attempting overdraft",
            },
        )
        assert overdraft_res.status_code == 422, overdraft_res.text

        # ---------------------------------------------------------------------
        # Step 8: Agent 1 submits valid Payout Request of ₹100.00 (Escrow Hold)
        # ---------------------------------------------------------------------
        payout_res = await client.post(
            "/api/v1/payouts/requests",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={
                "X-CSRF-Token": e2e_harness["agent1_csrf"],
                "Host": "tenant-e2e-a.local",
            },
            json={
                "amount": 100.00,
                "idempotency_key": "payout_e2e_valid_key_01",
                "request_note": "Direct Agent One payout request",
            },
        )
        assert payout_res.status_code == 201, payout_res.text
        payout_data = payout_res.json()
        payout_id = payout_data["id"]
        assert payout_data["status"] == "PENDING_REVIEW"
        assert Decimal(str(payout_data["amount"])) == Decimal("100.00")

        # Verify updated balance after escrow hold: available drops to ₹20.00
        bal2_res = await client.get(
            "/api/v1/payouts/eligible-balance",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={"Host": "tenant-e2e-a.local"},
        )
        bal2 = bal2_res.json()
        assert Decimal(str(bal2["current_balance"])) == Decimal("20.00")
        assert Decimal(str(bal2["pending_payout_amount"])) == Decimal("100.00")
        assert Decimal(str(bal2["available_to_withdraw"])) == Decimal("20.00")

        # ---------------------------------------------------------------------
        # Step 9: Strict Anti-Self-Approval Enforcement
        # Agent 1 (who also has Tenant Admin role) attempts to review own payout
        # ---------------------------------------------------------------------
        self_review_res = await client.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={
                "X-CSRF-Token": e2e_harness["agent1_csrf"],
                "Host": "tenant-e2e-a.local",
            },
            json={
                "decision": "APPROVE",
                "reason": "Attempting unauthorized self approval of payout.",
            },
        )
        assert self_review_res.status_code == 403, self_review_res.text

        # ---------------------------------------------------------------------
        # Step 10: Independent Community Admin Reviews and Approves Payout
        # ---------------------------------------------------------------------
        approve_res = await client.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            cookies={"community_session": e2e_harness["admin_a_token"]},
            headers={
                "X-CSRF-Token": e2e_harness["admin_a_csrf"],
                "Host": "tenant-e2e-a.local",
            },
            json={
                "decision": "APPROVE",
                "reason": "Approved by independent tenant admin after verification.",
            },
        )
        assert approve_res.status_code == 200, approve_res.text
        assert approve_res.json()["status"] == "APPROVED"

        # ---------------------------------------------------------------------
        # Step 11: Tenant Admin Cannot Disburse Funds (Platform super admin only)
        # ---------------------------------------------------------------------
        unauth_disburse_res = await client.post(
            f"/api/v1/platform/payouts/requests/{payout_id}/disburse",
            cookies={"community_session": e2e_harness["admin_a_token"]},
            headers={
                "X-CSRF-Token": e2e_harness["admin_a_csrf"],
                "Host": "tenant-e2e-a.local",
            },
            json={
                "disbursement_reference": "UTR-UNAUTHORIZED-ATTEMPT-01",
                "notes": "Tenant admin disbursement attempt",
            },
        )
        assert unauth_disburse_res.status_code in (401, 403)

        # ---------------------------------------------------------------------
        # Step 12: Platform Super Admin Disburses Payout (With Verifiable UTR)
        # ---------------------------------------------------------------------
        disburse_res = await client.post(
            f"/api/v1/platform/payouts/requests/{payout_id}/disburse",
            cookies={"community_platform_session": e2e_harness["super_token"]},
            headers={"X-CSRF-Token": e2e_harness["super_csrf"]},
            json={
                "disbursement_reference": "UTR-E2E-20261021-998877",
                "notes": "NEFT corporate payout settlement",
            },
        )
        assert disburse_res.status_code == 200, disburse_res.text
        disbursed_data = disburse_res.json()
        assert disbursed_data["status"] == "PAID"
        assert disbursed_data["disbursement_reference"] == "UTR-E2E-20261021-998877"

        # Verify updated balance: current_balance drops to 20, pending drops to 0, available is 20
        bal3_res = await client.get(
            "/api/v1/payouts/eligible-balance",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={"Host": "tenant-e2e-a.local"},
        )
        bal3 = bal3_res.json()
        assert Decimal(str(bal3["current_balance"])) == Decimal("20.00")
        assert Decimal(str(bal3["pending_payout_amount"])) == Decimal("0.00")
        assert Decimal(str(bal3["available_to_withdraw"])) == Decimal("20.00")

        # ---------------------------------------------------------------------
        # Step 13: Terminal State Protection (HTTP 409 Conflict)
        # ---------------------------------------------------------------------
        # Attempt to re-disburse
        redisburse_res = await client.post(
            f"/api/v1/platform/payouts/requests/{payout_id}/disburse",
            cookies={"community_platform_session": e2e_harness["super_token"]},
            headers={"X-CSRF-Token": e2e_harness["super_csrf"]},
            json={
                "disbursement_reference": "UTR-RETRY-DUPLICATE",
            },
        )
        assert redisburse_res.status_code == 409, redisburse_res.text

        # Attempt to reject completed payout
        rereview_res = await client.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            cookies={"community_session": e2e_harness["admin_a_token"]},
            headers={
                "X-CSRF-Token": e2e_harness["admin_a_csrf"],
                "Host": "tenant-e2e-a.local",
            },
            json={
                "decision": "REJECT",
                "reason": "Attempting invalid rejection on completed payout.",
            },
        )
        assert rereview_res.status_code == 409, rereview_res.text

        # ---------------------------------------------------------------------
        # Step 14: System-Wide Financial Ledger Reconciliation
        # Every journal across the database must balance (Debits == Credits)
        # ---------------------------------------------------------------------
        with factory() as session:
            all_journals = session.execute(select(CommissionJournal)).scalars().all()
            assert len(all_journals) >= 3  # 1 multi-tier commission + 1 hold + 1 disbursement

            total_system_debits = Decimal("0.00")
            total_system_credits = Decimal("0.00")

            for j in all_journals:
                lines = session.execute(
                    select(CommissionLedgerEntry).where(CommissionLedgerEntry.journal_id == j.id)
                ).scalars().all()
                j_debits = sum(line.amount for line in lines if line.entry_type == EntryType.DEBIT)
                j_credits = sum(line.amount for line in lines if line.entry_type == EntryType.CREDIT)
                assert j_debits == j_credits, f"Journal {j.id} ({j.reference_type}) unbalanced"
                total_system_debits += j_debits
                total_system_credits += j_credits

            assert total_system_debits == total_system_credits
            assert total_system_debits > Decimal("0.00")


@pytest.mark.anyio
async def test_payout_rejection_and_compensating_reversal_e2e(e2e_harness):
    """Verifies that Community Admin rejection posts a balanced REVERSAL journal
    and completely restores the agent's available balance.
    """
    app = e2e_harness["app"]
    factory = e2e_harness["factory"]
    tenant_a_id = e2e_harness["tenant_a_id"]
    agent1_mem_id = e2e_harness["agent1_mem_id"]

    # Pre-credit agent with ₹500 commission
    fund_agent_commission(factory, tenant_a_id, agent1_mem_id, Decimal("500.00"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Check initial balance: ₹500.00 available
        b1 = (await client.get("/api/v1/payouts/eligible-balance", cookies={"community_session": e2e_harness["agent1_token"]}, headers={"Host": "tenant-e2e-a.local"})).json()
        assert Decimal(str(b1["available_to_withdraw"])) == Decimal("500.00")

        # Submit payout request for ₹200.00
        p_res = await client.post(
            "/api/v1/payouts/requests",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={"X-CSRF-Token": e2e_harness["agent1_csrf"], "Host": "tenant-e2e-a.local"},
            json={
                "amount": 200.00,
                "idempotency_key": "payout_rejection_key_01",
                "request_note": "Request for rejection test",
            },
        )
        assert p_res.status_code == 201
        payout_id = p_res.json()["id"]

        # Balance dropped to ₹300.00
        b2 = (await client.get("/api/v1/payouts/eligible-balance", cookies={"community_session": e2e_harness["agent1_token"]}, headers={"Host": "tenant-e2e-a.local"})).json()
        assert Decimal(str(b2["available_to_withdraw"])) == Decimal("300.00")
        assert Decimal(str(b2["pending_payout_amount"])) == Decimal("200.00")

        # Community Admin rejects payout
        rej_res = await client.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            cookies={"community_session": e2e_harness["admin_a_token"]},
            headers={"X-CSRF-Token": e2e_harness["admin_a_csrf"], "Host": "tenant-e2e-a.local"},
            json={
                "decision": "REJECT",
                "reason": "Bank IFSC code does not match branch record.",
            },
        )
        assert rej_res.status_code == 200
        assert rej_res.json()["status"] == "REJECTED"

        # Balance fully restored to ₹500.00
        b3 = (await client.get("/api/v1/payouts/eligible-balance", cookies={"community_session": e2e_harness["agent1_token"]}, headers={"Host": "tenant-e2e-a.local"})).json()
        assert Decimal(str(b3["available_to_withdraw"])) == Decimal("500.00")
        assert Decimal(str(b3["pending_payout_amount"])) == Decimal("0.00")


@pytest.mark.anyio
async def test_payout_cancellation_and_compensating_reversal_e2e(e2e_harness):
    """Verifies that an agent can cancel their pending request and funds are restored."""
    app = e2e_harness["app"]
    factory = e2e_harness["factory"]
    tenant_a_id = e2e_harness["tenant_a_id"]
    agent1_mem_id = e2e_harness["agent1_mem_id"]

    # Pre-credit agent with ₹300 commission
    fund_agent_commission(factory, tenant_a_id, agent1_mem_id, Decimal("300.00"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Submit payout request for ₹150.00
        p_res = await client.post(
            "/api/v1/payouts/requests",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={"X-CSRF-Token": e2e_harness["agent1_csrf"], "Host": "tenant-e2e-a.local"},
            json={
                "amount": 150.00,
                "idempotency_key": "payout_cancel_key_01",
                "request_note": "Request for cancel test",
            },
        )
        assert p_res.status_code == 201
        payout_id = p_res.json()["id"]

        # Cancel pending payout
        c_res = await client.post(
            f"/api/v1/payouts/requests/{payout_id}/cancel",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={"X-CSRF-Token": e2e_harness["agent1_csrf"], "Host": "tenant-e2e-a.local"},
            json={"reason": "Agent requested cancellation before review."},
        )
        assert c_res.status_code == 200
        assert c_res.json()["status"] == "CANCELLED"

        # Balance restored to ₹300.00
        b = (await client.get("/api/v1/payouts/eligible-balance", cookies={"community_session": e2e_harness["agent1_token"]}, headers={"Host": "tenant-e2e-a.local"})).json()
        assert Decimal(str(b["available_to_withdraw"])) == Decimal("300.00")
        assert Decimal(str(b["pending_payout_amount"])) == Decimal("0.00")


@pytest.mark.anyio
async def test_platform_disbursement_failure_and_compensating_reversal_e2e(e2e_harness):
    """Verifies that platform recording disbursement failure releases escrow back to agent."""
    app = e2e_harness["app"]
    factory = e2e_harness["factory"]
    tenant_a_id = e2e_harness["tenant_a_id"]
    agent1_mem_id = e2e_harness["agent1_mem_id"]

    # Pre-credit agent with ₹400 commission
    fund_agent_commission(factory, tenant_a_id, agent1_mem_id, Decimal("400.00"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Submit payout request for ₹250.00
        p_res = await client.post(
            "/api/v1/payouts/requests",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={"X-CSRF-Token": e2e_harness["agent1_csrf"], "Host": "tenant-e2e-a.local"},
            json={
                "amount": 250.00,
                "idempotency_key": "payout_fail_key_01",
                "request_note": "Request for fail test",
            },
        )
        payout_id = p_res.json()["id"]

        # Admin approves payout
        await client.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            cookies={"community_session": e2e_harness["admin_a_token"]},
            headers={"X-CSRF-Token": e2e_harness["admin_a_csrf"], "Host": "tenant-e2e-a.local"},
            json={"decision": "APPROVE", "reason": "Approved for disbursement."},
        )

        # Super Admin records disbursement failure (e.g. Beneficiary bank account frozen)
        fail_res = await client.post(
            f"/api/v1/platform/payouts/requests/{payout_id}/fail",
            cookies={"community_platform_session": e2e_harness["super_token"]},
            headers={"X-CSRF-Token": e2e_harness["super_csrf"]},
            json={"reason": "IMPS rejection: Beneficiary bank account is dormant."},
        )
        assert fail_res.status_code == 200
        assert fail_res.json()["status"] == "FAILED"

        # Balance restored to ₹400.00
        b = (await client.get("/api/v1/payouts/eligible-balance", cookies={"community_session": e2e_harness["agent1_token"]}, headers={"Host": "tenant-e2e-a.local"})).json()
        assert Decimal(str(b["available_to_withdraw"])) == Decimal("400.00")
        assert Decimal(str(b["pending_payout_amount"])) == Decimal("0.00")


@pytest.mark.anyio
async def test_cross_tenant_strict_isolation_e2e(e2e_harness):
    """Verifies that Tenant B cannot view or act on Tenant A's subscriptions, orders, or payouts."""
    app = e2e_harness["app"]
    factory = e2e_harness["factory"]
    tenant_a_id = e2e_harness["tenant_a_id"]
    agent1_mem_id = e2e_harness["agent1_mem_id"]

    # Fund Tenant A agent and create payout via API
    fund_agent_commission(factory, tenant_a_id, agent1_mem_id, Decimal("100.00"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create a payout for Tenant A
        p_res = await client.post(
            "/api/v1/payouts/requests",
            cookies={"community_session": e2e_harness["agent1_token"]},
            headers={"X-CSRF-Token": e2e_harness["agent1_csrf"], "Host": "tenant-e2e-a.local"},
            json={
                "amount": 50.00,
                "idempotency_key": "cross_tenant_iso_payout_e2e",
                "request_note": "Tenant A isolated payout",
            },
        )
        assert p_res.status_code == 201
        payout_a_id = p_res.json()["id"]

        # Tenant B agent attempts to view Tenant A's payout
        get_res = await client.get(
            f"/api/v1/payouts/requests/{payout_a_id}",
            cookies={"community_session": e2e_harness["agent_b_token"]},
            headers={"Host": "tenant-e2e-b.local"},
        )
        assert get_res.status_code == 404

        # Tenant B agent attempts to review Tenant A's payout
        rev_res = await client.post(
            f"/api/v1/payouts/requests/{payout_a_id}/review",
            cookies={"community_session": e2e_harness["agent_b_token"]},
            headers={"X-CSRF-Token": e2e_harness["agent_b_csrf"], "Host": "tenant-e2e-b.local"},
            json={"decision": "APPROVE", "reason": "Cross-tenant intrusion attempt"},
        )
        assert rev_res.status_code in (403, 404)
