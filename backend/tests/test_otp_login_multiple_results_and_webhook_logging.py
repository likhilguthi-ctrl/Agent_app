"""Tests for OTP Login MultipleResultsFound resolution, Outbound Notification Logging,
and Webhook Return Status Logging.

Covers the 12 required scenarios:
1. No existing OTP challenge -> creates challenge cleanly.
2. Exactly one existing active challenge -> updates/resends challenge cleanly.
3. Multiple matching historical records -> deduplicates without raising MultipleResultsFound.
4. Expired challenge and cooldown handling.
5. Concurrent OTP requests -> savepoint handles IntegrityError without returning 500.
6. Duplicate records do not cause unhandled 500 in login endpoint.
7. Successful notification dispatch including HTTP 202 (structured log emission).
8. External notification 4xx, 5xx, timeout, and connection failure logging & error codes.
9. Valid webhook response status and structured log emission.
10. Duplicate webhook event and invalid HMAC rejection logging.
11. Webhook processing/database failure logging.
12. OTP verification, expiry, attempt limits, and replay protection remain intact.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.cli.bootstrap_accounts import run_bootstrap
from app.core.config import Settings
from app.core.tenant_context import TenantContext
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.authentication.abuse_control import SQLAlchemyOtpAbuseGuard
from app.modules.authentication.api import (
    get_authentication_repository,
    get_otp_abuse_guard,
    get_otp_service,
)
from app.modules.authentication.models import OtpChallenge, OtpRateBucket
from app.modules.authentication.otp_service import (
    OtpRateLimitExceeded,
    OtpService,
    OtpVerificationFailed,
)
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.api import get_authorization_repository
from app.modules.authorization.repository import SQLAlchemyAuthorizationRepository
from app.modules.identity.repository import (
    SQLAlchemyTenantMembershipRepository,
    get_tenant_membership_lookup,
)
from app.modules.messaging.api import notification_webhooks_router
from app.modules.messaging.errors import (
    MessagingError,
    MessagingProviderRejected,
    MessagingProviderUnavailable,
)
from app.modules.messaging.models import NotificationMessage, NotificationWebhookEvent
from app.modules.messaging.notification_service import (
    NotificationDispatchResult,
    NotificationServiceClient,
)
from app.modules.messaging.webhook_service import (
    NotificationWebhookResponse,
    NotificationWebhookService,
)
from app.modules.tenants.dependencies import get_tenant_context
from app.modules.tenants.models import Tenant

TEST_WEBHOOK_SECRET = "test_webhook_secret_key_32bytes_min!"


def compute_signature(timestamp: str, raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    msg = timestamp.encode("utf-8") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def create_legacy_duplicate_schema(engine):
    """Recreate otp_challenges table without the UNIQUE constraint to simulate pre-migration duplicate records."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS otp_challenges"))
        conn.execute(text("""
            CREATE TABLE otp_challenges (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(36),
                mobile_e164 VARCHAR(16) NOT NULL,
                purpose VARCHAR(32) NOT NULL DEFAULT 'login',
                otp_hash VARCHAR(128) NOT NULL DEFAULT '',
                otp_salt VARCHAR(64) NOT NULL DEFAULT '',
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                resend_count INTEGER NOT NULL DEFAULT 0,
                dispatch_id VARCHAR(36),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                consumed_at TIMESTAMP,
                verified_at TIMESTAMP,
                verification_token VARCHAR(64),
                metadata_json JSON
            )
        """))


@pytest.fixture
def sqlite_factory(tmp_path):
    db_file = tmp_path / "test_otp_fix.sqlite"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_fk(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def test_settings():
    return Settings(
        _env_file=None,
        environment="development",
        otp_challenge_ttl_seconds=300,
        otp_send_cooldown_seconds=60,
        otp_skip_notification_dispatch=False,
        otp_log_to_console=True,
        otp_delivery_mode="notification_service",
        notification_service_base_url="https://notifications.internal",
        notification_service_api_key=SecretStr("notif-test-key"),
        notification_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
        frontend_origins="http://localhost:3000",
        auth_session_cookie_secure=False,
    )


# ===========================================================================
# Scenario 1: No existing OTP challenge -> creates challenge cleanly
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_1_no_existing_challenge_creates_cleanly(sqlite_factory, test_settings, caplog, capsys):
    mock_client = MagicMock(spec=NotificationServiceClient)
    mock_client.send_otp = AsyncMock(return_value=NotificationDispatchResult(
        success=True, message_id="msg_scen_1", status="QUEUED", channel="WHATSAPP"
    ))

    otp_svc = OtpService(
        settings=test_settings,
        notification_client=mock_client,
        session_factory=sqlite_factory,
    )

    mobile = "+919100209205"
    with caplog.at_level(logging.INFO):
        challenge_id, cooldown = await otp_svc.request_otp(mobile, purpose="login")

    assert challenge_id is not None
    assert cooldown == 60

    # Verify diagnostic log
    captured = capsys.readouterr()
    all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
    assert "otp_challenge_lookup" in all_output
    assert "matching_row_count=0" in all_output
    assert "purpose=login" in all_output

    # Verify database state
    with sqlite_factory() as session:
        challenges = session.execute(
            select(OtpChallenge).where(OtpChallenge.mobile_e164 == mobile, OtpChallenge.purpose == "login")
        ).scalars().all()
        assert len(challenges) == 1
        assert challenges[0].id == challenge_id
        assert challenges[0].resend_count == 0


# ===========================================================================
# Scenario 2: Exactly one existing active challenge -> updates/resends cleanly
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_2_single_existing_challenge_updates_cleanly(sqlite_factory, test_settings, caplog, capsys):
    mock_client = MagicMock(spec=NotificationServiceClient)
    mock_client.send_otp = AsyncMock(return_value=NotificationDispatchResult(
        success=True, message_id="msg_scen_2", status="QUEUED", channel="WHATSAPP"
    ))

    otp_svc = OtpService(
        settings=test_settings,
        notification_client=mock_client,
        session_factory=sqlite_factory,
    )

    mobile = "+919100209205"
    # First request
    cid1, _ = await otp_svc.request_otp(mobile, purpose="login")

    # Clear rate bucket to permit immediate resend in test
    with sqlite_factory.begin() as session:
        session.query(OtpRateBucket).delete()

    # Second request (update existing)
    with caplog.at_level(logging.INFO):
        cid2, _ = await otp_svc.request_otp(mobile, purpose="login")

    # Diagnostic log should report matching_row_count=1
    captured = capsys.readouterr()
    all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
    assert "otp_challenge_lookup" in all_output
    assert "matching_row_count=1" in all_output

    # Database must still have exactly 1 record, with updated resend_count
    with sqlite_factory() as session:
        challenges = session.execute(
            select(OtpChallenge).where(OtpChallenge.mobile_e164 == mobile, OtpChallenge.purpose == "login")
        ).scalars().all()
        assert len(challenges) == 1
        assert challenges[0].id == cid2
        assert challenges[0].resend_count == 1


# ===========================================================================
# Scenario 3: Multiple matching historical records -> deduplicates safely
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_3_multiple_records_deduplicated_without_multipleresultsfound(sqlite_factory, test_settings, caplog, capsys):
    mobile = "+919100209205"
    now = datetime.now(UTC)

    # Recreate table without unique constraint to simulate legacy duplicate rows in DB
    create_legacy_duplicate_schema(sqlite_factory.kw["bind"])

    # Pre-seed 3 duplicate rows for the same mobile and purpose
    with sqlite_factory.begin() as session:
        for i in range(3):
            session.add(OtpChallenge(
                id=uuid4(),
                tenant_id=None,
                mobile_e164=mobile,
                purpose="login",
                otp_hash=f"hash_{i}",
                otp_salt=f"salt_{i}",
                failed_attempts=0,
                resend_count=0,
                created_at=now - timedelta(minutes=10 - i),
                expires_at=now + timedelta(minutes=5),
            ))

    # Verify 3 rows exist before call
    with sqlite_factory() as session:
        count = len(session.execute(
            select(OtpChallenge).where(OtpChallenge.mobile_e164 == mobile, OtpChallenge.purpose == "login")
        ).scalars().all())
        assert count == 3

    mock_client = MagicMock(spec=NotificationServiceClient)
    mock_client.send_otp = AsyncMock(return_value=NotificationDispatchResult(
        success=True, message_id="msg_scen_3", status="QUEUED", channel="WHATSAPP"
    ))

    otp_svc = OtpService(
        settings=test_settings,
        notification_client=mock_client,
        session_factory=sqlite_factory,
    )

    # Calling request_otp must NOT raise MultipleResultsFound
    with caplog.at_level(logging.INFO):
        challenge_id, _ = await otp_svc.request_otp(mobile, purpose="login")

    assert challenge_id is not None

    # Verify diagnostic log captured matching_row_count=3 and warning emitted
    captured = capsys.readouterr()
    all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
    assert "otp_challenge_lookup" in all_output
    assert "matching_row_count=3" in all_output
    assert "otp_challenge_duplicate_records_detected" in all_output

    # Verify obsolete duplicates were pruned: exactly 1 row remains
    with sqlite_factory() as session:
        challenges = session.execute(
            select(OtpChallenge).where(OtpChallenge.mobile_e164 == mobile, OtpChallenge.purpose == "login")
        ).scalars().all()
        assert len(challenges) == 1
        assert challenges[0].id == challenge_id


# ===========================================================================
# Scenario 4: Expired challenge and cooldown handling
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_4_expired_challenge_and_cooldown_handling(sqlite_factory, test_settings):
    mobile = "+919100209205"
    now = datetime.now(UTC)
    tenant_id = uuid4()

    # Commit Tenant first so FK constraint is satisfied
    with sqlite_factory.begin() as session:
        session.add(Tenant(
            id=tenant_id,
            slug="tenant-scen-4",
            status="active",
            created_at=now,
            updated_at=now,
        ))

    # Then commit the expired challenge
    with sqlite_factory.begin() as session:
        session.add(OtpChallenge(
            id=uuid4(),
            tenant_id=tenant_id,
            mobile_e164=mobile,
            purpose="login",
            otp_hash="old_hash",
            otp_salt="old_salt",
            failed_attempts=0,
            resend_count=0,
            created_at=now - timedelta(minutes=10),
            expires_at=now - timedelta(minutes=5),
        ))

    mock_client = MagicMock(spec=NotificationServiceClient)
    mock_client.send_otp = AsyncMock(return_value=NotificationDispatchResult(
        success=True, message_id="msg_scen_4", status="QUEUED", channel="WHATSAPP"
    ))

    otp_svc = OtpService(
        settings=test_settings,
        notification_client=mock_client,
        session_factory=sqlite_factory,
    )

    # Requesting OTP on expired challenge succeeds and refreshes it
    cid, cooldown = await otp_svc.request_otp(mobile, purpose="login", tenant_id=tenant_id)
    assert cid is not None
    assert cooldown == 60

    # Requesting OTP immediately again (within 60s cooldown) raises OtpRateLimitExceeded
    with pytest.raises(OtpRateLimitExceeded) as exc_info:
        await otp_svc.request_otp(mobile, purpose="login", tenant_id=tenant_id)
    assert exc_info.value.retry_after_seconds > 0


# ===========================================================================
# Scenario 5: Concurrent OTP requests -> savepoint handles IntegrityError
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_5_concurrent_requests_integrity_error_handled(sqlite_factory, test_settings):
    mobile = "+919100209205"
    now = datetime.now(UTC)

    # Pre-seed existing challenge row (simulating another concurrent transaction inserting first)
    with sqlite_factory.begin() as session:
        session.add(OtpChallenge(
            id=uuid4(),
            tenant_id=None,
            mobile_e164=mobile,
            purpose="login",
            otp_hash="winner_hash",
            otp_salt="winner_salt",
            failed_attempts=0,
            resend_count=0,
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        ))

    mock_client = MagicMock(spec=NotificationServiceClient)
    mock_client.send_otp = AsyncMock(return_value=NotificationDispatchResult(
        success=True, message_id="msg_scen_5", status="QUEUED", channel="WHATSAPP"
    ))

    otp_svc = OtpService(
        settings=test_settings,
        notification_client=mock_client,
        session_factory=sqlite_factory,
    )

    # Mock the initial lookup query to return empty list (simulating race condition:
    # it thought no record existed, but when it attempts insert, IntegrityError occurs)
    original_execute = Session.execute
    lookup_calls = 0

    def mock_execute(session_self, statement, *args, **kwargs):
        nonlocal lookup_calls
        if "FROM otp_challenges" in str(statement) and "ORDER BY" in str(statement) and lookup_calls == 0:
            lookup_calls += 1
            mock_res = MagicMock()
            mock_res.scalars.return_value.all.return_value = []
            return mock_res
        return original_execute(session_self, statement, *args, **kwargs)

    with patch.object(Session, "execute", mock_execute):
        cid, cooldown = await otp_svc.request_otp(mobile, purpose="login")

    assert cid is not None
    # Verify exactly 1 row exists and it was updated cleanly by the IntegrityError handler
    with sqlite_factory() as session:
        challenges = session.execute(
            select(OtpChallenge).where(OtpChallenge.mobile_e164 == mobile, OtpChallenge.purpose == "login")
        ).scalars().all()
        assert len(challenges) == 1
        assert challenges[0].id == cid


# ===========================================================================
# Scenario 6: Duplicate records do not cause unhandled 500 in login endpoint
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_6_duplicate_records_do_not_cause_unhandled_500_api(sqlite_factory, test_settings):
    tenant_id = uuid4()
    with sqlite_factory.begin() as session:
        t = Tenant(
            id=tenant_id,
            slug="demo-tenant-dup",
            status="active",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(t)

    # Bootstrap Super Admin user
    run_bootstrap(
        role="SUPER_ADMIN",
        username="super_admin_dup_test",
        password="SuperAdmin@123",
        mobile_number="+919100209205",
        must_change_password=False,
        factory=sqlite_factory,
    )

    # Recreate table without unique constraint to simulate legacy duplicate rows in DB
    create_legacy_duplicate_schema(sqlite_factory.kw["bind"])

    now = datetime.now(UTC)
    with sqlite_factory.begin() as session:
        session.add(OtpChallenge(
            id=uuid4(),
            tenant_id=None,
            mobile_e164="+919100209205",
            purpose="login",
            otp_hash="dup_hash_1",
            otp_salt="dup_salt_1",
            failed_attempts=0,
            resend_count=0,
            created_at=now - timedelta(minutes=10),
            expires_at=now + timedelta(minutes=5),
        ))
        session.add(OtpChallenge(
            id=uuid4(),
            tenant_id=None,
            mobile_e164="+919100209205",
            purpose="login",
            otp_hash="dup_hash_2",
            otp_salt="dup_salt_2",
            failed_attempts=0,
            resend_count=0,
            created_at=now - timedelta(minutes=5),
            expires_at=now + timedelta(minutes=5),
        ))

    app = create_app(test_settings)
    app.state.test_factory = sqlite_factory

    mock_client = MagicMock(spec=NotificationServiceClient)
    mock_client.send_otp = AsyncMock(return_value=NotificationDispatchResult(
        success=True, message_id="msg_scen_6", status="QUEUED", channel="WHATSAPP"
    ))
    app.state.notification_client = mock_client

    tenant_ctx = TenantContext(tenant_id, "demo-tenant-dup", "demo.test", uuid4())
    app.state.test_tenant = tenant_ctx

    auth_repo = SQLAlchemyAuthenticationRepository(sqlite_factory)
    authz_repo = SQLAlchemyAuthorizationRepository(sqlite_factory)
    tenant_lookup = SQLAlchemyTenantMembershipRepository(sqlite_factory)
    abuse_guard = SQLAlchemyOtpAbuseGuard(test_settings, sqlite_factory)

    app.dependency_overrides[get_tenant_context] = lambda: app.state.test_tenant
    app.dependency_overrides[get_authentication_repository] = lambda: auth_repo
    app.dependency_overrides[get_authorization_repository] = lambda: authz_repo
    app.dependency_overrides[get_tenant_membership_lookup] = lambda: tenant_lookup
    app.dependency_overrides[get_otp_abuse_guard] = lambda: abuse_guard

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        # Step 1: Submit credentials to trigger 2FA OTP
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "username": "super_admin_dup_test",
                "password": "SuperAdmin@123",
                "two_factor": True,
            },
        )
        # CRITICAL ASSERTION: Must return 200 (OTP required), NOT 500 INTERNAL_ERROR!
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "otp_required"
        assert "challenge_id" in data
        assert data["masked_mobile"] == "+91 91002 ****05"

    # Verify duplicate records were resolved in DB: exactly 1 remains
    with sqlite_factory() as session:
        challenges = session.execute(
            select(OtpChallenge).where(OtpChallenge.mobile_e164 == "+919100209205", OtpChallenge.purpose == "login")
        ).scalars().all()
        assert len(challenges) == 1


# ===========================================================================
# Scenario 7: Outbound notification dispatch logging (202 and 200)
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_7_outbound_notification_dispatch_logging_202_and_200(test_settings, caplog, capsys):
    # Test HTTP 202
    def mock_handler_202(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"messageId": "msg_202_test", "status": "QUEUED"})

    transport_202 = httpx.MockTransport(mock_handler_202)
    async with httpx.AsyncClient(transport=transport_202) as http_c:
        client = NotificationServiceClient(test_settings, client=http_c)
        with caplog.at_level(logging.INFO):
            res202 = await client.send_otp("+919100209205", "123456", purpose="login", challenge_id="cid-202")

    assert res202.success is True
    assert res202.status == "QUEUED"
    assert res202.message_id == "msg_202_test"
    captured = capsys.readouterr()
    all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
    assert "notification_dispatch_result" in all_output
    assert "http_status=202" in all_output
    assert "outcome=ACCEPTED" in all_output
    assert "msg_202_test" in all_output

    caplog.clear()

    # Test HTTP 200
    def mock_handler_200(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messageId": "msg_200_test", "status": "SENT"})

    transport_200 = httpx.MockTransport(mock_handler_200)
    async with httpx.AsyncClient(transport=transport_200) as http_c:
        client = NotificationServiceClient(test_settings, client=http_c)
        with caplog.at_level(logging.INFO):
            res200 = await client.send_otp("+919100209205", "654321", purpose="login", challenge_id="cid-200")

    assert res200.success is True
    assert res200.status == "SENT"
    captured = capsys.readouterr()
    all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
    assert "notification_dispatch_result" in all_output
    assert "http_status=200" in all_output
    assert "outcome=ACCEPTED" in all_output
    assert "msg_200_test" in all_output


# ===========================================================================
# Scenario 8: Outbound notification 4xx, 5xx, timeout logging & error codes
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_8_outbound_notification_error_logging_and_safe_api_mapping(test_settings, caplog, capsys):
    # 1. Test 400 Bad Request
    transport_400 = httpx.MockTransport(lambda req: httpx.Response(400, json={"error": "Invalid payload"}))
    async with httpx.AsyncClient(transport=transport_400) as http_c:
        client = NotificationServiceClient(test_settings, client=http_c)
        with caplog.at_level(logging.WARNING):
            res400 = await client.send_otp("+919100209205", "123456", purpose="login", challenge_id="cid-400")
        assert res400.success is False
        captured = capsys.readouterr()
        all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
        assert "outcome=REJECTED error=BAD_REQUEST" in all_output

    caplog.clear()

    # 2. Test 409 Conflict
    transport_409 = httpx.MockTransport(lambda req: httpx.Response(409, json={"error": "Idempotency conflict"}))
    async with httpx.AsyncClient(transport=transport_409) as http_c:
        client = NotificationServiceClient(test_settings, client=http_c)
        with caplog.at_level(logging.WARNING):
            res409 = await client.send_otp("+919100209205", "123456", purpose="login", challenge_id="cid-409")
        assert res409.success is False
        captured = capsys.readouterr()
        all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
        assert "outcome=REJECTED error=IDEMPOTENCY_CONFLICT" in all_output

    caplog.clear()

    # 3. Test 503 Service Unavailable from external provider
    transport_503 = httpx.MockTransport(lambda req: httpx.Response(503, json={"error": "Downstream unavailable"}))
    async with httpx.AsyncClient(transport=transport_503) as http_c:
        client = NotificationServiceClient(test_settings, client=http_c)
        with caplog.at_level(logging.WARNING):
            res503 = await client.send_otp("+919100209205", "123456", purpose="login", challenge_id="cid-503")
        assert res503.success is False
        captured = capsys.readouterr()
        all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
        assert "outcome=REJECTED error=HTTP_503" in all_output

    caplog.clear()

    # 4. Test ConnectTimeout
    def mock_timeout(req):
        raise httpx.ConnectTimeout("Connection timed out")

    transport_timeout = httpx.MockTransport(mock_timeout)
    async with httpx.AsyncClient(transport=transport_timeout) as http_c:
        client = NotificationServiceClient(test_settings, client=http_c)
        with caplog.at_level(logging.ERROR):
            res_to = await client.send_otp("+919100209205", "123456", purpose="login", challenge_id="cid-to")
        assert res_to.success is False
        captured = capsys.readouterr()
        all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
        assert "outcome=TIMED_OUT error=CONNECT_TIMEOUT" in all_output


# ===========================================================================
# Scenario 9: Valid webhook response status and structured log emission
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_9_webhook_status_valid_hmac_and_structured_logging(sqlite_factory, test_settings, caplog, capsys):
    app = create_app(test_settings)
    app.state.test_factory = sqlite_factory
    app.dependency_overrides[get_db] = lambda: sqlite_factory()

    msg_id = "msg_webhook_valid_01"
    now = datetime.now(UTC)
    with sqlite_factory.begin() as session:
        session.add(NotificationMessage(
            tenant_id=None,
            message_id=msg_id,
            channel="WHATSAPP",
            message_type="LOGIN_OTP",
            recipient="+919100209205",
            status="QUEUED",
            priority="P0",
            purpose="login",
            challenge_id=None,
            occurred_at=now,
            created_at=now,
            updated_at=now,
        ))

    payload = {
        "eventId": "evt_valid_001",
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": msg_id,
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "timestamp": now.isoformat(),
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_body)

    headers = {
        "X-Notification-Timestamp": ts,
        "X-Notification-Signature": sig,
        "X-Event-ID": "evt_valid_001",
        "Content-Type": "application/json",
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
    ) as client:
        with caplog.at_level(logging.INFO):
            res = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)

    assert res.status_code == 200
    data = res.json()
    assert data["status"] in {"processed", "accepted"}
    assert data["event_id"] == "evt_valid_001"
    assert data["message_id"] == msg_id

    # Verify structured log for successful webhook
    captured = capsys.readouterr()
    all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
    assert "notification_webhook_result" in all_output
    assert "http_status=200" in all_output
    assert "event_id=evt_valid_001" in all_output
    assert "event_status=DELIVERED" in all_output
    assert "outcome=processed" in all_output or "outcome=accepted" in all_output


# ===========================================================================
# Scenario 10: Duplicate webhook event and invalid HMAC rejection logging
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_10_webhook_duplicate_event_and_invalid_hmac_logging(sqlite_factory, test_settings, caplog, capsys):
    app = create_app(test_settings)
    app.state.test_factory = sqlite_factory
    app.dependency_overrides[get_db] = lambda: sqlite_factory()

    msg_id = "msg_webhook_dup_01"
    now = datetime.now(UTC)
    with sqlite_factory.begin() as session:
        session.add(NotificationMessage(
            tenant_id=None,
            message_id=msg_id,
            channel="WHATSAPP",
            message_type="LOGIN_OTP",
            recipient="+919100209205",
            status="QUEUED",
            priority="P0",
            purpose="login",
            challenge_id=None,
            occurred_at=now,
            created_at=now,
            updated_at=now,
        ))

    payload = {
        "eventId": "evt_dup_001",
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": msg_id,
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "timestamp": now.isoformat(),
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_body)

    headers = {
        "X-Notification-Timestamp": ts,
        "X-Notification-Signature": sig,
        "X-Event-ID": "evt_dup_001",
        "Content-Type": "application/json",
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
    ) as client:
        # First call: processed/accepted
        res1 = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)
        assert res1.status_code == 200
        assert res1.json()["status"] in {"processed", "accepted"}

        caplog.clear()

        # Second call: duplicate event
        with caplog.at_level(logging.INFO):
            res2 = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)
        assert res2.status_code == 200
        assert res2.json()["status"] == "duplicate"

        # Verify duplicate log
        captured = capsys.readouterr()
        all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
        assert "notification_webhook_result" in all_output
        assert "http_status=200" in all_output
        assert "event_id=evt_dup_001" in all_output
        assert "outcome=duplicate" in all_output

        caplog.clear()

        # Third call: invalid HMAC signature
        bad_headers = dict(headers)
        bad_headers["X-Notification-Signature"] = "sha256=invaliddeadbeef"
        with caplog.at_level(logging.WARNING):
            res3 = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=bad_headers)
        assert res3.status_code == 401

        # Verify invalid signature warning log
        captured = capsys.readouterr()
        all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
        assert "notification_webhook_result" in all_output
        assert "http_status=401" in all_output
        assert "outcome=rejected" in all_output


# ===========================================================================
# Scenario 11: Webhook processing/database failure logging
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_11_webhook_database_failure_logging(sqlite_factory, test_settings, caplog, capsys):
    app = create_app(test_settings)
    app.state.test_factory = sqlite_factory
    app.dependency_overrides[get_db] = lambda: sqlite_factory()

    now = datetime.now(UTC)
    payload = {
        "eventId": "evt_fail_001",
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_fail_001",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "timestamp": now.isoformat(),
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_body)

    headers = {
        "X-Notification-Timestamp": ts,
        "X-Notification-Signature": sig,
        "X-Event-ID": "evt_fail_001",
        "Content-Type": "application/json",
    }

    # Simulate database error during process_webhook
    with patch.object(NotificationWebhookService, "process_webhook", side_effect=RuntimeError("Database failure")):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://localhost:8000",
        ) as client:
            with caplog.at_level(logging.ERROR):
                res = await client.post("/api/v1/webhooks/notifications/status", content=raw_body, headers=headers)

    assert res.status_code == 500
    captured = capsys.readouterr()
    all_output = captured.out + captured.err + "\n".join(r.message for r in caplog.records)
    assert "notification_webhook_result" in all_output
    assert "http_status=500" in all_output
    assert "outcome=failed" in all_output


# ===========================================================================
# Scenario 12: OTP verification, expiry, attempt limits, and replay protection
# ===========================================================================
@pytest.mark.anyio
async def test_scenario_12_otp_verification_security_and_replay_protection(sqlite_factory, test_settings):
    mock_client = MagicMock(spec=NotificationServiceClient)
    mock_client.send_otp = AsyncMock(return_value=NotificationDispatchResult(
        success=True, message_id="msg_scen_12", status="QUEUED", channel="WHATSAPP"
    ))

    otp_svc = OtpService(
        settings=test_settings,
        notification_client=mock_client,
        session_factory=sqlite_factory,
    )

    mobile = "+919100209205"

    # Capture the generated OTP via terminal display hook
    with patch.object(otp_svc, "_print_dev_otp_to_terminal") as mock_print:
        cid, _ = await otp_svc.request_otp(mobile, purpose="login")
        call_args = mock_print.call_args[1]
        correct_otp = call_args["otp_code"]

    # 1. Verify correct OTP succeeds
    success, token = otp_svc.verify_otp(
        challenge_id=cid,
        mobile_number=mobile,
        otp=correct_otp,
        purpose="login",
    )
    assert success is True

    # 2. Replay attack: submitting the same OTP again must FAIL (challenge was consumed)
    with pytest.raises(OtpVerificationFailed, match="already been used"):
        otp_svc.verify_otp(
            challenge_id=cid,
            mobile_number=mobile,
            otp=correct_otp,
            purpose="login",
        )

    # 3. Wrong OTP increments failed_attempts and locks after maximum attempts (e.g. 5)
    with sqlite_factory.begin() as session:
        session.query(OtpRateBucket).delete()

    with patch.object(otp_svc, "_print_dev_otp_to_terminal") as mock_print:
        cid2, _ = await otp_svc.request_otp(mobile, purpose="login")
        correct_otp2 = mock_print.call_args[1]["otp_code"]

    # Submit 4 incorrect attempts
    for i in range(4):
        with pytest.raises(OtpVerificationFailed, match="Invalid verification code"):
            otp_svc.verify_otp(cid2, mobile, "000000", purpose="login")

    # 5th attempt exhausts attempts and locks challenge
    with pytest.raises(OtpVerificationFailed, match="Too many failed attempts"):
        otp_svc.verify_otp(cid2, mobile, "000000", purpose="login")

    # Even correct OTP is now rejected because it's locked
    with pytest.raises(OtpVerificationFailed, match="already been used|no longer valid|Too many failed attempts"):
        otp_svc.verify_otp(cid2, mobile, correct_otp2, purpose="login")

    # 4. Delivery webhook arrival does NOT mark OTP as verified
    with sqlite_factory.begin() as session:
        session.query(OtpRateBucket).delete()

    with patch.object(otp_svc, "_print_dev_otp_to_terminal") as mock_print:
        cid3, _ = await otp_svc.request_otp(mobile, purpose="login")

    # Simulate webhook delivery event arriving
    webhook_svc = NotificationWebhookService(test_settings)
    now = datetime.now(UTC)
    raw_wh = json.dumps({
        "eventId": "evt_otp_sec_01",
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": "msg_scen_12",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "timestamp": now.isoformat(),
        "occurredAt": now.isoformat(),
    }).encode("utf-8")
    ts = str(int(now.timestamp()))
    sig = compute_signature(ts, raw_wh)

    with sqlite_factory() as session:
        webhook_svc.process_webhook(
            session=session,
            raw_body=raw_wh,
            timestamp_header=ts,
            signature_header=sig,
            event_id_header="evt_otp_sec_01",
        )

    # Verify challenge in DB is NOT marked consumed or verified!
    with sqlite_factory() as session:
        ch = session.execute(select(OtpChallenge).where(OtpChallenge.id == cid3)).scalar_one()
        assert ch.consumed_at is None
        assert ch.verified_at is None
