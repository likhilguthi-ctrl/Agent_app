"""Automated tests for Registration Workflow Upgrade.

Covers:
1. Canonical State & District validation:
   - Admin: State mandatory (Telangana/AP), District not required/persisted.
   - Agent: State and District mandatory, filtered & validated.
2. Fixed Indian Mobile Number format (+91[6-9]d{9}).
3. WhatsApp number selection and verification (whatsapp_verification purpose).
4. Role-specific email & email OTP verification (email_verification purpose).
5. Token consumption and replay protection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.agents.schemas import AgentCreateRequest, UserRegistrationRequest
from app.modules.authentication.email_otp_service import (
    EmailOtpService,
    EmailOtpVerificationFailed,
    normalize_email,
)
from app.modules.authentication.models import AccountCredential, EmailOtpChallenge, OtpChallenge
from app.modules.authentication.otp_service import OtpService
from app.modules.identity.models import TenantMembership, User
from app.modules.tenants.locations import (
    CANONICAL_LOCATIONS,
    SUPPORTED_STATES,
    get_districts_for_state,
    get_or_create_region_for_district,
    normalize_district_name,
    validate_state_district,
)
from app.modules.tenants.models import AdminProfile, Region, Tenant
from app.modules.tenants.schemas import AdministratorCreateRequest


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
def test_settings():
    return Settings(
        _env_file=None,
        environment="test",
        app_name="CommunityOTTTest",
        frontend_origins="http://localhost:3000",
        otp_challenge_ttl_seconds=300,
        otp_send_cooldown_seconds=60,
        otp_verify_limit=5,
        otp_delivery_mode="mock",
        notification_service_base_url="https://notification.example.com",
        notification_service_api_key=SecretStr("fixed-test-api-key"),
        identity_encryption_key=b"12345678901234567890123456789012",
    )


# =============================================================================
# 1. State and District Rules & Canonical Locations
# =============================================================================

def test_canonical_locations_and_district_counts():
    assert "Andhra Pradesh" in SUPPORTED_STATES
    assert "Telangana" in SUPPORTED_STATES
    assert len(SUPPORTED_STATES) == 2

    ap_districts = get_districts_for_state("Andhra Pradesh")
    ts_districts = get_districts_for_state("Telangana")

    assert len(ap_districts) == 26
    assert len(ts_districts) == 33

    assert "Visakhapatnam" in ap_districts
    assert "Dr. B. R. Ambedkar Konaseema" in ap_districts
    assert "Hyderabad" in ts_districts
    assert "Rangareddy" in ts_districts


def test_validate_state_district_rules():
    # Valid matching
    assert validate_state_district("Telangana", "Hyderabad") is True
    assert normalize_district_name("Hyderabad") == "Hyderabad"

    assert validate_state_district("Telangana", "Rangareddy") is True
    assert normalize_district_name("Rangareddy") == "Rangareddy"

    # Legacy spelling alias
    assert validate_state_district("Telangana", "Ranga Reddy") is True
    assert normalize_district_name("Ranga Reddy") == "Rangareddy"

    # Valid AP
    assert validate_state_district("Andhra Pradesh", "Visakhapatnam") is True
    assert normalize_district_name("Visakhapatnam") == "Visakhapatnam"

    # Mismatch: AP district under Telangana
    assert validate_state_district("Telangana", "Visakhapatnam") is False

    # Mismatch: TS district under AP
    assert validate_state_district("Andhra Pradesh", "Hyderabad") is False

    # Invalid state
    assert validate_state_district("Karnataka", "Bengaluru") is False


# =============================================================================
# 2. Fixed Indian Mobile Number Validation
# =============================================================================

def test_fixed_indian_mobile_schema_validation():
    # Valid: +91 followed by 10 digits starting with 6, 7, 8, or 9
    valid_mobile = "+919876543210"
    valid_admin_dict = {
        "first_name": "Test",
        "last_name": "Admin",
        "username": "testadmin",
        "password": "Password123!",
        "mobile_e164": valid_mobile,
        "email": "admin@example.com",
        "email_verification_token": "token-123",
        "state": "Telangana",
    }
    req = AdministratorCreateRequest(**valid_admin_dict)
    assert req.mobile_e164 == valid_mobile

    # Invalid non-Indian prefixes
    with pytest.raises(ValidationError):
        AdministratorCreateRequest(**{**valid_admin_dict, "mobile_e164": "+12025550199"})

    # Invalid digit count (9 digits instead of 10)
    with pytest.raises(ValidationError):
        AdministratorCreateRequest(**{**valid_admin_dict, "mobile_e164": "+91987654321"})

    # Invalid starting digit (5 instead of 6-9)
    with pytest.raises(ValidationError):
        AdministratorCreateRequest(**{**valid_admin_dict, "mobile_e164": "+915123456789"})

    # Missing +91 prefix
    with pytest.raises(ValidationError):
        AdministratorCreateRequest(**{**valid_admin_dict, "mobile_e164": "9876543210"})


# =============================================================================
# 3. WhatsApp Number Selection & Verification
# =============================================================================

def test_whatsapp_same_as_mobile_defaults():
    req = AdministratorCreateRequest(
        first_name="Test",
        last_name="Admin",
        username="testadmin",
        password="Password123!",
        mobile_e164="+919876543210",
        email="admin@example.com",
        email_verification_token="token-123",
        state="Telangana",
        whatsapp_same_as_mobile=True,
    )
    assert req.whatsapp_same_as_mobile is True
    assert req.whatsapp_number is None  # Defaults to mobile_e164 in business logic


def test_whatsapp_alternative_number_requires_valid_indian_format():
    # Valid alternative Indian WhatsApp
    req = AdministratorCreateRequest(
        first_name="Test",
        last_name="Admin",
        username="testadmin",
        password="Password123!",
        mobile_e164="+919876543210",
        whatsapp_same_as_mobile=False,
        whatsapp_number="+918765432109",
        email="admin@example.com",
        email_verification_token="token-123",
        state="Telangana",
    )
    assert req.whatsapp_number == "+918765432109"

    # Invalid alternative WhatsApp format
    with pytest.raises(ValidationError):
        AdministratorCreateRequest(
            first_name="Test",
            last_name="Admin",
            username="testadmin",
            password="Password123!",
            mobile_e164="+919876543210",
            whatsapp_same_as_mobile=False,
            whatsapp_number="invalid-num",
            email="admin@example.com",
            email_verification_token="token-123",
            state="Telangana",
        )


# =============================================================================
# 4. Email OTP Service: Challenge, Verification, Cooldown, Lockout, and Token
# =============================================================================

@pytest.mark.anyio
async def test_email_otp_lifecycle(memory_db, test_settings):
    session_factory, _ = memory_db
    mock_notif = AsyncMock()
    mock_notif.send_email = AsyncMock(return_value={"status": "accepted"})
    email_service = EmailOtpService(settings=test_settings, session_factory=session_factory, notification_client=mock_notif)
    test_email = "test.user@domain.com"

    # Step 1: Request Email OTP
    result = await email_service.request_otp(email=test_email, purpose="email_verification")
    assert result["status"] == "accepted"
    challenge_id = UUID(result["challenge_id"])
    assert result["cooldown_seconds"] == 60

    # Verify challenge in DB
    with session_factory() as session:
        ch = session.execute(
            select(EmailOtpChallenge).where(EmailOtpChallenge.id == challenge_id)
        ).scalar_one()
        assert ch.email == "test.user@domain.com"
        assert ch.purpose == "email_verification"
        assert ch.failed_attempts == 0
        assert ch.consumed_at is None

    # Step 2: Test Cooldown Enforcement
    cooldown_res = await email_service.request_otp(email=test_email, purpose="email_verification")
    assert cooldown_res["status"] == "cooldown"
    assert cooldown_res["cooldown_seconds"] > 0

    # Step 3: Wrong OTP raises EmailOtpVerificationFailed and decrements attempts
    with pytest.raises(EmailOtpVerificationFailed) as exc_info:
        email_service.verify_otp(challenge_id=challenge_id, email=test_email, otp="999999", purpose="email_verification")
    assert exc_info.value.attempts_remaining == 4

    with session_factory() as session:
        ch_after = session.execute(
            select(EmailOtpChallenge).where(EmailOtpChallenge.id == challenge_id)
        ).scalar_one()
        assert ch_after.failed_attempts == 1

    # Step 4: Correct OTP succeeds and returns a verification token
    with session_factory() as session:
        ch = session.execute(
            select(EmailOtpChallenge).where(EmailOtpChallenge.id == challenge_id)
        ).scalar_one()
        ch.otp_hash = email_service._hash_otp("123456", ch.otp_salt)
        session.commit()

    success, v_token = email_service.verify_otp(challenge_id=challenge_id, email=test_email, otp="123456", purpose="email_verification")
    assert success is True
    assert v_token is not None

    # Step 5: Consume verification token
    with session_factory() as session:
        consumed = email_service.validate_and_consume_token(
            session, v_token, test_email, expected_purpose="email_verification"
        )
        session.commit()
        assert consumed is True

        # Step 6: Replay protection: Attempting to reuse the same token must fail
        replay = email_service.validate_and_consume_token(
            session, v_token, test_email, expected_purpose="email_verification"
        )
        assert replay is False


@pytest.mark.anyio
async def test_email_otp_lockout_after_5_attempts(memory_db, test_settings):
    session_factory, _ = memory_db
    mock_notif = AsyncMock()
    mock_notif.send_email = AsyncMock(return_value={"status": "accepted"})
    email_service = EmailOtpService(settings=test_settings, session_factory=session_factory, notification_client=mock_notif)
    test_email = "lockout@example.com"

    result = await email_service.request_otp(email=test_email, purpose="email_verification")
    cid = UUID(result["challenge_id"])

    # 5 wrong attempts
    for i in range(1, 6):
        with pytest.raises(EmailOtpVerificationFailed) as exc_info:
            email_service.verify_otp(challenge_id=cid, email=test_email, otp="000000", purpose="email_verification")
        expected_remaining = 5 - i
        assert exc_info.value.attempts_remaining == expected_remaining

    # Challenge should now be locked
    with session_factory() as session:
        ch = session.execute(
            select(EmailOtpChallenge).where(EmailOtpChallenge.id == cid)
        ).scalar_one()
        assert ch.failed_attempts == 5
        assert ch.consumed_at is not None

    # Further attempt fails with locked message
    with pytest.raises(EmailOtpVerificationFailed) as exc_info:
        email_service.verify_otp(challenge_id=cid, email=test_email, otp="123456", purpose="email_verification")
    assert "locked" in str(exc_info.value).lower()


# =============================================================================
# 5. Role-Specific Registration Schemas & Rules
# =============================================================================

def test_admin_schema_state_mandatory_district_not_required():
    # Admin only requires state (Telangana or Andhra Pradesh)
    admin_req = AdministratorCreateRequest(
        first_name="Venkat",
        last_name="Rao",
        username="venkat_admin",
        password="SecurePassword123!",
        mobile_e164="+919876543210",
        email="venkat@community.org",
        email_verification_token="valid-email-token",
        state="Telangana",
    )
    assert admin_req.state == "Telangana"
    assert getattr(admin_req, "district", None) is None


def test_agent_schema_state_and_district_mandatory():
    # Agent requires both state and district
    agent_req = AgentCreateRequest(
        first_name="Ramesh",
        last_name="Kumar",
        username="ramesh_agent",
        password="AgentPassword123!",
        mobile_e164="+919876543210",
        email="ramesh@agent.org",
        email_verification_token="agent-email-token",
        whatsapp_same_as_mobile=True,
        state="Andhra Pradesh",
        district="Visakhapatnam",
        aadhaar_number="123456789012",
        aadhaar_document_key="aadhaar-key-1",
        pan_number="ABCDE1234F",
        pan_document_key="pan-key-1",
        profile_photo_key="photo-key-1",
    )
    assert agent_req.state == "Andhra Pradesh"
    assert agent_req.district == "Visakhapatnam"


def test_user_subscriber_schema_optional_email():
    # Subscriber without email
    sub_no_email = UserRegistrationRequest(
        full_name="Priya Sharma",
        mobile_e164="+919876543210",
    )
    assert sub_no_email.email is None
    assert sub_no_email.email_verification_token is None

    # Subscriber with email
    sub_with_email = UserRegistrationRequest(
        full_name="Priya Sharma",
        mobile_e164="+919876543210",
        email="priya@example.com",
        email_verification_token="priya-email-token",
    )
    assert sub_with_email.email == "priya@example.com"
    assert sub_with_email.email_verification_token == "priya-email-token"


# =============================================================================
# 6. Service-Level Registration Workflow Tests
# =============================================================================

from app.modules.agents.api import _create_agent_in_community
from app.modules.registration_photos.models import StoredObject
from fastapi import HTTPException


def test_agent_registration_service_validations(memory_db, test_settings):
    session_factory, _ = memory_db
    tenant_id = uuid4()
    operator_user_id = uuid4()
    now = datetime.now(UTC)

    with session_factory() as session:
        # Seed Tenant
        tenant = Tenant(
            id=tenant_id,
            slug="test-community",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.commit()

        # Seed StoredObjects
        photo_key = "test-agent-photo"
        aadhaar_key = "test-aadhaar-doc"
        pan_key = "test-pan-doc"
        for k, cat in [(photo_key, "agent-photo"), (aadhaar_key, "aadhaar-document"), (pan_key, "pan-document")]:
            session.add(
                StoredObject(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    object_key=k,
                    category=cat,
                    bucket_name="r2-test-bucket",
                    content_type="image/jpeg",
                    file_size=1024,
                    created_at=now,
                )
            )
        session.commit()

        base_agent_data = {
            "first_name": "Kiran",
            "last_name": "Kumar",
            "username": "kiran_agent",
            "password": "Password123!",
            "mobile_e164": "+919876543210",
            "whatsapp_same_as_mobile": True,
            "email": "kiran@example.com",
            "email_verification_token": "valid-token",
            "state": "Telangana",
            "district": "Hyderabad",
            "aadhaar_number": "123456789012",
            "aadhaar_document_key": aadhaar_key,
            "pan_number": "ABCDE1234F",
            "pan_document_key": pan_key,
            "profile_photo_key": photo_key,
        }

        # 1. Reject invalid state
        with pytest.raises(HTTPException) as exc_info:
            _create_agent_in_community(
                session=session,
                tenant=tenant,
                body=AgentCreateRequest(**{**base_agent_data, "state": "Karnataka"}),
                created_by_user_id=operator_user_id,
                created_by_username="superadmin",
                created_by_membership_id=None,
                settings=test_settings,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"] == "INVALID_STATE"

        # 2. Reject state/district mismatch (AP district under Telangana)
        with pytest.raises(HTTPException) as exc_info:
            _create_agent_in_community(
                session=session,
                tenant=tenant,
                body=AgentCreateRequest(**{**base_agent_data, "state": "Telangana", "district": "Visakhapatnam"}),
                created_by_user_id=operator_user_id,
                created_by_username="superadmin",
                created_by_membership_id=None,
                settings=test_settings,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"] == "INVALID_DISTRICT"

        # 3. Reject missing email verification token
        with pytest.raises(HTTPException) as exc_info:
            _create_agent_in_community(
                session=session,
                tenant=tenant,
                body=AgentCreateRequest(**{**base_agent_data, "email_verification_token": None}),
                created_by_user_id=operator_user_id,
                created_by_username="superadmin",
                created_by_membership_id=None,
                settings=test_settings,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"] == "EMAIL_TOKEN_REQUIRED"

        # 4. Reject unverified alternative WhatsApp number
        with pytest.raises(HTTPException) as exc_info:
            _create_agent_in_community(
                session=session,
                tenant=tenant,
                body=AgentCreateRequest(
                    **{
                        **base_agent_data,
                        "whatsapp_same_as_mobile": False,
                        "whatsapp_number": "+918765432109",
                        "whatsapp_verification_token": None,
                    }
                ),
                created_by_user_id=operator_user_id,
                created_by_username="superadmin",
                created_by_membership_id=None,
                settings=test_settings,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"] == "WHATSAPP_TOKEN_REQUIRED"

        # 5. Successful Agent Registration with verified email OTP
        # Issue genuine email verification token
        v_token = "valid-email-verification-token"
        test_email = "kiran@example.com"
        token_hash = v_token
        session.add(
            EmailOtpChallenge(
                id=uuid4(),
                tenant_id=tenant_id,
                email=test_email,
                purpose="email_verification",
                otp_hash="hash",
                otp_salt="salt",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                verified_at=now,
                verification_token=token_hash,
            )
        )
        session.commit()

        summary = _create_agent_in_community(
            session=session,
            tenant=tenant,
            body=AgentCreateRequest(
                **{
                    **base_agent_data,
                    "email": test_email,
                    "email_verification_token": v_token,
                }
            ),
            created_by_user_id=operator_user_id,
            created_by_username="superadmin",
            created_by_membership_id=None,
            settings=test_settings,
        )

        assert summary.username == "kiran_agent"
        assert summary.state == "Telangana"
        assert summary.district == "Hyderabad"
        assert summary.mobile_e164 == "+919876543210"
        assert summary.whatsapp_number == "+919876543210"
        assert summary.whatsapp_verified is True
        assert summary.email == test_email
        assert summary.email_verified is True

        # Verify persisted AgentProfile
        profile = session.execute(
            select(AgentProfile).where(AgentProfile.id == summary.id)
        ).scalar_one()
        assert profile.state == "Telangana"
        assert profile.district == "Hyderabad"
        assert profile.whatsapp_number == "+919876543210"
        assert profile.whatsapp_verified is True
        assert profile.email_verified is True
        assert profile.email_verified_at is not None

        # Verify canonical Region was auto-bound
        region = session.execute(
            select(Region).where(Region.id.in_(summary.assigned_region_ids))
        ).scalar_one()
        assert region.name == "Hyderabad"
        assert region.state == "Telangana"


@pytest.mark.anyio
async def test_email_notification_service_client_contract(test_settings):
    from app.modules.messaging.notification_service import NotificationServiceClient

    client = NotificationServiceClient(test_settings)
    assert client.messages_endpoint_url.endswith("/api/v1/messages")

    # Mock mode delivery
    res = await client.send_email(
        recipient_email="test@example.com",
        subject="Test OTP",
        text_content="Your OTP is 123456",
        message_type="ACCOUNT_UPDATE",
    )
    assert res.success is True
    assert res.status == "MOCK_SENT"
    assert res.channel == "EMAIL"
    assert res.message_id is not None


def test_admin_creation_mobile_and_email_otp_requirements(memory_db, test_settings):
    from app.modules.tenants.api import create_administrator
    from unittest.mock import MagicMock

    session_factory, _ = memory_db
    tenant_id = uuid4()
    now = datetime.now(UTC)

    with session_factory() as session:
        tenant = Tenant(
            id=tenant_id,
            slug="admin-comm",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.commit()

        mock_operator = MagicMock()
        mock_operator.membership_id = uuid4()
        mock_operator.user_id = uuid4()
        mock_operator.username = "superadmin"

        mock_request = MagicMock()
        mock_request.app.state.settings = test_settings

        admin_data = {
            "first_name": "Rao",
            "last_name": "Bahadur",
            "username": "rao_admin",
            "password": "Password123!",
            "mobile_e164": "+919876543210",
            "email": "rao@admin.org",
            "state": "Telangana",
        }

        # 1. Missing mobile verification token
        with pytest.raises(HTTPException) as exc_info:
            create_administrator(
                community_id=tenant_id,
                body=AdministratorCreateRequest(**admin_data),
                operator=mock_operator,
                request=mock_request,
                session=session,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"] == "MOBILE_TOKEN_REQUIRED"

        # 2. Invalid mobile verification token
        with pytest.raises(HTTPException) as exc_info:
            create_administrator(
                community_id=tenant_id,
                body=AdministratorCreateRequest(**{**admin_data, "verification_token": "fake-mobile-token"}),
                operator=mock_operator,
                request=mock_request,
                session=session,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"] == "INVALID_OTP_TOKEN"

        # 3. Seed valid mobile OTP challenge for Step 4
        mobile_token_4 = "valid-mobile-token-step4"
        session.add(
            OtpChallenge(
                id=uuid4(),
                tenant_id=tenant_id,
                mobile_e164="+919876543210",
                purpose="admin_registration",
                otp_hash="hash",
                otp_salt="salt",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                verified_at=now,
                verification_token=mobile_token_4,
            )
        )
        session.commit()

        # 4. Valid mobile token, but missing email OTP token
        with pytest.raises(HTTPException) as exc_info:
            create_administrator(
                community_id=tenant_id,
                body=AdministratorCreateRequest(**{**admin_data, "verification_token": mobile_token_4}),
                operator=mock_operator,
                request=mock_request,
                session=session,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"] == "EMAIL_TOKEN_REQUIRED"

        # 5. Re-issue fresh mobile token and seed valid email OTP challenge and token for Step 6
        mobile_token = "valid-mobile-token-final"
        ch = session.execute(
            select(OtpChallenge).where(
                OtpChallenge.mobile_e164 == "+919876543210",
                OtpChallenge.purpose == "admin_registration",
            )
        ).scalar_one()
        ch.verification_token = mobile_token
        ch.consumed_at = None

        email_token = "valid-email-token-123"
        session.add(
            EmailOtpChallenge(
                id=uuid4(),
                tenant_id=tenant_id,
                email="rao@admin.org",
                purpose="email_verification",
                otp_hash="hash",
                otp_salt="salt",
                failed_attempts=0,
                resend_count=0,
                dispatch_id=uuid4(),
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                verified_at=now,
                verification_token=email_token,
            )
        )
        session.commit()

        # 6. Both mobile and email OTP verified -> Successful Admin creation
        summary = create_administrator(
            community_id=tenant_id,
            body=AdministratorCreateRequest(
                **{
                    **admin_data,
                    "verification_token": mobile_token,
                    "email_verification_token": email_token,
                }
            ),
            operator=mock_operator,
            request=mock_request,
            session=session,
        )
        assert summary.username == "rao_admin"
        assert summary.mobile_e164 == "+919876543210"
        assert summary.role == "ADMIN"
        assert summary.state == "Telangana"
        assert summary.email_verified is True
        assert summary.whatsapp_verified is True

