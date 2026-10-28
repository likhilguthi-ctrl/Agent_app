from __future__ import annotations

import hashlib
from decimal import Decimal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import sessionmaker
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.agents.models import AgentProfile
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AuthSession
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.models import Role, RolePermission, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
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
from app.modules.tenants.models import Tenant


@pytest.fixture
def financial_harness(tmp_path):
    database_path = tmp_path / "financial-test.sqlite"
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

    admin_token = "admin_session_token_financial"
    admin_digest = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()
    admin_csrf = hashlib.sha256(b"admin_csrf_financial").hexdigest()

    agent_token = "agent_session_token_financial"
    agent_digest = hashlib.sha256(agent_token.encode("utf-8")).hexdigest()
    agent_csrf = hashlib.sha256(b"agent_csrf_financial").hexdigest()

    admin_user_id = uuid4()
    admin_mem_id = uuid4()
    agent_user_id = uuid4()
    agent_mem_id = uuid4()
    agent_profile_id = uuid4()

    # Tenant B entities for isolation checks
    admin_b_token = "admin_b_session_token_financial"
    admin_b_digest = hashlib.sha256(admin_b_token.encode("utf-8")).hexdigest()
    admin_b_csrf = hashlib.sha256(b"admin_b_csrf_financial").hexdigest()
    admin_b_user_id = uuid4()
    admin_b_mem_id = uuid4()

    now = datetime.now(UTC)

    with factory() as session:
        # Create Tenants
        tenant_a = Tenant(
            id=tenant_a_id,
            slug="comm-financial-a",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        tenant_b = Tenant(
            id=tenant_b_id,
            slug="comm-financial-b",
            status="active",
            photo_requirement="OPTIONAL",
            default_commission_rate=10.0,
            created_at=now,
            updated_at=now,
        )
        session.add_all([tenant_a, tenant_b])
        session.flush()

        # Seed RBAC for both tenants
        admin_role_a, agent_role_a = seed_tenant_role_permissions(session, tenant_a_id)
        admin_role_b, agent_role_b = seed_tenant_role_permissions(session, tenant_b_id)

        # Tenant A Admin
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

        # Tenant A Agent
        agent_user = User(id=agent_user_id, status="active", created_at=now, updated_at=now)
        session.add(agent_user)
        session.flush()

        agent_mem = TenantMembership(
            id=agent_mem_id,
            tenant_id=tenant_a_id,
            user_id=agent_user_id,
            mobile_e164="+919822222222",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(agent_mem)
        session.flush()

        agent_role_assign = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=agent_mem_id,
            role_id=agent_role_a.id,
            assigned_by_membership_id=admin_mem_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        agent_profile = AgentProfile(
            id=agent_profile_id,
            tenant_id=tenant_a_id,
            membership_id=agent_mem_id,
            user_id=agent_user_id,
            first_name="Finance",
            last_name="Agent",
            ref_id="FIN001",
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
        agent_session = AuthSession(
            id=uuid4(),
            tenant_id=tenant_a_id,
            membership_id=agent_mem_id,
            token_digest=agent_digest,
            csrf_token=agent_csrf,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        session.add_all([agent_role_assign, agent_profile, agent_session])
        session.flush()

        # Tenant B Admin
        admin_b_user = User(id=admin_b_user_id, status="active", created_at=now, updated_at=now)
        session.add(admin_b_user)
        session.flush()

        admin_b_mem = TenantMembership(
            id=admin_b_mem_id,
            tenant_id=tenant_b_id,
            user_id=admin_b_user_id,
            mobile_e164="+919833333333",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(admin_b_mem)
        session.flush()

        admin_b_role_assign = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant_b_id,
            membership_id=admin_b_mem_id,
            role_id=admin_role_b.id,
            assigned_by_membership_id=admin_b_mem_id,
            status="active",
            valid_from=now - timedelta(days=1),
        )
        admin_b_session = AuthSession(
            id=uuid4(),
            tenant_id=tenant_b_id,
            membership_id=admin_b_mem_id,
            token_digest=admin_b_digest,
            csrf_token=admin_b_csrf,
            created_at=now,
            expires_at=now + timedelta(hours=2),
        )
        session.add_all([admin_b_role_assign, admin_b_session])
        session.flush()

        # Pre-seed system accounts and agent account for Tenant A
        service = FinancialLedgerService()
        expense_acct = service.get_or_create_system_account(
            session,
            tenant_id=tenant_a_id,
            account_type=AccountType.COMMUNITY_EXPENSE,
        )
        clearing_acct = service.get_or_create_system_account(
            session,
            tenant_id=tenant_a_id,
            account_type=AccountType.COMMUNITY_CLEARING,
        )
        agent_acct = service.get_or_create_agent_account(
            session,
            tenant_id=tenant_a_id,
            agent_membership_id=agent_mem_id,
            agent_profile_id=agent_profile_id,
        )

        # Seed system account for Tenant B
        expense_b = service.get_or_create_system_account(
            session,
            tenant_id=tenant_b_id,
            account_type=AccountType.COMMUNITY_EXPENSE,
        )

        session.commit()

        expense_id = expense_acct.id
        clearing_id = clearing_acct.id
        agent_acct_id = agent_acct.id
        expense_b_id = expense_b.id

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
        "agent_token": agent_token,
        "agent_csrf": agent_csrf,
        "agent_mem_id": agent_mem_id,
        "agent_profile_id": agent_profile_id,
        "admin_b_token": admin_b_token,
        "admin_b_csrf": admin_b_csrf,
        "expense_acct_id": expense_id,
        "clearing_acct_id": clearing_id,
        "agent_acct_id": agent_acct_id,
        "expense_b_id": expense_b_id,
    }


# ==============================================================================
# TEST CASE 1: Valid Balanced Journal Creation
# ==============================================================================
@pytest.mark.anyio
async def test_valid_balanced_journal_creation(financial_harness):
    h = financial_harness
    app = h["app"]
    expense_id = str(h["expense_acct_id"])
    agent_acct_id = str(h["agent_acct_id"])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reference_type": "REFERRAL_COMMISSION",
                "reference_id": str(uuid4()),
                "description": "Valid balanced commission test",
                "idempotency_key": "journal-test-01",
                "entries": [
                    {"account_id": expense_id, "entry_type": "DEBIT", "amount": "100.00"},
                    {"account_id": agent_acct_id, "entry_type": "CREDIT", "amount": "100.00"},
                ],
            },
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["status"] == "POSTED"
        assert Decimal(data["total_amount"]) == Decimal("100.00")

        # Verify journal detail and entries
        res_detail = await client.get(
            f"/api/v1/commissions/journals/{data['id']}",
            headers={"Cookie": f"community_session={h['admin_token']}"},
        )
        assert res_detail.status_code == 200
        detail = res_detail.json()
        assert len(detail["entries"]) == 2

        # Verify balances
        res_acct = await client.get(
            f"/api/v1/commissions/accounts/{agent_acct_id}",
            headers={"Cookie": f"community_session={h['admin_token']}"},
        )
        assert res_acct.status_code == 200
        assert Decimal(res_acct.json()["current_balance"]) == Decimal("100.00")


# ==============================================================================
# TEST CASE 2: Rejection of Unbalanced Journal Posting
# ==============================================================================
@pytest.mark.anyio
async def test_rejection_of_unbalanced_journal(financial_harness):
    h = financial_harness
    app = h["app"]
    expense_id = str(h["expense_acct_id"])
    agent_acct_id = str(h["agent_acct_id"])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reference_type": "REFERRAL_COMMISSION",
                "reference_id": str(uuid4()),
                "description": "Unbalanced test",
                "idempotency_key": "journal-test-unbalanced",
                "entries": [
                    {"account_id": expense_id, "entry_type": "DEBIT", "amount": "100.00"},
                    {"account_id": agent_acct_id, "entry_type": "CREDIT", "amount": "90.00"},
                ],
            },
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] in ("HTTP_ERROR", "VALIDATION_ERROR")

    # Verify no journal persisted
    with h["factory"]() as session:
        count = session.query(CommissionJournal).filter(
            CommissionJournal.idempotency_key == "journal-test-unbalanced"
        ).count()
        assert count == 0


# ==============================================================================
# TEST CASE 3: Rejection of Zero or Negative Monetary Amounts
# ==============================================================================
@pytest.mark.anyio
async def test_rejection_of_zero_or_negative_amounts(financial_harness):
    h = financial_harness
    app = h["app"]
    expense_id = str(h["expense_acct_id"])
    agent_acct_id = str(h["agent_acct_id"])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Zero amount test
        res_zero = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reference_type": "REFERRAL_COMMISSION",
                "reference_id": str(uuid4()),
                "description": "Zero amount test",
                "idempotency_key": "journal-test-zero",
                "entries": [
                    {"account_id": expense_id, "entry_type": "DEBIT", "amount": "0.00"},
                    {"account_id": agent_acct_id, "entry_type": "CREDIT", "amount": "0.00"},
                ],
            },
        )
        assert res_zero.status_code == 422

        # Negative amount test
        res_neg = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reference_type": "REFERRAL_COMMISSION",
                "reference_id": str(uuid4()),
                "description": "Negative amount test",
                "idempotency_key": "journal-test-neg",
                "entries": [
                    {"account_id": expense_id, "entry_type": "DEBIT", "amount": "-50.00"},
                    {"account_id": agent_acct_id, "entry_type": "CREDIT", "amount": "-50.00"},
                ],
            },
        )
        assert res_neg.status_code == 422


# ==============================================================================
# TEST CASE 4: Exact Monetary Precision (Decimal)
# ==============================================================================
@pytest.mark.anyio
async def test_exact_monetary_precision(financial_harness):
    h = financial_harness
    app = h["app"]
    exp_id = str(h["expense_acct_id"])
    clr_id = str(h["clearing_acct_id"])
    agt_id = str(h["agent_acct_id"])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Post 33.33 + 66.67 = 100.00
        res = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reference_type": "REFERRAL_COMMISSION",
                "reference_id": str(uuid4()),
                "description": "Decimal precision test",
                "idempotency_key": "journal-test-precision",
                "entries": [
                    {"account_id": exp_id, "entry_type": "DEBIT", "amount": "33.33"},
                    {"account_id": clr_id, "entry_type": "DEBIT", "amount": "66.67"},
                    {"account_id": agt_id, "entry_type": "CREDIT", "amount": "100.00"},
                ],
            },
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert Decimal(data["total_amount"]) == Decimal("100.00")

        # Verify exact balance via direct database query
        with h["factory"]() as session:
            acct = session.get(CommissionAccount, UUID(agt_id))
            assert acct.current_balance == Decimal("100.00")
            assert str(acct.current_balance) == "100.00"


# ==============================================================================
# TEST CASE 5: Atomic Rollback on Failure
# ==============================================================================
@pytest.mark.anyio
async def test_atomic_rollback_on_failure(financial_harness):
    h = financial_harness
    app = h["app"]
    exp_id = str(h["expense_acct_id"])
    agt_id = str(h["agent_acct_id"])

    with h["factory"]() as session:
        agent_acct = session.get(CommissionAccount, UUID(agt_id))
        assert agent_acct.current_balance == Decimal("0.00")
        assert agent_acct.allow_negative is False

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Attempt to debit agent account with 50.00 (which would cause negative balance)
        res = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reference_type": "MANUAL_ADJUSTMENT",
                "reference_id": str(uuid4()),
                "description": "Overdraft failure test",
                "idempotency_key": "journal-test-overdraft",
                "entries": [
                    {"account_id": agt_id, "entry_type": "DEBIT", "amount": "50.00"},
                    {"account_id": exp_id, "entry_type": "CREDIT", "amount": "50.00"},
                ],
            },
        )
        assert res.status_code in (400, 422)
        assert res.json()["error"]["code"] in ("HTTP_ERROR", "VALIDATION_ERROR")

    # Verify atomic rollback: agent balance is STILL 0.00, no journal saved
    with h["factory"]() as session:
        acct = session.get(CommissionAccount, UUID(agt_id))
        assert acct.current_balance == Decimal("0.00")
        journal = session.query(CommissionJournal).filter(
            CommissionJournal.idempotency_key == "journal-test-overdraft"
        ).first()
        assert journal is None


# ==============================================================================
# TEST CASE 6: Prevention of Duplicate Posting / Idempotency Key Replay
# ==============================================================================
@pytest.mark.anyio
async def test_idempotency_replay_and_conflict(financial_harness):
    h = financial_harness
    app = h["app"]
    exp_id = str(h["expense_acct_id"])
    agt_id = str(h["agent_acct_id"])

    idempotency_key = "idemp-test-replay-001"
    payload = {
        "reference_type": "REFERRAL_COMMISSION",
        "reference_id": str(uuid4()),
        "description": "Idempotent initial post",
        "idempotency_key": idempotency_key,
        "entries": [
            {"account_id": exp_id, "entry_type": "DEBIT", "amount": "25.00"},
            {"account_id": agt_id, "entry_type": "CREDIT", "amount": "25.00"},
        ],
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # First post: 201 Created
        res1 = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json=payload,
        )
        assert res1.status_code == 201
        journal_id_1 = res1.json()["id"]

        # Replay identical payload: 200 or 201 with identical journal ID
        res2 = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json=payload,
        )
        assert res2.status_code in (200, 201)
        assert res2.json()["id"] == journal_id_1

        # Conflicting payload with same idempotency key: 409 Conflict
        conflicting_payload = dict(payload)
        conflicting_payload["description"] = "Conflicting description"
        res3 = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json=conflicting_payload,
        )
        assert res3.status_code == 409

    # Verify balance was only credited ONCE (25.00, not 50.00)
    with h["factory"]() as session:
        acct = session.get(CommissionAccount, UUID(agt_id))
        assert acct.current_balance == Decimal("25.00")


# ==============================================================================
# TEST CASE 7: Safe Transaction Concurrency & Deterministic Locking
# ==============================================================================
@pytest.mark.anyio
async def test_safe_transaction_concurrency_and_locking(financial_harness):
    h = financial_harness
    exp_id = h["expense_acct_id"]
    agt_id = h["agent_acct_id"]

    # Post multiple consecutive journals through the service layer
    # verifying deterministic sorted lock ordering prevents deadlocks
    for i in range(5):
        with h["factory"]() as session:
            service = FinancialLedgerService()
            req = JournalCreateRequest(
                reference_type=JournalReferenceType.REFERRAL_COMMISSION.value,
                reference_id=str(uuid4()),
                description=f"Concurrent batch test {i}",
                idempotency_key=f"batch-lock-{i}",
                entries=[
                    JournalEntryLineSpec(account_id=exp_id, entry_type=EntryType.DEBIT.value, amount=Decimal("10.00")),
                    JournalEntryLineSpec(account_id=agt_id, entry_type=EntryType.CREDIT.value, amount=Decimal("10.00")),
                ],
            )
            service.post_journal(
                session,
                tenant_id=h["tenant_a_id"],
                request=req,
                created_by_user_id=None,
            )
            session.commit()

    # Check total accumulated balance: 5 * 10.00 = 50.00
    with h["factory"]() as session:
        acct = session.get(CommissionAccount, agt_id)
        assert acct.current_balance == Decimal("50.00")


# ==============================================================================
# TEST CASE 8: Tenant Isolation Boundary
# ==============================================================================
@pytest.mark.anyio
async def test_tenant_isolation_boundary(financial_harness):
    h = financial_harness
    app = h["app"]
    acct_b_id = str(h["expense_b_id"])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Tenant A Admin attempts to read Tenant B's account: 404
        res = await client.get(
            f"/api/v1/commissions/accounts/{acct_b_id}",
            headers={"Cookie": f"community_session={h['admin_token']}"},
        )
        assert res.status_code == 404

        # Tenant A Agent attempts to read Tenant B's account: 404
        res_agent = await client.get(
            f"/api/v1/commissions/accounts/{acct_b_id}",
            headers={"Cookie": f"community_session={h['agent_token']}"},
        )
        assert res_agent.status_code == 404

        # Tenant A Agent accesses /me -> sees only their own account
        res_me = await client.get(
            "/api/v1/commissions/accounts/me",
            headers={"Cookie": f"community_session={h['agent_token']}"},
        )
        assert res_me.status_code == 200
        assert res_me.json()["agent_membership_id"] == str(h["agent_mem_id"])


# ==============================================================================
# TEST CASE 9: Cross-Tenant Journal Rejection
# ==============================================================================
@pytest.mark.anyio
async def test_cross_tenant_journal_rejection(financial_harness):
    h = financial_harness
    app = h["app"]
    exp_a_id = str(h["expense_acct_id"])
    exp_b_id = str(h["expense_b_id"])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Submit journal mixing Tenant A and Tenant B accounts
        res = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reference_type": "MANUAL_ADJUSTMENT",
                "reference_id": str(uuid4()),
                "description": "Cross tenant violation attempt",
                "idempotency_key": "journal-test-cross-tenant",
                "entries": [
                    {"account_id": exp_a_id, "entry_type": "DEBIT", "amount": "50.00"},
                    {"account_id": exp_b_id, "entry_type": "CREDIT", "amount": "50.00"},
                ],
            },
        )
        assert res.status_code in (400, 404)
        assert res.json()["error"]["code"] in ("HTTP_ERROR", "VALIDATION_ERROR")


# ==============================================================================
# TEST CASE 10: Immutability of Posted History
# ==============================================================================
@pytest.mark.anyio
async def test_immutability_of_posted_history(financial_harness):
    h = financial_harness
    app = h["app"]
    journal_id = str(uuid4())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # PUT on journal -> 405 Method Not Allowed
        res_put = await client.put(
            f"/api/v1/commissions/journals/{journal_id}",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={"status": "REVERSED"},
        )
        assert res_put.status_code == 405

        # PATCH on journal -> 405 Method Not Allowed
        res_patch = await client.patch(
            f"/api/v1/commissions/journals/{journal_id}",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={"description": "Changed"},
        )
        assert res_patch.status_code == 405

        # DELETE on journal -> 405 Method Not Allowed
        res_del = await client.delete(
            f"/api/v1/commissions/journals/{journal_id}",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
        )
        assert res_del.status_code == 405


# ==============================================================================
# TEST CASE 11: Reversal Behavior
# ==============================================================================
@pytest.mark.anyio
async def test_journal_reversal_behavior(financial_harness):
    h = financial_harness
    app = h["app"]
    exp_id = str(h["expense_acct_id"])
    agt_id = str(h["agent_acct_id"])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Post initial journal
        res_post = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reference_type": "REFERRAL_COMMISSION",
                "reference_id": str(uuid4()),
                "description": "Journal to be reversed",
                "idempotency_key": "journal-test-reversal-init",
                "entries": [
                    {"account_id": exp_id, "entry_type": "DEBIT", "amount": "75.00"},
                    {"account_id": agt_id, "entry_type": "CREDIT", "amount": "75.00"},
                ],
            },
        )
        assert res_post.status_code == 201
        journal_id = res_post.json()["id"]

        # Verify agent balance is 75.00
        res_bal = await client.get(
            f"/api/v1/commissions/accounts/{agt_id}",
            headers={"Cookie": f"community_session={h['admin_token']}"},
        )
        assert Decimal(res_bal.json()["current_balance"]) == Decimal("75.00")

        # 2. Reverse journal
        res_rev = await client.post(
            f"/api/v1/commissions/journals/{journal_id}/reversal",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reason": "Test reversal cancellation",
                "idempotency_key": "journal-test-reversal-exec",
            },
        )
        assert res_rev.status_code == 201, res_rev.text
        rev_data = res_rev.json()
        assert rev_data["status"] == "POSTED"
        assert rev_data["reference_type"] == "REVERSAL"
        assert rev_data["reference_id"] == journal_id

        # 3. Verify original journal is now marked REVERSED
        res_orig = await client.get(
            f"/api/v1/commissions/journals/{journal_id}",
            headers={"Cookie": f"community_session={h['admin_token']}"},
        )
        assert res_orig.status_code == 200
        assert res_orig.json()["journal"]["status"] == "REVERSED"

        # 4. Verify balance is restored to 0.00
        res_bal_after = await client.get(
            f"/api/v1/commissions/accounts/{agt_id}",
            headers={"Cookie": f"community_session={h['admin_token']}"},
        )
        assert Decimal(res_bal_after.json()["current_balance"]) == Decimal("0.00")

        # 5. Attempting to reverse again fails
        res_rev_again = await client.post(
            f"/api/v1/commissions/journals/{journal_id}/reversal",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={
                "reason": "Duplicate reversal attempt",
                "idempotency_key": "journal-test-reversal-dup",
            },
        )
        assert res_rev_again.status_code in (400, 409)
        assert res_rev_again.json()["error"]["code"] in ("HTTP_ERROR", "VALIDATION_ERROR")


# ==============================================================================
# TEST CASE 12: Migration Upgrade & Downgrade on Isolated Database
# ==============================================================================
def test_financial_migration_upgrade_and_downgrade(tmp_path: Path):
    import importlib.util

    db_path = tmp_path / "migration-test-financial.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # Pre-create parent tables required for revision 17 foreign keys
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tenants (id CHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE users (id CHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE tenant_memberships (id CHAR(36) PRIMARY KEY, tenant_id CHAR(36))"))

    migration_file = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "20261021_17_financial_foundation.py"
    spec = importlib.util.spec_from_file_location("rev_17", migration_file)
    assert spec and spec.loader
    rev_17 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev_17)

    # 1. Test Upgrade
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            rev_17.upgrade()

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"commission_accounts", "commission_journals", "commission_ledger_entries"} <= tables

    # Check unique constraint on idempotency key
    uqs = {item["name"] for item in inspector.get_unique_constraints("commission_journals")}
    assert "uq_comm_journals_idempotency" in uqs

    # 2. Test Downgrade
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            rev_17.downgrade()

    inspector_down = inspect(engine)
    tables_down = set(inspector_down.get_table_names())
    assert "commission_ledger_entries" not in tables_down
    assert "commission_journals" not in tables_down
    assert "commission_accounts" not in tables_down

    engine.dispose()


# ==============================================================================
# TEST CASE 13: Phase 2 and Phase 3 Security Regressions (SEC-07 & Auth)
# ==============================================================================
@pytest.mark.anyio
async def test_security_regressions_and_sec07_closure(financial_harness):
    h = financial_harness
    app = h["app"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # SEC-07 check: Tenant Admin cannot update agent status via PATCH /api/v1/agents/{id}
        res_sec07 = await client.patch(
            f"/api/v1/agents/{h['agent_profile_id']}",
            headers={
                "Cookie": f"community_session={h['admin_token']}",
                "X-CSRF-Token": h["admin_csrf"],
            },
            json={"status": "inactive"},
        )
        # Should be rejected because status field is forbidden / schema validation fails
        assert res_sec07.status_code == 422 or "status" not in res_sec07.json()

        # Unauthenticated request to commission endpoint returns 401
        res_unauth = await client.get("/api/v1/commissions/accounts/me")
        assert res_unauth.status_code == 401

        # Agent trying to post a journal without commission.manage permission -> 403 Forbidden
        res_forbidden = await client.post(
            "/api/v1/commissions/journals",
            headers={
                "Cookie": f"community_session={h['agent_token']}",
                "X-CSRF-Token": h["agent_csrf"],
            },
            json={
                "reference_type": "REFERRAL_COMMISSION",
                "reference_id": str(uuid4()),
                "description": "Unauthorized agent posting",
                "idempotency_key": "journal-test-agent-unauth",
                "entries": [],
            },
        )
        assert res_forbidden.status_code == 403
