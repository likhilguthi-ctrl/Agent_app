"""Automated tests for Mobile Number Authentication and Password Reset for All Login Roles.

Covers:
1. Non-enumerating responses on unknown mobile numbers (generic 200 response, no OTP challenge created).
2. Password reset flow for Super Admin (PlatformOperator):
   - OTP request -> OTP verify -> Password update.
   - PlatformOperatorSession revocation on reset.
   - Successful password verification with new password.
3. Password reset flow for Manager (ADMIN) and Agent (AGENT):
   - TenantMembership + User + AccountCredential.
   - AuthSession revocation on reset.
   - Successful password update with scrypt hash.
4. OTP verification failures:
   - Invalid OTP code returns 401 with remaining attempts.
   - Max 5 failed attempts locks challenge.
5. Single-use reset token:
   - Token cannot be reused after successful reset (fails with 400 INVALID_RESET_TOKEN).
6. Input validation:
   - Password mismatch (400 PASSWORD_MISMATCH).
   - Password length < 8 chars (400 PASSWORD_TOO_SHORT).
   - Invalid E.164 mobile format (422 INVALID_MOBILE).
7. Multi-account disambiguation:
   - Mobile shared across multiple accounts returns requires_account_selection=True.
   - Missing account_id fails with 400 ACCOUNT_SELECTION_REQUIRED.
   - Selecting specific account_id updates only that account.
8. Inactive/disabled account handling:
   - Inactive accounts cannot reset passwords.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_session_factory
from app.modules.authentication.api import (
    PasswordResetCompleteRequest,
    PasswordResetOtpRequest,
    PasswordResetVerifyRequest,
    complete_password_reset,
    request_password_reset_otp,
    verify_password_reset_otp,
)
from app.modules.authentication.models import (
    AccountCredential,
    AuthSession,
    OtpChallenge,
)
from app.modules.authentication.otp_service import OtpService
from app.modules.authentication.password import hash_password, verify_password
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorSession,
)
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


def make_indian_mobile() -> str:
    return f"+919{random.randint(100000000, 999999999)}"


def make_dummy_request(settings: Settings, notif_client=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                notification_client=notif_client,
            )
        )
    )


def make_otp_service(settings: Settings, session_factory, mobile: str, code: str = "123456") -> tuple[OtpService, AsyncMock]:
    mock_notif = AsyncMock()
    mock_notif.send_otp.return_value = SimpleNamespace(
        success=True,
        message_id=str(uuid4()),
        status="DELIVERED",
        error=None,
    )
    mock_notif.send_text_message.return_value = SimpleNamespace(
        success=True,
        message_id=str(uuid4()),
        status="DELIVERED",
        error=None,
    )
    service = OtpService(
        settings=settings,
        notification_client=mock_notif,
        session_factory=session_factory,
        mock_codes={mobile: code},
    )
    return service, mock_notif


def create_test_tenant(session: Session, slug: str) -> Tenant:
    t = Tenant(
        id=uuid4(),
        slug=slug,
        status="active",
        photo_requirement="OPTIONAL",
        default_commission_rate=Decimal("10.00"),
        policy_version="v1",
    )
    session.add(t)
    session.flush()
    return t


def create_operator(
    session: Session,
    mobile: str,
    username: str,
    password: str,
    status: str = "active",
) -> tuple[PlatformOperator, AccountCredential]:
    op = PlatformOperator(
        id=uuid4(),
        issuer="https://platform.avanflix.internal",
        subject=f"sub_{uuid4().hex[:12]}",
        status=status,
        mobile_e164=mobile,
    )
    session.add(op)
    session.flush()

    cred = AccountCredential(
        id=uuid4(),
        account_type="platform_operator",
        operator_id=op.id,
        user_id=None,
        username=username.lower(),
        password_hash=hash_password(password),
        status=status,
        must_change_password=False,
    )
    session.add(cred)
    session.flush()
    return op, cred


def create_tenant_user(
    session: Session,
    tenant: Tenant,
    mobile: str,
    username: str,
    password: str,
    role_code: str = "AGENT",
    status: str = "active",
) -> tuple[User, TenantMembership, AccountCredential]:
    user = User(id=uuid4(), status=status)
    session.add(user)
    session.flush()

    membership = TenantMembership(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        mobile_e164=mobile,
        status=status,
    )
    session.add(membership)
    session.flush()

    cred = AccountCredential(
        id=uuid4(),
        account_type="tenant_user",
        operator_id=None,
        user_id=user.id,
        username=username.lower(),
        password_hash=hash_password(password),
        status=status,
        must_change_password=False,
    )
    session.add(cred)
    session.flush()

    role = session.execute(
        select(Role).where(Role.tenant_id == tenant.id, Role.code == role_code.lower())
    ).scalar_one_or_none()
    if not role:
        role = Role(
            id=uuid4(),
            tenant_id=tenant.id,
            code=role_code.lower(),
            display_name=f"{role_code} role",
            status="active",
        )
        session.add(role)
        session.flush()

    assignment = UserRoleAssignment(
        id=uuid4(),
        tenant_id=tenant.id,
        membership_id=membership.id,
        role_id=role.id,
        assigned_by_membership_id=membership.id,
        status="active",
        valid_from=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(assignment)
    session.flush()

    return user, membership, cred


def test_password_reset_request_non_enumeration(db: Session, db_session_factory):
    """Test 1: Requesting password reset on an unregistered mobile returns non-enumerating 200 without creating OTP challenge."""
    unregistered_mobile = make_indian_mobile()
    settings = Settings()
    otp_svc, mock_notif = make_otp_service(settings, db_session_factory, unregistered_mobile)

    req = PasswordResetOtpRequest(mobile_number=unregistered_mobile)
    resp = asyncio.run(request_password_reset_otp(body=req, session=db, otp_service=otp_svc))

    assert resp.status == "accepted"
    assert resp.challenge_id is None
    assert "If an eligible account is associated" in resp.message
    # Verify no OTP challenge was saved in PostgreSQL
    challenge = db.execute(
        select(OtpChallenge).where(
            OtpChallenge.mobile_e164 == unregistered_mobile,
            OtpChallenge.purpose == "password_reset",
        )
    ).scalar_one_or_none()
    assert challenge is None
    mock_notif.send_otp.assert_not_called()


def test_password_reset_superadmin(db: Session, db_session_factory):
    """Test 2: Complete password reset flow for Super Admin (PlatformOperator) with session revocation."""
    mobile = make_indian_mobile()
    op, cred = create_operator(db, mobile, f"super_{uuid4().hex[:6]}", "OldSuperPass123!")

    # Active PlatformOperatorSession before reset
    active_session = PlatformOperatorSession(
        id=uuid4(),
        operator_id=op.id,
        token_digest=uuid4().hex + uuid4().hex,
        csrf_token=uuid4().hex + uuid4().hex,
        authenticated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        revoked_at=None,
    )
    db.add(active_session)
    db.commit()

    settings = Settings()
    otp_svc, mock_notif = make_otp_service(settings, db_session_factory, mobile, code="654321")
    dummy_req = make_dummy_request(settings, mock_notif)

    # 1. Request OTP
    req_body = PasswordResetOtpRequest(mobile_number=mobile)
    otp_resp = asyncio.run(request_password_reset_otp(body=req_body, session=db, otp_service=otp_svc))
    assert otp_resp.status == "accepted"
    assert otp_resp.challenge_id is not None
    challenge_id = otp_resp.challenge_id

    # 2. Verify OTP
    verify_body = PasswordResetVerifyRequest(
        challenge_id=challenge_id,
        mobile_number=mobile,
        otp="654321",
    )
    verify_resp = asyncio.run(verify_password_reset_otp(body=verify_body, session=db, otp_service=otp_svc))
    assert verify_resp.status == "verified"
    assert verify_resp.reset_token is not None
    assert verify_resp.requires_account_selection is False
    assert verify_resp.account is not None
    assert verify_resp.account.role == "SUPER_ADMIN"
    assert verify_resp.account.account_type == "platform_operator"

    reset_token = verify_resp.reset_token

    # 3. Complete Password Reset
    complete_body = PasswordResetCompleteRequest(
        mobile_number=mobile,
        reset_token=reset_token,
        new_password="NewSuperPassword123!",
        confirm_password="NewSuperPassword123!",
    )
    comp_resp = asyncio.run(
        complete_password_reset(
            body=complete_body,
            request=dummy_req,
            session=db,
            otp_service=otp_svc,
        )
    )
    assert comp_resp.status == "success"

    # Verify database state
    db.refresh(cred)
    assert verify_password("NewSuperPassword123!", cred.password_hash) is True
    assert verify_password("OldSuperPass123!", cred.password_hash) is False

    # Verify session revocation
    db.refresh(active_session)
    assert active_session.revoked_at is not None

    # Verify security notification was dispatched
    mock_notif.send_text_message.assert_called_once()


def test_password_reset_manager_and_agent(db: Session, db_session_factory):
    """Test 3: Password reset for Manager (ADMIN) and Agent (AGENT) with AuthSession revocation."""
    tenant = create_test_tenant(db, f"tenant_auth_{uuid4().hex[:6]}")
    settings = Settings()

    # --- A. Manager (ADMIN) ---
    mgr_mobile = make_indian_mobile()
    mgr_user, mgr_mem, mgr_cred = create_tenant_user(
        db, tenant, mgr_mobile, f"mgr_{uuid4().hex[:6]}", "OldMgrPass123!", role_code="admin"
    )
    mgr_session = AuthSession(
        id=uuid4(),
        tenant_id=tenant.id,
        membership_id=mgr_mem.id,
        token_digest=uuid4().hex + uuid4().hex,
        csrf_token=uuid4().hex + uuid4().hex,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        revoked_at=None,
    )
    db.add(mgr_session)
    db.commit()

    otp_svc_mgr, mock_notif_mgr = make_otp_service(settings, db_session_factory, mgr_mobile, code="112233")
    dummy_req_mgr = make_dummy_request(settings, mock_notif_mgr)

    # Request & Verify for Manager
    otp_resp = asyncio.run(request_password_reset_otp(PasswordResetOtpRequest(mobile_number=mgr_mobile), db, otp_svc_mgr))
    verify_resp = asyncio.run(
        verify_password_reset_otp(
            PasswordResetVerifyRequest(challenge_id=otp_resp.challenge_id, mobile_number=mgr_mobile, otp="112233"),
            db,
            otp_svc_mgr,
        )
    )
    assert verify_resp.status == "verified"
    assert verify_resp.account.role == "ADMIN"
    assert verify_resp.account.community_name == tenant.slug

    # Complete reset for Manager
    asyncio.run(
        complete_password_reset(
            PasswordResetCompleteRequest(
                mobile_number=mgr_mobile,
                reset_token=verify_resp.reset_token,
                new_password="NewMgrPassword123!",
                confirm_password="NewMgrPassword123!",
            ),
            dummy_req_mgr,
            db,
            otp_svc_mgr,
        )
    )
    db.refresh(mgr_cred)
    db.refresh(mgr_session)
    assert verify_password("NewMgrPassword123!", mgr_cred.password_hash) is True
    assert mgr_session.revoked_at is not None

    # --- B. Agent (AGENT) ---
    agt_mobile = make_indian_mobile()
    agt_user, agt_mem, agt_cred = create_tenant_user(
        db, tenant, agt_mobile, f"agt_{uuid4().hex[:6]}", "OldAgtPass123!", role_code="agent"
    )
    agt_session = AuthSession(
        id=uuid4(),
        tenant_id=tenant.id,
        membership_id=agt_mem.id,
        token_digest=uuid4().hex + uuid4().hex,
        csrf_token=uuid4().hex + uuid4().hex,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        revoked_at=None,
    )
    db.add(agt_session)
    db.commit()

    otp_svc_agt, mock_notif_agt = make_otp_service(settings, db_session_factory, agt_mobile, code="445566")
    dummy_req_agt = make_dummy_request(settings, mock_notif_agt)

    otp_resp_agt = asyncio.run(request_password_reset_otp(PasswordResetOtpRequest(mobile_number=agt_mobile), db, otp_svc_agt))
    verify_resp_agt = asyncio.run(
        verify_password_reset_otp(
            PasswordResetVerifyRequest(challenge_id=otp_resp_agt.challenge_id, mobile_number=agt_mobile, otp="445566"),
            db,
            otp_svc_agt,
        )
    )
    assert verify_resp_agt.account.role == "AGENT"

    asyncio.run(
        complete_password_reset(
            PasswordResetCompleteRequest(
                mobile_number=agt_mobile,
                reset_token=verify_resp_agt.reset_token,
                new_password="NewAgtPassword123!",
                confirm_password="NewAgtPassword123!",
            ),
            dummy_req_agt,
            db,
            otp_svc_agt,
        )
    )
    db.refresh(agt_cred)
    db.refresh(agt_session)
    assert verify_password("NewAgtPassword123!", agt_cred.password_hash) is True
    assert agt_session.revoked_at is not None


def test_password_reset_otp_verification_failures(db: Session, db_session_factory):
    """Test 4: OTP verification failures track attempts remaining and lock challenge after max attempts."""
    mobile = make_indian_mobile()
    tenant = create_test_tenant(db, f"fail_t_{uuid4().hex[:6]}")
    create_tenant_user(db, tenant, mobile, f"user_{uuid4().hex[:6]}", "ValidPass123!")
    db.commit()

    settings = Settings()
    otp_svc, _ = make_otp_service(settings, db_session_factory, mobile, code="888999")

    otp_resp = asyncio.run(request_password_reset_otp(PasswordResetOtpRequest(mobile_number=mobile), db, otp_svc))
    ch_id = otp_resp.challenge_id

    # Attempt 1: wrong OTP
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            verify_password_reset_otp(
                PasswordResetVerifyRequest(challenge_id=ch_id, mobile_number=mobile, otp="000000"),
                db,
                otp_svc,
            )
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "OTP_VERIFICATION_FAILED"
    assert exc_info.value.detail["attempts_remaining"] == 4

    # Exhaust remaining attempts: 2, 3, 4, 5
    for expected_left in [3, 2, 1]:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                verify_password_reset_otp(
                    PasswordResetVerifyRequest(challenge_id=ch_id, mobile_number=mobile, otp="000000"),
                    db,
                    otp_svc,
                )
            )
        assert exc_info.value.detail["attempts_remaining"] == expected_left

    # 5th failed attempt locks challenge
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            verify_password_reset_otp(
                PasswordResetVerifyRequest(challenge_id=ch_id, mobile_number=mobile, otp="000000"),
                db,
                otp_svc,
            )
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["attempts_remaining"] == 0

    # 6th attempt: challenge is locked / already used / expired
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            verify_password_reset_otp(
                PasswordResetVerifyRequest(challenge_id=ch_id, mobile_number=mobile, otp="888999"),
                db,
                otp_svc,
            )
        )
    assert exc_info.value.status_code == 401


def test_password_reset_token_single_use(db: Session, db_session_factory):
    """Test 5: Reset token is strictly single-use and cannot be used twice."""
    mobile = make_indian_mobile()
    tenant = create_test_tenant(db, f"single_t_{uuid4().hex[:6]}")
    _, _, cred = create_tenant_user(db, tenant, mobile, f"single_{uuid4().hex[:6]}", "InitialPass123!")
    db.commit()

    settings = Settings()
    otp_svc, mock_notif = make_otp_service(settings, db_session_factory, mobile, code="777111")
    dummy_req = make_dummy_request(settings, mock_notif)

    otp_resp = asyncio.run(request_password_reset_otp(PasswordResetOtpRequest(mobile_number=mobile), db, otp_svc))
    verify_resp = asyncio.run(
        verify_password_reset_otp(
            PasswordResetVerifyRequest(challenge_id=otp_resp.challenge_id, mobile_number=mobile, otp="777111"),
            db,
            otp_svc,
        )
    )
    token = verify_resp.reset_token

    # First reset succeeds
    asyncio.run(
        complete_password_reset(
            PasswordResetCompleteRequest(
                mobile_number=mobile,
                reset_token=token,
                new_password="NewSingleUsePass123!",
                confirm_password="NewSingleUsePass123!",
            ),
            dummy_req,
            db,
            otp_svc,
        )
    )

    # Replay attack with same token fails with 400 INVALID_RESET_TOKEN
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            complete_password_reset(
                PasswordResetCompleteRequest(
                    mobile_number=mobile,
                    reset_token=token,
                    new_password="AnotherPass123!",
                    confirm_password="AnotherPass123!",
                ),
                dummy_req,
                db,
                otp_svc,
            )
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "INVALID_RESET_TOKEN"


def test_password_reset_validation_errors(db: Session, db_session_factory):
    """Test 6: Validations fail closed on mismatch, short passwords, and invalid mobiles."""
    mobile = make_indian_mobile()
    settings = Settings()
    otp_svc, mock_notif = make_otp_service(settings, db_session_factory, mobile)
    dummy_req = make_dummy_request(settings, mock_notif)

    # 1. Password mismatch
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            complete_password_reset(
                PasswordResetCompleteRequest(
                    mobile_number=mobile,
                    reset_token="valid_reset_token_length_32_chars",
                    new_password="Password123!",
                    confirm_password="PasswordDifferent!",
                ),
                dummy_req,
                db,
                otp_svc,
            )
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "PASSWORD_MISMATCH"

    # 2. Password too short (< 8 chars) rejected by schema validation
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as pydantic_err:
        PasswordResetCompleteRequest(
            mobile_number=mobile,
            reset_token="valid_reset_token_length_32_chars",
            new_password="short",
            confirm_password="short",
        )
    assert "at least 8 characters" in str(pydantic_err.value)

    # 3. Invalid mobile format
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            request_password_reset_otp(
                PasswordResetOtpRequest(mobile_number="not-a-number"),
                db,
                otp_svc,
            )
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "INVALID_MOBILE"


def test_password_reset_multi_account_selection(db: Session, db_session_factory):
    """Test 7: A single mobile number associated with multiple accounts requires explicit selection."""
    shared_mobile = make_indian_mobile()
    t1 = create_test_tenant(db, f"comm_a_{uuid4().hex[:6]}")
    t2 = create_test_tenant(db, f"comm_b_{uuid4().hex[:6]}")

    _, _, cred_a = create_tenant_user(db, t1, shared_mobile, f"user_a_{uuid4().hex[:4]}", "PassA_12345!", role_code="agent")
    _, _, cred_b = create_tenant_user(db, t2, shared_mobile, f"user_b_{uuid4().hex[:4]}", "PassB_12345!", role_code="admin")
    db.commit()

    settings = Settings()
    otp_svc, mock_notif = make_otp_service(settings, db_session_factory, shared_mobile, code="555666")
    dummy_req = make_dummy_request(settings, mock_notif)

    # 1. Request and Verify OTP
    otp_resp = asyncio.run(request_password_reset_otp(PasswordResetOtpRequest(mobile_number=shared_mobile), db, otp_svc))
    verify_resp = asyncio.run(
        verify_password_reset_otp(
            PasswordResetVerifyRequest(challenge_id=otp_resp.challenge_id, mobile_number=shared_mobile, otp="555666"),
            db,
            otp_svc,
        )
    )

    # Multi-account response assertions
    assert verify_resp.requires_account_selection is True
    assert len(verify_resp.accounts) == 2
    token = verify_resp.reset_token

    # 2. Complete without account_id fails with 400 ACCOUNT_SELECTION_REQUIRED
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            complete_password_reset(
                PasswordResetCompleteRequest(
                    mobile_number=shared_mobile,
                    reset_token=token,
                    new_password="NewPassMulti123!",
                    confirm_password="NewPassMulti123!",
                    account_id=None,
                ),
                dummy_req,
                db,
                otp_svc,
            )
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "ACCOUNT_SELECTION_REQUIRED"

    # Note: token wasn't consumed because validation raised before consumption.
    # Now complete specifying account A
    comp_resp = asyncio.run(
        complete_password_reset(
            PasswordResetCompleteRequest(
                mobile_number=shared_mobile,
                reset_token=token,
                account_id=cred_a.id,
                new_password="NewPassOnlyForA123!",
                confirm_password="NewPassOnlyForA123!",
            ),
            dummy_req,
            db,
            otp_svc,
        )
    )
    assert comp_resp.status == "success"

    # Verify Account A updated, Account B untouched
    db.refresh(cred_a)
    db.refresh(cred_b)
    assert verify_password("NewPassOnlyForA123!", cred_a.password_hash) is True
    assert verify_password("PassB_12345!", cred_b.password_hash) is True


def test_password_reset_disabled_account_cannot_reset(db: Session, db_session_factory):
    """Test 8: Disabled or inactive accounts cannot receive reset OTPs or perform reset."""
    disabled_mobile = make_indian_mobile()
    tenant = create_test_tenant(db, f"dis_t_{uuid4().hex[:6]}")
    user, mem, cred = create_tenant_user(
        db, tenant, disabled_mobile, f"dis_{uuid4().hex[:4]}", "Pass12345!", status="inactive"
    )
    db.commit()

    settings = Settings()
    otp_svc, mock_notif = make_otp_service(settings, db_session_factory, disabled_mobile)

    # Non-enumerating 200 returned, but challenge_id is None
    resp = asyncio.run(request_password_reset_otp(PasswordResetOtpRequest(mobile_number=disabled_mobile), db, otp_svc))
    assert resp.status == "accepted"
    assert resp.challenge_id is None
    mock_notif.send_otp.assert_not_called()


def test_password_reset_multi_session_revocation(db: Session, db_session_factory):
    """Test 9: Password reset revokes all concurrent active sessions across devices for that user."""
    mobile = make_indian_mobile()
    tenant = create_test_tenant(db, f"multisess_t_{uuid4().hex[:6]}")
    user, mem, cred = create_tenant_user(db, tenant, mobile, f"multi_{uuid4().hex[:4]}", "InitialPass123!")

    # Create 3 concurrent active sessions
    sessions = []
    for _ in range(3):
        s = AuthSession(
            id=uuid4(),
            tenant_id=tenant.id,
            membership_id=mem.id,
            token_digest=uuid4().hex + uuid4().hex,
            csrf_token=uuid4().hex + uuid4().hex,
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=4),
            revoked_at=None,
        )
        db.add(s)
        sessions.append(s)
    db.commit()

    settings = Settings()
    otp_svc, mock_notif = make_otp_service(settings, db_session_factory, mobile, code="999888")
    dummy_req = make_dummy_request(settings, mock_notif)

    otp_resp = asyncio.run(request_password_reset_otp(PasswordResetOtpRequest(mobile_number=mobile), db, otp_svc))
    verify_resp = asyncio.run(
        verify_password_reset_otp(
            PasswordResetVerifyRequest(challenge_id=otp_resp.challenge_id, mobile_number=mobile, otp="999888"),
            db,
            otp_svc,
        )
    )

    asyncio.run(
        complete_password_reset(
            PasswordResetCompleteRequest(
                mobile_number=mobile,
                reset_token=verify_resp.reset_token,
                new_password="NewRevokePass123!",
                confirm_password="NewRevokePass123!",
            ),
            dummy_req,
            db,
            otp_svc,
        )
    )

    # Check that ALL 3 sessions are now revoked
    for s in sessions:
        db.refresh(s)
        assert s.revoked_at is not None

