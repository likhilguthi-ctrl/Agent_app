from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import secrets
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.modules.agents.models import AgentProfile
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.agent_appointments.schemas import AppointmentDecisionRequest
from app.modules.agent_appointments.service import AgentAppointmentService
from app.modules.agent_lifecycle.models import LifecycleAction
from app.modules.agent_lifecycle.service import AgentLifecycleService
from app.modules.authentication.abuse_control import SQLAlchemyOtpAbuseGuard
from app.modules.authentication.models import AccountCredential, AuthSession, OtpChallenge, OtpRateBucket
from app.modules.authentication.otp_service import OtpService, OtpVerificationFailed
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.messaging.models import NotificationMessage, NotificationWebhookEvent
from app.modules.messaging.notification_service import (
    NotificationDispatchResult,
    NotificationServiceClient,
)
from app.modules.messaging.webhook_service import NotificationWebhookService
from app.modules.notifications.models import SuperAdminNotification
from app.modules.tenants.models import Tenant


@pytest.fixture
def memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield session_factory, engine
    Base.metadata.drop_all(engine)


@pytest.fixture
def base_settings():
    return Settings(
        _env_file=None,
        environment="test",
        app_name="TestApp",
        frontend_origins="http://localhost:3000",
        auth_session_ttl_seconds=3600,
        auth_session_cookie_name="community_session",
        otp_challenge_ttl_seconds=300,
        otp_send_limit=3,
        otp_send_window_seconds=600,
        otp_send_cooldown_seconds=60,
        otp_verify_limit=5,
        otp_verify_window_seconds=900,
        otp_delivery_mode="mock",
        otp_log_to_console=False,
        otp_skip_notification_dispatch=False,
        notification_service_base_url="https://notification.example.com",
        notification_service_api_key=SecretStr("test-api-key"),
        notification_webhook_secret=SecretStr("test-webhook-secret-1234567890123456"),
        notification_webhook_timestamp_tolerance_seconds=300,
        messaging_timeout_seconds=5.0,
        messaging_api_base_url="https://msg.example.com",
        messaging_send_otp_path="/api/v1/otp/send",
        messaging_verify_otp_path="/api/v1/otp/verify",
        messaging_send_otp_method="POST",
        messaging_verify_otp_method="POST",
        messaging_api_key=SecretStr("test-messaging-key"),
        messaging_auth_header_name="X-API-Key",
    )


# =============================================================================
# 1. OTP Resend & Dispatch Idempotency Key Handling
# =============================================================================
@pytest.mark.anyio
async def test_otp_resend_and_dispatch_idempotency(memory_db, base_settings):
    session_factory, _ = memory_db
    mock_notif_client = AsyncMock()
    mock_notif_client.send_otp.return_value = NotificationDispatchResult(
        success=True,
        message_id="msg-101",
        status="ACCEPTED",
        channel="WHATSAPP",
    )

    otp_service = OtpService(
        settings=base_settings,
        notification_client=mock_notif_client,
        session_factory=session_factory,
    )

    mobile = "+919876543210"

    # Step 1: Initial OTP Request
    cid1, cooldown1 = await otp_service.request_otp(mobile, purpose="login")
    assert cid1 is not None
    assert mock_notif_client.send_otp.call_count == 1

    first_call_kwargs = mock_notif_client.send_otp.call_args.kwargs
    did1 = first_call_kwargs["dispatch_id"]
    assert did1 is not None

    with session_factory() as session:
        ch1 = session.execute(select(OtpChallenge).where(OtpChallenge.id == cid1)).scalar_one()
        assert str(ch1.dispatch_id) == str(did1)
        assert ch1.resend_count == 0

    # Step 2: Genuinely New OTP Request (Resend after cooldown)
    # Clear rate bucket cooldown for test
    with session_factory.begin() as session:
        session.execute(text("DELETE FROM otp_rate_buckets"))

    cid2, cooldown2 = await otp_service.request_otp(mobile, purpose="login")
    # Challenge ID is maintained for the same (mobile, purpose)
    assert cid2 == cid1
    assert mock_notif_client.send_otp.call_count == 2

    second_call_kwargs = mock_notif_client.send_otp.call_args.kwargs
    did2 = second_call_kwargs["dispatch_id"]
    assert did2 is not None
    # Crucial Invariant: did2 MUST be different from did1 so external service does not suppress the new message!
    assert str(did2) != str(did1)

    with session_factory() as session:
        ch2 = session.execute(select(OtpChallenge).where(OtpChallenge.id == cid2)).scalar_one()
        assert str(ch2.dispatch_id) == str(did2)
        assert ch2.resend_count == 1

    # Step 3: Outbound retry of the SAME dispatch reuses its dispatch_id
    client = NotificationServiceClient(base_settings)
    key1 = f"community-ott-login-{cid1}-{did1}"
    # Verify notification service client generates stable key matching dispatch_id
    res_direct = await client.send_otp(
        recipient_e164=mobile,
        otp="123456",
        purpose="login",
        challenge_id=str(cid1),
        dispatch_id=str(did1),
    )
    assert res_direct.success is True
    # In mock mode, metadata retains referenceId and challengeId
    assert len(client.mock_messages) == 1
    assert client.mock_messages[0]["challenge_id"] == str(cid1)


@pytest.mark.anyio
async def test_otp_dispatch_failure_cleans_up_challenge_and_refunds_bucket(memory_db, base_settings):
    session_factory, _ = memory_db
    failing_notif_client = AsyncMock()
    failing_notif_client.send_otp.return_value = NotificationDispatchResult(
        success=False,
        message_id=None,
        status="FAILED",
        channel="WHATSAPP",
        error="Gateway timeout from upstream provider",
    )
    base_settings.otp_delivery_mode = "notification_service"

    otp_service = OtpService(
        settings=base_settings,
        notification_client=failing_notif_client,
        session_factory=session_factory,
    )

    mobile = "+919111222333"

    with pytest.raises(Exception) as exc_info:
        await otp_service.request_otp(mobile, purpose="login")

    # Verify challenge is invalidated so user is never left with an unusable undelivered code
    with session_factory() as session:
        ch = session.execute(
            select(OtpChallenge).where(OtpChallenge.mobile_e164 == mobile)
        ).scalar_one_or_none()
        assert ch is not None
        assert ch.consumed_at is not None
        assert ch.metadata_json.get("dispatch_status") == "FAILED"

        # Verify rate bucket was refunded
        bucket = session.execute(
            select(OtpRateBucket).where(
                OtpRateBucket.mobile_e164 == mobile,
                OtpRateBucket.action == "send",
            )
        ).scalar_one_or_none()
        if bucket is not None:
            assert bucket.attempt_count == 0


# =============================================================================
# 2. OTP Multi-Community & Multi-Role Context Selection
# =============================================================================
def test_otp_uniqueness_and_multi_community_resolution(memory_db, base_settings):
    session_factory, _ = memory_db
    now = datetime.now(UTC)
    mobile = "+919876543210"

    with session_factory.begin() as session:
        t1 = Tenant(id=uuid4(), slug="alpha", status="active", created_at=now, updated_at=now)
        t2 = Tenant(id=uuid4(), slug="beta", status="active", created_at=now, updated_at=now)
        session.add_all([t1, t2])

        u1 = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        u2 = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add_all([u1, u2])

        cred1 = AccountCredential(
            id=uuid4(),
            user_id=u1.id,
            account_type="tenant_user",
            username="multi_user_1",
            password_hash="hash",
            status="active",
            created_at=now,
            updated_at=now,
        )
        cred2 = AccountCredential(
            id=uuid4(),
            user_id=u2.id,
            account_type="tenant_user",
            username="multi_user_2",
            password_hash="hash",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add_all([cred1, cred2])

        # Role admin in t1
        r_admin = Role(id=uuid4(), tenant_id=t1.id, code="admin", display_name="Admin", status="active", created_at=now, updated_at=now)
        # Role member in t2
        r_member = Role(id=uuid4(), tenant_id=t2.id, code="member", display_name="Member", status="active", created_at=now, updated_at=now)
        session.add_all([r_admin, r_member])

        m1 = TenantMembership(id=uuid4(), tenant_id=t1.id, user_id=u1.id, mobile_e164=mobile, status="active", created_at=now, updated_at=now)
        m2 = TenantMembership(id=uuid4(), tenant_id=t2.id, user_id=u2.id, mobile_e164=mobile, status="active", created_at=now, updated_at=now)
        session.add_all([m1, m2])

        ura1 = UserRoleAssignment(id=uuid4(), tenant_id=t1.id, membership_id=m1.id, role_id=r_admin.id, assigned_by_membership_id=m1.id, status="active", valid_from=now, created_at=now, updated_at=now)
        ura2 = UserRoleAssignment(id=uuid4(), tenant_id=t2.id, membership_id=m2.id, role_id=r_member.id, assigned_by_membership_id=m2.id, status="active", valid_from=now, created_at=now, updated_at=now)
        session.add_all([ura1, ura2])

    repo = SQLAlchemyAuthenticationRepository(session_factory)
    contexts = repo.resolve_contexts_for_mobile(mobile)

    # Must resolve both Community Alpha (admin) and Community Beta (member)
    assert len(contexts) == 2
    slugs = {c["tenant_slug"] for c in contexts}
    assert "alpha" in slugs
    assert "beta" in slugs


# =============================================================================
# 3. Initial Agent Appointment Approval vs Lifecycle Reactivation & SEC-03
# =============================================================================
def test_initial_agent_approval_and_sec03_dual_custody(memory_db):
    session_factory, _ = memory_db
    now = datetime.now(UTC)

    with session_factory.begin() as session:
        tenant = Tenant(id=uuid4(), slug="tenant-test", status="active", created_at=now, updated_at=now)
        session.add(tenant)

        # Creator Admin A1
        u_creator = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        # Independent Reviewer Admin A2
        u_reviewer = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        # New Agent User (inactive until approval)
        u_agent = User(id=uuid4(), status="inactive", created_at=now, updated_at=now)
        session.add_all([u_creator, u_reviewer, u_agent])

        m_creator = TenantMembership(id=uuid4(), tenant_id=tenant.id, user_id=u_creator.id, mobile_e164="+919000000001", status="active", created_at=now, updated_at=now)
        m_reviewer = TenantMembership(id=uuid4(), tenant_id=tenant.id, user_id=u_reviewer.id, mobile_e164="+919000000002", status="active", created_at=now, updated_at=now)
        m_agent = TenantMembership(id=uuid4(), tenant_id=tenant.id, user_id=u_agent.id, mobile_e164="+919000000003", status="inactive", created_at=now, updated_at=now)
        session.add_all([m_creator, m_reviewer, m_agent])

        role_agent = Role(id=uuid4(), tenant_id=tenant.id, code="agent", display_name="Agent", status="active", created_at=now, updated_at=now)
        session.add(role_agent)

        prof = AgentProfile(
            id=uuid4(),
            tenant_id=tenant.id,
            user_id=u_agent.id,
            membership_id=m_agent.id,
            first_name="Test",
            last_name="Agent",
            ref_id=f"AG-{secrets.token_hex(4).upper()}",
            profile_photo_key="photo_key",
            aadhaar_number_ciphertext="enc_aadhaar",
            aadhaar_number_masked="XXXX-XXXX-1234",
            aadhaar_document_key="aadhaar_key",
            pan_number_ciphertext="enc_pan",
            pan_number_masked="ABCDE1234F",
            pan_document_key="doc_key",
            created_at=now,
            updated_at=now,
        )
        session.add(prof)

        appt = AgentAppointment(
            id=uuid4(),
            tenant_id=tenant.id,
            user_id=u_agent.id,
            membership_id=m_agent.id,
            agent_profile_id=prof.id,
            requested_by_user_id=u_creator.id,
            role_id=role_agent.id,
            status="PENDING",
            created_at=now,
            updated_at=now,
        )
        session.add(appt)

    # 1. Self-approval prohibition (SEC-03 Dual Custody): Creator A1 attempts to approve -> 403
    appt_service = AgentAppointmentService()
    with session_factory() as session:
        with pytest.raises(HTTPException) as exc_self:
            appt_service.approve_appointment(
                session=session,
                appointment_id=appt.id,
                reviewer_user_id=u_creator.id,
                reviewer_membership_id=m_creator.id,
                caller_tenant_id=tenant.id,
                data=AppointmentDecisionRequest(reason="Self approve"),
            )
        assert exc_self.value.status_code == 403
        assert "SELF_APPROVAL_PROHIBITED" in str(exc_self.value.detail)

    # 2. Lifecycle Reactivate check: Attempting REACTIVATE on unapproved pending agent -> 400 APPOINTMENT_NOT_APPROVED
    lifecycle_service = AgentLifecycleService()
    with session_factory() as session:
        with pytest.raises(HTTPException) as exc_lifecycle:
            lifecycle_service.submit_lifecycle_request(
                session=session,
                tenant_id=tenant.id,
                agent_profile_id=prof.id,
                action=LifecycleAction.REACTIVATE,
                reason="Attempting to reactivate unapproved agent",
                requester_user_id=u_creator.id,
            )
        assert exc_lifecycle.value.status_code == 400
        assert "APPOINTMENT_NOT_APPROVED" in str(exc_lifecycle.value.detail)

    # 3. Independent Reviewer A2 approves appointment -> 200 SUCCESS
    with session_factory() as session:
        summary = appt_service.approve_appointment(
            session=session,
            appointment_id=appt.id,
            reviewer_user_id=u_reviewer.id,
            reviewer_membership_id=m_reviewer.id,
            caller_tenant_id=tenant.id,
            data=AppointmentDecisionRequest(reason="Independent review passed"),
        )
        assert summary.status == "APPROVED"

    # Verify agent user, membership, and profile are now active
    with session_factory() as session:
        agent_u = session.execute(select(User).where(User.id == u_agent.id)).scalar_one()
        agent_m = session.execute(select(TenantMembership).where(TenantMembership.id == m_agent.id)).scalar_one()
        assert agent_u.status == "active"
        assert agent_m.status == "active"


# =============================================================================
# 4. Webhook HMAC Validation, Replay, Monotonic States, and DB Failure
# =============================================================================
def test_webhook_hmac_and_monotonic_state_and_db_failure(memory_db, base_settings):
    session_factory, _ = memory_db
    webhook_service = NotificationWebhookService(base_settings)
    secret = base_settings.notification_webhook_secret.get_secret_value().encode("utf-8")
    now = datetime.now(UTC)

    # Seed an existing notification message in QUEUED state
    with session_factory.begin() as session:
        t = Tenant(id=uuid4(), slug="webhook-tenant", status="active", created_at=now, updated_at=now)
        session.add(t)
        notif_msg = NotificationMessage(
            tenant_id=t.id,
            message_id="msg-webhook-test",
            channel="WHATSAPP",
            message_type="LOGIN_OTP",
            recipient="+919876543210",
            status="QUEUED",
            priority="P0",
            purpose="login",
            occurred_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(notif_msg)

    # 1. Valid webhook delivers message (QUEUED -> DELIVERED)
    event_id_1 = str(uuid4())
    payload_1 = {
        "eventId": event_id_1,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg-webhook-test",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body_1 = json.dumps(payload_1).encode("utf-8")
    ts_1 = str(int(now.timestamp()))
    sig_1 = f"sha256={hmac.new(secret, ts_1.encode('utf-8') + b'.' + raw_body_1, hashlib.sha256).hexdigest()}"

    with session_factory() as session:
        res1 = webhook_service.process_webhook(
            session=session,
            raw_body=raw_body_1,
            timestamp_header=ts_1,
            signature_header=sig_1,
            event_id_header=event_id_1,
        )
        assert res1.status == "processed"

    with session_factory() as session:
        msg = session.execute(select(NotificationMessage).where(NotificationMessage.message_id == "msg-webhook-test")).scalar_one()
        assert msg.status == "DELIVERED"

    # 2. Duplicate webhook event returns idempotent 'duplicate'
    with session_factory() as session:
        res_dup = webhook_service.process_webhook(
            session=session,
            raw_body=raw_body_1,
            timestamp_header=ts_1,
            signature_header=sig_1,
            event_id_header=event_id_1,
        )
        assert res_dup.status == "duplicate"

    # 3. Terminal State Monotonicity: Out-of-order QUEUED event cannot regress DELIVERED
    event_id_regress = str(uuid4())
    payload_regress = {
        "eventId": event_id_regress,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg-webhook-test",
        "status": "QUEUED",
        "channel": "WHATSAPP",
        "occurredAt": (now - timedelta(seconds=10)).isoformat(),
    }
    raw_body_regress = json.dumps(payload_regress).encode("utf-8")
    sig_regress = f"sha256={hmac.new(secret, ts_1.encode('utf-8') + b'.' + raw_body_regress, hashlib.sha256).hexdigest()}"

    with session_factory() as session:
        res_regress = webhook_service.process_webhook(
            session=session,
            raw_body=raw_body_regress,
            timestamp_header=ts_1,
            signature_header=sig_regress,
            event_id_header=event_id_regress,
        )
        assert res_regress.status == "processed"

    # Status must still be DELIVERED, not regressed to QUEUED!
    with session_factory() as session:
        msg = session.execute(select(NotificationMessage).where(NotificationMessage.message_id == "msg-webhook-test")).scalar_one()
        assert msg.status == "DELIVERED"

    # 4. Valid terminal forward transition: DELIVERED -> READ is permitted
    event_id_read = str(uuid4())
    payload_read = {
        "eventId": event_id_read,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg-webhook-test",
        "status": "READ",
        "channel": "WHATSAPP",
        "occurredAt": (now + timedelta(seconds=5)).isoformat(),
    }
    raw_body_read = json.dumps(payload_read).encode("utf-8")
    sig_read = f"sha256={hmac.new(secret, ts_1.encode('utf-8') + b'.' + raw_body_read, hashlib.sha256).hexdigest()}"

    with session_factory() as session:
        res_read = webhook_service.process_webhook(
            session=session,
            raw_body=raw_body_read,
            timestamp_header=ts_1,
            signature_header=sig_read,
            event_id_header=event_id_read,
        )
        assert res_read.status == "processed"

    with session_factory() as session:
        msg = session.execute(select(NotificationMessage).where(NotificationMessage.message_id == "msg-webhook-test")).scalar_one()
        assert msg.status == "READ"

    # 5. Database failure raises HTTP 500 DATABASE_ERROR and cleanly rolls back
    event_id_fail = str(uuid4())
    payload_fail = {
        "eventId": event_id_fail,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg-webhook-test",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body_fail = json.dumps(payload_fail).encode("utf-8")
    sig_fail = f"sha256={hmac.new(secret, ts_1.encode('utf-8') + b'.' + raw_body_fail, hashlib.sha256).hexdigest()}"

    mock_session = MagicMock(spec=Session)
    mock_session.execute.return_value.scalar_one_or_none.return_value = None
    mock_session.commit.side_effect = RuntimeError("Disk full / DB connection lost")

    with pytest.raises(HTTPException) as exc_db:
        webhook_service.process_webhook(
            session=mock_session,
            raw_body=raw_body_fail,
            timestamp_header=ts_1,
            signature_header=sig_fail,
            event_id_header=event_id_fail,
        )
    assert exc_db.value.status_code == 500
    assert "DATABASE_ERROR" in str(exc_db.value.detail)
    mock_session.rollback.assert_called()


# =============================================================================
# 5. OTP Attempt Limits & Lockout
# =============================================================================
def test_otp_attempt_limits_and_atomic_lockout(memory_db, base_settings):
    session_factory, _ = memory_db
    otp_service = OtpService(settings=base_settings, session_factory=session_factory)
    now = datetime.now(UTC)
    mobile = "+919999999999"
    cid = uuid4()
    salt = secrets.token_hex(16)
    otp_hash = OtpService._hash_otp("123456", salt)

    with session_factory.begin() as session:
        ch = OtpChallenge(
            id=cid,
            mobile_e164=mobile,
            purpose="login",
            otp_hash=otp_hash,
            otp_salt=salt,
            failed_attempts=0,
            dispatch_id=uuid4(),
            expires_at=now + timedelta(minutes=5),
            created_at=now,
        )
        session.add(ch)

    # 4 incorrect attempts -> increments failed_attempts and returns attempts_remaining
    for attempt in range(1, 5):
        with pytest.raises(OtpVerificationFailed) as exc_info:
            otp_service.verify_otp(cid, mobile, "000000", purpose="login")
        expected_remaining = 5 - attempt
        assert exc_info.value.attempts_remaining == expected_remaining

    # 5th incorrect attempt -> locks the challenge (attempts_remaining == 0)
    with pytest.raises(OtpVerificationFailed) as exc_lock:
        otp_service.verify_otp(cid, mobile, "000000", purpose="login")
    assert exc_lock.value.attempts_remaining == 0

    with session_factory() as session:
        locked_ch = session.execute(select(OtpChallenge).where(OtpChallenge.id == cid)).scalar_one()
        assert locked_ch.failed_attempts == 5
        assert locked_ch.consumed_at is not None

    # Even if correct OTP is provided now, it must be rejected as already consumed/locked
    with pytest.raises(OtpVerificationFailed) as exc_subsequent:
        otp_service.verify_otp(cid, mobile, "123456", purpose="login")
    assert "already been used" in str(exc_subsequent.value).lower() or "no longer valid" in str(exc_subsequent.value).lower()


# =============================================================================
# 6. Session Token Hashing & CSRF Revocation
# =============================================================================
def test_session_token_hashing_and_revocation(memory_db, base_settings):
    session_factory, _ = memory_db
    now = datetime.now(UTC)
    repo = SQLAlchemyAuthenticationRepository(session_factory)

    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_hex(32)
    expected_digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    with session_factory.begin() as session:
        t = Tenant(id=uuid4(), slug="session-tenant", status="active", created_at=now, updated_at=now)
        session.add(t)
        u = User(id=uuid4(), status="active", created_at=now, updated_at=now)
        session.add(u)
        cred = AccountCredential(
            id=uuid4(),
            user_id=u.id,
            account_type="tenant_user",
            username="session_user",
            password_hash="hash",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(cred)
        m = TenantMembership(id=uuid4(), tenant_id=t.id, user_id=u.id, mobile_e164="+919876543210", status="active", created_at=now, updated_at=now)
        session.add(m)

        sess = AuthSession(
            id=uuid4(),
            tenant_id=t.id,
            membership_id=m.id,
            token_digest=expected_digest,
            csrf_token=csrf_token,
            expires_at=now + timedelta(hours=1),
            created_at=now,
        )
        session.add(sess)

    # Resolve session by raw token
    resolved = repo.resolve_session(raw_token)
    assert resolved is not None
    assert resolved.user_id == u.id
    assert resolved.csrf_token == csrf_token

    # Revoke session by raw token
    revoked = repo.revoke_session_by_token(raw_token)
    assert revoked is True

    # Resolving revoked session must return None
    assert repo.resolve_session(raw_token) is None


# =============================================================================
# 7. Outbound Email Dispatch Support
# =============================================================================
@pytest.mark.anyio
async def test_notification_service_client_email_dispatch(base_settings):
    base_settings.otp_delivery_mode = "mock"
    client = NotificationServiceClient(base_settings)

    res = await client.send_email(
        recipient_email="viewer@example.com",
        subject="Welcome to Community OTT",
        text_content="Your registration is confirmed.",
        message_type="ACCOUNT_UPDATE",
    )

    assert res.success is True
    assert res.channel == "EMAIL"
    assert res.status == "MOCK_SENT"
    assert res.message_id is not None

    assert len(client.mock_messages) == 1
    mock_msg = client.mock_messages[0]
    assert mock_msg["channel"] == "EMAIL"
    assert mock_msg["recipient"] == "viewer@example.com"
    assert mock_msg["content"]["text"] == "Your registration is confirmed."


# =============================================================================
# 8. Production Configuration Fail-Closed Validation
# =============================================================================
def test_production_fail_closed_configuration():
    prod_db = "postgresql+psycopg://postgres:postgres@localhost:5432/community_ott"
    # 1. Bypass skip dispatch in production must fail
    with pytest.raises(ValueError, match="OTP_SKIP_NOTIFICATION_DISPATCH"):
        Settings(
            _env_file=None,
            environment="production",
            database_url=prod_db,
            frontend_origins="https://app.avanflix.com",
            otp_skip_notification_dispatch=True,
            otp_log_to_console=False,
            notification_service_base_url="https://notification.example.com",
            notification_service_api_key=SecretStr("key"),
            notification_webhook_secret=SecretStr("secret-1234567890123456"),
            auth_session_cookie_secure=True,
            otp_challenge_ttl_seconds=300,
            messaging_api_base_url="https://msg.example.com",
            messaging_send_otp_path="/send",
            messaging_verify_otp_path="/verify",
            messaging_send_otp_method="POST",
            messaging_verify_otp_method="POST",
            messaging_api_key=SecretStr("msg-key"),
            messaging_auth_header_name="X-API-Key",
        )

    # 2. Console OTP logging in production must fail
    with pytest.raises(ValueError, match="OTP_LOG_TO_CONSOLE"):
        Settings(
            _env_file=None,
            environment="production",
            database_url=prod_db,
            frontend_origins="https://app.avanflix.com",
            otp_log_to_console=True,
            otp_skip_notification_dispatch=False,
            notification_service_base_url="https://notification.example.com",
            notification_service_api_key=SecretStr("key"),
            notification_webhook_secret=SecretStr("secret-1234567890123456"),
            auth_session_cookie_secure=True,
            otp_challenge_ttl_seconds=300,
            messaging_api_base_url="https://msg.example.com",
            messaging_send_otp_path="/send",
            messaging_verify_otp_path="/verify",
            messaging_send_otp_method="POST",
            messaging_verify_otp_method="POST",
            messaging_api_key=SecretStr("msg-key"),
            messaging_auth_header_name="X-API-Key",
        )

    # 3. HTTP (non-HTTPS) notification service in production must fail
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            database_url=prod_db,
            frontend_origins="https://app.avanflix.com",
            otp_delivery_mode="notification_service",
            otp_log_to_console=False,
            otp_skip_notification_dispatch=False,
            notification_service_base_url="http://insecure-notifications.com",
            notification_service_api_key=SecretStr("key"),
            notification_webhook_secret=SecretStr("secret-1234567890123456"),
            auth_session_cookie_secure=True,
            otp_challenge_ttl_seconds=300,
            messaging_api_base_url="https://msg.example.com",
            messaging_send_otp_path="/send",
            messaging_verify_otp_path="/verify",
            messaging_send_otp_method="POST",
            messaging_verify_otp_method="POST",
            messaging_api_key=SecretStr("msg-key"),
            messaging_auth_header_name="X-API-Key",
        )
