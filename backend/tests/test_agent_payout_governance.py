"""Tests for Phase 4 Stage 4.4 Agent Payout Governance & Workflow.

Covers:
1. Authoritative eligible balance calculation.
2. Payout request creation with double-entry PAYOUT_HOLD escrow journal.
3. Overdraft prevention through FinancialLedgerService enforcement.
4. Idempotency key replay protection without double-debit.
5. Strict anti-self-approval enforcement (HTTP 403 SELF_APPROVAL_PROHIBITED).
6. Community Admin rejection with compensating REVERSAL journal restoring agent balance.
7. Community Admin approval workflow.
8. Platform Super Admin disbursement with mandatory disbursement_reference and PAYOUT_DISBURSEMENT journal.
9. Platform Super Admin disbursement failure with compensating REVERSAL journal.
10. Agent cancellation of pending requests with compensating REVERSAL journal.
11. Terminal state conflict prevention (HTTP 409 INVALID_STATE_TRANSITION).
12. Cross-tenant isolation (Tenant B cannot access or review Tenant A payouts).
13. Platform endpoint authorization (only Platform Super Admin can disburse).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
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
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import (
    get_authorization_repository,
    require_session_capability,
)
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.repository import SQLAlchemyAuthorizationRepository
from app.modules.authorization.seeding import seed_tenant_role_permissions
from app.modules.commissions.models import (
    AccountStatus,
    AccountType,
    CommissionAccount,
    CommissionJournal,
    CommissionLedgerEntry,
    EntryType,
    JournalReferenceType,
    JournalStatus,
)
from app.modules.commissions.schemas import JournalCreateRequest, JournalEntryLineSpec
from app.modules.commissions.service import FinancialLedgerService
from app.modules.identity.models import TenantMembership, User
from app.modules.payouts.models import PayoutRequest, PayoutStatus
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.tenants.models import Tenant


@pytest.fixture
def payout_harness():
    fd, path = tempfile.mkstemp(suffix="_test_payout_gov.db")
    os.close(fd)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    tenant_a_id = uuid4()
    tenant_b_id = uuid4()

    # Credentials
    super_user_id = uuid4()
    super_token = "super_session_token_payout"
    super_digest = hashlib.sha256(super_token.encode("utf-8")).hexdigest()
    super_csrf = hashlib.sha256(b"super_csrf_payout").hexdigest()

    # Tenant A Admin 1
    admin_a1_token = "admin_a1_session_token"
    admin_a1_digest = hashlib.sha256(admin_a1_token.encode("utf-8")).hexdigest()
    admin_a1_csrf = hashlib.sha256(b"admin_a1_csrf").hexdigest()
    admin_a1_user_id = uuid4()
    admin_a1_mem_id = uuid4()

    # Tenant A Admin 2 (also an agent for testing anti-self-approval)
    admin_a2_token = "admin_a2_session_token"
    admin_a2_digest = hashlib.sha256(admin_a2_token.encode("utf-8")).hexdigest()
    admin_a2_csrf = hashlib.sha256(b"admin_a2_csrf").hexdigest()
    admin_a2_user_id = uuid4()
    admin_a2_mem_id = uuid4()
    admin_a2_profile_id = uuid4()

    # Tenant A Agent 1 (pure agent)
    agent_a1_token = "agent_a1_session_token"
    agent_a1_digest = hashlib.sha256(agent_a1_token.encode("utf-8")).hexdigest()
    agent_a1_csrf = hashlib.sha256(b"agent_a1_csrf").hexdigest()
    agent_a1_user_id = uuid4()
    agent_a1_mem_id = uuid4()
    agent_a1_profile_id = uuid4()

    # Tenant B Admin & Agent
    admin_b_token = "admin_b_session_token"
    admin_b_digest = hashlib.sha256(admin_b_token.encode("utf-8")).hexdigest()
    admin_b_csrf = hashlib.sha256(b"admin_b_csrf").hexdigest()
    admin_b_user_id = uuid4()
    admin_b_mem_id = uuid4()

    agent_b_token = "agent_b_session_token"
    agent_b_digest = hashlib.sha256(agent_b_token.encode("utf-8")).hexdigest()
    agent_b_csrf = hashlib.sha256(b"agent_b_csrf").hexdigest()
    agent_b_user_id = uuid4()
    agent_b_mem_id = uuid4()
    agent_b_profile_id = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # Tenants
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="comm-payout-a",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        tenant_b = Tenant(
            id=tenant_b_id,
            slug="comm-payout-b",
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

        # Seed roles for Tenant A and B
        admin_role_a, agent_role_a = seed_tenant_role_permissions(session, tenant_a_id)
        admin_role_b, agent_role_b = seed_tenant_role_permissions(session, tenant_b_id)
        session.flush()

        # Tenant A Admin 1
        user_a1 = User(id=admin_a1_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_a1)
        session.flush()
        mem_a1 = TenantMembership(
            id=admin_a1_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_a1_user_id,
            mobile_e164="+919811111111",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_a1)
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a1_mem_id,
                role_id=admin_role_a.id,
                assigned_by_membership_id=admin_a1_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a1_mem_id,
                token_digest=admin_a1_digest,
                csrf_token=admin_a1_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        # Tenant A Admin 2 (Dual Admin + Agent)
        user_a2 = User(id=admin_a2_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_a2)
        session.flush()
        mem_a2 = TenantMembership(
            id=admin_a2_mem_id,
            tenant_id=tenant_a_id,
            user_id=admin_a2_user_id,
            mobile_e164="+919822222222",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_a2)
        session.flush()
        # Assigned BOTH Admin and Agent roles
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a2_mem_id,
                role_id=admin_role_a.id,
                assigned_by_membership_id=admin_a1_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a2_mem_id,
                role_id=agent_role_a.id,
                assigned_by_membership_id=admin_a1_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AgentProfile(
                id=admin_a2_profile_id,
                tenant_id=tenant_a_id,
                membership_id=admin_a2_mem_id,
                user_id=admin_a2_user_id,
                first_name="AdminAgent",
                last_name="A2",
                ref_id="AGT-DUAL-A2",
                profile_photo_key="photo_a2",
                aadhaar_number_ciphertext="enc_a2",
                aadhaar_number_masked="XXXX-XXXX-2222",
                aadhaar_document_key="doc_a2",
                pan_number_ciphertext="enc_pan_a2",
                pan_number_masked="ABCDE2222F",
                pan_document_key="doc_pan_a2",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=admin_a2_mem_id,
                token_digest=admin_a2_digest,
                csrf_token=admin_a2_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        # Tenant A Agent 1 (Pure Agent)
        user_agent_a1 = User(id=agent_a1_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_agent_a1)
        session.flush()
        mem_agent_a1 = TenantMembership(
            id=agent_a1_mem_id,
            tenant_id=tenant_a_id,
            user_id=agent_a1_user_id,
            mobile_e164="+919833333333",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_agent_a1)
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=agent_a1_mem_id,
                role_id=agent_role_a.id,
                assigned_by_membership_id=admin_a1_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AgentProfile(
                id=agent_a1_profile_id,
                tenant_id=tenant_a_id,
                membership_id=agent_a1_mem_id,
                user_id=agent_a1_user_id,
                first_name="PureAgent",
                last_name="A1",
                ref_id="AGT-PURE-A1",
                profile_photo_key="photo_a1",
                aadhaar_number_ciphertext="enc_a1",
                aadhaar_number_masked="XXXX-XXXX-3333",
                aadhaar_document_key="doc_a1",
                pan_number_ciphertext="enc_pan_a1",
                pan_number_masked="ABCDE3333F",
                pan_document_key="doc_pan_a1",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=agent_a1_mem_id,
                token_digest=agent_a1_digest,
                csrf_token=agent_a1_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        # Tenant B Admin & Agent
        user_b = User(id=admin_b_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_b)
        session.flush()
        mem_b = TenantMembership(
            id=admin_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=admin_b_user_id,
            mobile_e164="+919844444444",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_b)
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=admin_b_mem_id,
                role_id=admin_role_b.id,
                assigned_by_membership_id=admin_b_mem_id,
                status="active",
                valid_from=now - timedelta(days=1),
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=admin_b_mem_id,
                token_digest=admin_b_digest,
                csrf_token=admin_b_csrf,
                created_at=now,
                expires_at=now + timedelta(hours=2),
            )
        )

        user_agent_b = User(id=agent_b_user_id, status="active", created_at=now, updated_at=now)
        session.add(user_agent_b)
        session.flush()
        mem_agent_b = TenantMembership(
            id=agent_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=agent_b_user_id,
            mobile_e164="+919855555555",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(mem_agent_b)
        session.flush()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=agent_b_mem_id,
                role_id=agent_role_b.id,
                assigned_by_membership_id=admin_b_mem_id,
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
                aadhaar_number_ciphertext="enc_b",
                aadhaar_number_masked="XXXX-XXXX-4444",
                aadhaar_document_key="doc_b",
                pan_number_ciphertext="enc_pan_b",
                pan_number_masked="ABCDE4444F",
                pan_document_key="doc_pan_b",
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
        jwt_secret_key="test-secret-key-payout-governance",
        csrf_secret_key="test-csrf-secret-key-payout",
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

    yield {
        "engine": engine,
        "factory": factory,
        "app": app,
        "tenant_a_id": tenant_a_id,
        "tenant_b_id": tenant_b_id,
        "super_token": super_token,
        "super_csrf": super_csrf,
        "admin_a1_token": admin_a1_token,
        "admin_a1_csrf": admin_a1_csrf,
        "admin_a1_mem_id": admin_a1_mem_id,
        "admin_a2_token": admin_a2_token,
        "admin_a2_csrf": admin_a2_csrf,
        "admin_a2_mem_id": admin_a2_mem_id,
        "agent_a1_token": agent_a1_token,
        "agent_a1_csrf": agent_a1_csrf,
        "agent_a1_mem_id": agent_a1_mem_id,
        "agent_a1_user_id": agent_a1_user_id,
        "admin_b_token": admin_b_token,
        "admin_b_csrf": admin_b_csrf,
        "agent_b_token": agent_b_token,
        "agent_b_csrf": agent_b_csrf,
        "agent_b_mem_id": agent_b_mem_id,
    }

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def fund_agent_account(factory: sessionmaker, tenant_id: UUID, agent_mem_id: UUID, amount: Decimal):
    """Utility to post an initial double-entry commission journal to fund an agent."""
    ledger = FinancialLedgerService()
    with factory() as session:
        prof = session.execute(
            select(AgentProfile).where(AgentProfile.membership_id == agent_mem_id)
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

        # Post journal: debit COMMUNITY_CLEARING, credit AGENT_COMMISSION
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
                        entry_type=EntryType.DEBIT,
                        amount=amount,
                    ),
                    JournalEntryLineSpec(
                        account_id=agent_acct.id,
                        entry_type=EntryType.CREDIT,
                        amount=amount,
                    ),
                ],
            ),
        )
        session.commit()


# =============================================================================
# 1. Authoritative Eligible Balance Calculation
# =============================================================================
@pytest.mark.anyio
async def test_agent_eligible_balance_calculation(payout_harness):
    h = payout_harness
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client:
        # Initially 0 balance
        res = await client.get("/api/v1/payouts/eligible-balance")
        assert res.status_code == 200, res.text
        data = res.json()
        assert Decimal(str(data["current_balance"])) == Decimal("0.00")
        assert Decimal(str(data["available_to_withdraw"])) == Decimal("0.00")

        # Fund agent with 100.00 (₹100)
        fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("100.00"))

        # Re-check balance: now 100.00
        res = await client.get("/api/v1/payouts/eligible-balance")
        assert res.status_code == 200, res.text
        data = res.json()
        assert Decimal(str(data["current_balance"])) == Decimal("100.00")
        assert Decimal(str(data["available_to_withdraw"])) == Decimal("100.00")


# =============================================================================
# 2. Payout Request Creation & Double-Entry Escrow Hold
# =============================================================================
@pytest.mark.anyio
async def test_payout_request_creation_and_escrow_hold(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("100.00"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client:
        res = await client.post(
            "/api/v1/payouts/requests",
            json={
                "amount": 40.0,
                "idempotency_key": "idemp-req-001-abc",
                "request_note": "Requesting 40 INR payout",
                "bank_account_details": {
                    "account_number": "1234567890",
                    "ifsc": "HDFC0001234",
                    "account_holder_name": "Agent A1",
                },
            },
        )
        assert res.status_code == 201, res.text
        payout = res.json()
        assert payout["status"] == "PENDING_REVIEW"
        assert Decimal(str(payout["amount"])) == Decimal("40.00")
        assert payout["idempotency_key"] == "idemp-req-001-abc"

        # Check balance: available balance should now be 60.0 (100 - 40 hold)
        bal_res = await client.get("/api/v1/payouts/eligible-balance")
        assert bal_res.status_code == 200
        assert Decimal(str(bal_res.json()["available_to_withdraw"])) == Decimal("60.00")

        # Verify ledger journal entry directly in DB
        with h["factory"]() as session:
            journals = session.execute(
                select(CommissionJournal).where(
                    CommissionJournal.tenant_id == h["tenant_a_id"],
                    CommissionJournal.reference_type == JournalReferenceType.PAYOUT_HOLD.value,
                    CommissionJournal.reference_id == str(payout["id"]),
                )
            ).scalars().all()
            assert len(journals) == 1
            j = journals[0]
            assert j.status == JournalStatus.POSTED
            assert len(j.entries) == 2

            # Debit AGENT_COMMISSION, Credit PAYOUT_CLEARING
            debit_entry = next(e for e in j.entries if e.entry_type == EntryType.DEBIT)
            credit_entry = next(e for e in j.entries if e.entry_type == EntryType.CREDIT)

            assert debit_entry.amount == Decimal("40.00")
            assert credit_entry.amount == Decimal("40.00")
            assert debit_entry.account.account_type == AccountType.AGENT_COMMISSION
            assert credit_entry.account.account_type == AccountType.PAYOUT_CLEARING


# =============================================================================
# 3. Overdraft Prevention (Double-Entry Rejection)
# =============================================================================
@pytest.mark.anyio
async def test_payout_request_overdraft_prevention(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("50.00"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client:
        # Attempt to withdraw 70.00 (more than available 50.00)
        res = await client.post(
            "/api/v1/payouts/requests",
            json={
                "amount": 70.0,
                "idempotency_key": "idemp-overdraft-test",
                "request_note": "Trying to overdraft",
            },
        )
        assert res.status_code == 422, res.text

        # Balance remains 50.00
        bal_res = await client.get("/api/v1/payouts/eligible-balance")
        assert Decimal(str(bal_res.json()["available_to_withdraw"])) == Decimal("50.00")


# =============================================================================
# 4. Idempotency Key Replay Protection
# =============================================================================
@pytest.mark.anyio
async def test_payout_idempotency_replay(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("100.00"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client:
        # First attempt
        res1 = await client.post(
            "/api/v1/payouts/requests",
            json={
                "amount": 30.0,
                "idempotency_key": "idemp-same-key-999",
                "request_note": "Attempt 1",
            },
        )
        assert res1.status_code == 201
        payout1_id = res1.json()["id"]

        # Replay attempt with same idempotency key
        res2 = await client.post(
            "/api/v1/payouts/requests",
            json={
                "amount": 30.0,
                "idempotency_key": "idemp-same-key-999",
                "request_note": "Attempt 1 replay",
            },
        )
        assert res2.status_code in (200, 201)  # Returns existing record
        payout2 = res2.json()
        assert payout2["id"] == payout1_id

        # Verify balance only deducted 30.0 once (remaining 70.0, not 40.0)
        bal_res = await client.get("/api/v1/payouts/eligible-balance")
        assert Decimal(str(bal_res.json()["available_to_withdraw"])) == Decimal("70.00")


# =============================================================================
# 5. Strict Anti-Self-Approval Enforcement
# =============================================================================
@pytest.mark.anyio
async def test_strict_anti_self_approval_enforcement(payout_harness):
    h = payout_harness
    # Fund Admin A2 (who has both Admin and Agent roles)
    fund_agent_account(h["factory"], h["tenant_a_id"], h["admin_a2_mem_id"], Decimal("100.00"))

    # Admin A2 submits a payout request as an agent
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a2_token"]},
        headers={"x-csrf-token": h["admin_a2_csrf"]},
    ) as client_a2:
        res = await client_a2.post(
            "/api/v1/payouts/requests",
            json={
                "amount": 50.0,
                "idempotency_key": "idemp-dual-a2-001",
                "request_note": "Dual role agent request",
            },
        )
        assert res.status_code == 201, res.text
        payout_id = res.json()["id"]

        # Admin A2 attempts to approve their own payout request
        approve_res = await client_a2.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            json={"decision": "APPROVE", "reason": "Self-approval justification", "notes": "Trying to self-approve"},
        )
        assert approve_res.status_code == 403, approve_res.text

        # Admin A2 attempts to reject their own payout request
        reject_res = await client_a2.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            json={"decision": "REJECT", "reason": "Self-reject justification", "notes": "Trying to self-reject"},
        )
        assert reject_res.status_code == 403

    # Now, a DIFFERENT admin (Admin A1) reviews and approves the request
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a1_token"]},
        headers={"x-csrf-token": h["admin_a1_csrf"]},
    ) as client_a1:
        valid_review = await client_a1.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            json={"decision": "APPROVE", "reason": "Approved by independent Admin A1", "notes": "All checks passed"},
        )
        assert valid_review.status_code == 200, valid_review.text
        assert valid_review.json()["status"] == "APPROVED"
        assert valid_review.json()["reviewed_by_membership_id"] == str(h["admin_a1_mem_id"])


# =============================================================================
# 6. Community Admin Rejection & Compensating Ledger Reversal
# =============================================================================
@pytest.mark.anyio
async def test_community_admin_rejection_and_ledger_reversal(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("80.00"))

    # Agent submits request for 30.00
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client_agent:
        res = await client_agent.post(
            "/api/v1/payouts/requests",
            json={"amount": 30.0, "idempotency_key": "idemp-reject-test", "request_note": "Requesting 30"},
        )
        assert res.status_code == 201
        payout_id = res.json()["id"]

        bal_during = await client_agent.get("/api/v1/payouts/eligible-balance")
        assert Decimal(str(bal_during.json()["available_to_withdraw"])) == Decimal("50.00")

    # Admin rejects request
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a1_token"]},
        headers={"x-csrf-token": h["admin_a1_csrf"]},
    ) as client_admin:
        reject_res = await client_admin.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            json={"decision": "REJECT", "reason": "KYC incomplete", "notes": "Please re-upload PAN card"},
        )
        assert reject_res.status_code == 200, reject_res.text
        assert reject_res.json()["status"] == "REJECTED"
        assert reject_res.json()["rejection_reason"] == "KYC incomplete"

    # Agent's balance is restored back to 80.00 via compensating reversal journal
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client_agent:
        bal_after = await client_agent.get("/api/v1/payouts/eligible-balance")
        assert Decimal(str(bal_after.json()["available_to_withdraw"])) == Decimal("80.00")

    # Verify compensating REVERSAL journal in DB
    with h["factory"]() as session:
        reversal_journals = session.execute(
            select(CommissionJournal).where(
                CommissionJournal.tenant_id == h["tenant_a_id"],
                CommissionJournal.reference_type == JournalReferenceType.REVERSAL.value,
                CommissionJournal.reference_id == str(payout_id),
            )
        ).scalars().all()
        assert len(reversal_journals) == 1
        rj = reversal_journals[0]
        # Reversal debits PAYOUT_CLEARING and credits AGENT_COMMISSION
        debit_e = next(e for e in rj.entries if e.entry_type == EntryType.DEBIT)
        credit_e = next(e for e in rj.entries if e.entry_type == EntryType.CREDIT)
        assert debit_e.account.account_type == AccountType.PAYOUT_CLEARING
        assert credit_e.account.account_type == AccountType.AGENT_COMMISSION
        assert debit_e.amount == Decimal("30.00")
        assert credit_e.amount == Decimal("30.00")


# =============================================================================
# 7. Platform Super Admin Disbursement Workflow
# =============================================================================
@pytest.mark.anyio
async def test_platform_super_admin_disbursement(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("100.00"))

    # Agent requests payout of 50.00
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client_agent:
        res = await client_agent.post(
            "/api/v1/payouts/requests",
            json={"amount": 50.0, "idempotency_key": "idemp-disburse-001", "request_note": "Request 50 INR"},
        )
        assert res.status_code == 201
        payout_id = res.json()["id"]

    # Admin approves
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a1_token"]},
        headers={"x-csrf-token": h["admin_a1_csrf"]},
    ) as client_admin:
        app_res = await client_admin.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            json={"decision": "APPROVE", "reason": "All checks passed", "notes": "Approved for IMPS"},
        )
        assert app_res.status_code == 200

    # Platform Super Admin executes disbursement
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as client_super:
        # Missing disbursement reference must fail
        fail_disburse = await client_super.post(
            f"/api/v1/platform/payouts/requests/{payout_id}/disburse",
            json={"disbursement_reference": ""},
        )
        assert fail_disburse.status_code == 422

        # Valid disbursement with UTR reference
        valid_disburse = await client_super.post(
            f"/api/v1/platform/payouts/requests/{payout_id}/disburse",
            json={
                "disbursement_reference": "UTR-HDFC-20261021-0001",
                "disbursement_method": "IMPS",
            },
        )
        assert valid_disburse.status_code == 200, valid_disburse.text
        paid_payout = valid_disburse.json()
        assert paid_payout["status"] == "PAID"
        assert paid_payout["disbursement_reference"] == "UTR-HDFC-20261021-0001"
        assert paid_payout["disbursed_at"] is not None

    # Verify double-entry PAYOUT_DISBURSEMENT journal
    with h["factory"]() as session:
        disb_journals = session.execute(
            select(CommissionJournal).where(
                CommissionJournal.tenant_id == h["tenant_a_id"],
                CommissionJournal.reference_type == JournalReferenceType.PAYOUT_DISBURSEMENT.value,
                CommissionJournal.reference_id == str(payout_id),
            )
        ).scalars().all()
        assert len(disb_journals) == 1
        dj = disb_journals[0]
        # Debits PAYOUT_CLEARING, Credits COMMUNITY_CLEARING
        debit_e = next(e for e in dj.entries if e.entry_type == EntryType.DEBIT)
        credit_e = next(e for e in dj.entries if e.entry_type == EntryType.CREDIT)
        assert debit_e.account.account_type == AccountType.PAYOUT_CLEARING
        assert credit_e.account.account_type == AccountType.COMMUNITY_CLEARING
        assert debit_e.amount == Decimal("50.00")
        assert credit_e.amount == Decimal("50.00")


# =============================================================================
# 8. Platform Super Admin Disbursement Failure & Reversal
# =============================================================================
@pytest.mark.anyio
async def test_platform_super_admin_disbursement_failure_reversal(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("100.00"))

    # Agent requests 60.00
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client_agent:
        res = await client_agent.post(
            "/api/v1/payouts/requests",
            json={"amount": 60.0, "idempotency_key": "idemp-fail-disburse-001"},
        )
        payout_id = res.json()["id"]

    # Admin approves
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a1_token"]},
        headers={"x-csrf-token": h["admin_a1_csrf"]},
    ) as client_admin:
        await client_admin.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            json={"decision": "APPROVE", "reason": "Pre-approved by admin"},
        )

    # Super Admin marks disbursement as failed (e.g. bank rejected)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_platform_session": h["super_token"]},
        headers={"x-csrf-token": h["super_csrf"]},
    ) as client_super:
        fail_res = await client_super.post(
            f"/api/v1/platform/payouts/requests/{payout_id}/fail",
            json={"reason": "Beneficiary account frozen by bank"},
        )
        assert fail_res.status_code == 200, fail_res.text
        assert fail_res.json()["status"] == "FAILED"
        assert fail_res.json()["failure_reason"] == "Beneficiary account frozen by bank"

    # Agent's balance is fully restored to 100.00
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client_agent:
        bal = await client_agent.get("/api/v1/payouts/eligible-balance")
        assert Decimal(str(bal.json()["available_to_withdraw"])) == Decimal("100.00")


# =============================================================================
# 9. Agent Cancellation Before Review
# =============================================================================
@pytest.mark.anyio
async def test_agent_cancellation_before_review(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("100.00"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client:
        # Create request
        res = await client.post(
            "/api/v1/payouts/requests",
            json={"amount": 45.0, "idempotency_key": "idemp-cancel-001", "request_note": "Requesting 45"},
        )
        payout_id = res.json()["id"]

        bal_before = await client.get("/api/v1/payouts/eligible-balance")
        assert Decimal(str(bal_before.json()["available_to_withdraw"])) == Decimal("55.00")

        # Agent cancels request
        cancel_res = await client.post(
            f"/api/v1/payouts/requests/{payout_id}/cancel",
            json={"reason": "Changed my mind"},
        )
        assert cancel_res.status_code == 200, cancel_res.text
        assert cancel_res.json()["status"] == "CANCELLED"

        # Balance restored to 100.0
        bal_after = await client.get("/api/v1/payouts/eligible-balance")
        assert Decimal(str(bal_after.json()["available_to_withdraw"])) == Decimal("100.00")


# =============================================================================
# 10. Terminal State Conflict Protection (HTTP 409)
# =============================================================================
@pytest.mark.anyio
async def test_terminal_state_conflict_protection(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("100.00"))

    # Create and reject request
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client_agent:
        res = await client_agent.post(
            "/api/v1/payouts/requests",
            json={"amount": 20.0, "idempotency_key": "idemp-terminal-001"},
        )
        payout_id = res.json()["id"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a1_token"]},
        headers={"x-csrf-token": h["admin_a1_csrf"]},
    ) as client_admin:
        await client_admin.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            json={"decision": "REJECT", "reason": "Initial rejection"},
        )

        # Attempt to review a rejected (terminal) request must return 409
        conflict_res = await client_admin.post(
            f"/api/v1/payouts/requests/{payout_id}/review",
            json={"decision": "APPROVE", "reason": "Late approval attempt"},
        )
        assert conflict_res.status_code == 409, conflict_res.text


# =============================================================================
# 11. Cross-Tenant Isolation
# =============================================================================
@pytest.mark.anyio
async def test_cross_tenant_isolation_payouts(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("100.00"))

    # Agent A1 creates payout request in Tenant A
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client_agent_a:
        res = await client_agent_a.post(
            "/api/v1/payouts/requests",
            json={"amount": 25.0, "idempotency_key": "idemp-cross-tenant-001"},
        )
        payout_a_id = res.json()["id"]

    # Tenant B Admin attempts to access or review Tenant A's payout request
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_b_token"]},
        headers={"x-csrf-token": h["admin_b_csrf"]},
    ) as client_admin_b:
        # GET should be 404
        get_res = await client_admin_b.get(f"/api/v1/payouts/requests/{payout_a_id}")
        assert get_res.status_code == 404

        # POST review should be 404
        review_res = await client_admin_b.post(
            f"/api/v1/payouts/requests/{payout_a_id}/review",
            json={"decision": "APPROVE", "reason": "Malicious cross-tenant review"},
        )
        assert review_res.status_code == 404

        # List in Tenant B should not contain Tenant A's payout
        list_res = await client_admin_b.get("/api/v1/payouts/requests")
        assert list_res.status_code == 200
        items = list_res.json()["items"]
        assert all(item["id"] != payout_a_id for item in items)


# =============================================================================
# 12. Unauthorized Platform Disbursement (RBAC Guard)
# =============================================================================
@pytest.mark.anyio
async def test_unauthorized_platform_disbursement(payout_harness):
    h = payout_harness
    fund_agent_account(h["factory"], h["tenant_a_id"], h["agent_a1_mem_id"], Decimal("100.00"))

    # Agent creates request
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["agent_a1_token"]},
        headers={"x-csrf-token": h["agent_a1_csrf"]},
    ) as client_agent:
        res = await client_agent.post(
            "/api/v1/payouts/requests",
            json={"amount": 35.0, "idempotency_key": "idemp-unauth-disburse-001"},
        )
        payout_id = res.json()["id"]

    # Admin A attempts to call Platform Super Admin disbursement endpoint
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h["app"]),
        base_url="http://testserver",
        cookies={"community_session": h["admin_a1_token"]},
        headers={"x-csrf-token": h["admin_a1_csrf"]},
    ) as client_admin:
        disburse_attempt = await client_admin.post(
            f"/api/v1/platform/payouts/requests/{payout_id}/disburse",
            json={"disbursement_reference": "ILLEGAL-REF-001"},
        )
        assert disburse_attempt.status_code == 403
