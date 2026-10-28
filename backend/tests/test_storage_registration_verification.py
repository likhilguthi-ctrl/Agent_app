"""Verification tests for registration token authorization and size limits on Cloudflare R2 storage upload.

Covers:
- Unauthenticated upload rejection (HTTP 401)
- Valid registration verification token upload (agent_registration -> agent-photo, aadhaar-document, pan-document)
- Valid user_registration token upload (user-photo allowed, agent-photo rejected with 403)
- Purpose authorization enforcement (login tokens cannot upload photos -> 403)
- Token expiration enforcement (verified_at > 15m -> 401)
- Single-use/consumed token enforcement (consumed_at not None -> 401)
- Hard size limit enforcement (> 500 KB -> 422)
- Metadata tracking on challenge (upload count, object keys)
- Preview authorization via registration verification token (same tenant vs cross-tenant)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import OtpChallenge
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.registration_photos.models import StoredObject
from app.modules.registration_photos.storage import MockedR2Storage
from app.modules.tenants.models import Tenant


def create_test_image(
    format: str = "JPEG",
    size: tuple[int, int] = (64, 64),
    large: bool = False,
) -> bytes:
    buf = BytesIO()
    if large:
        img = Image.new("RGB", (1200, 1200))
        pixels = [
            (
                (x * 7 + y * 13) % 256,
                (x * 17 + y * 29) % 256,
                (x * 31 + y * 47) % 256,
            )
            for y in range(1200)
            for x in range(1200)
        ]
        img.putdata(pixels)
        img.save(buf, format=format, quality=95)
    else:
        img = Image.new("RGB", size, color=(50, 100, 150))
        img.save(buf, format=format)
    return buf.getvalue()


@pytest.fixture
def storage_reg_harness(tmp_path):
    database_path = tmp_path / "reg-storage-test.sqlite"
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
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    tenant_a_id = uuid4()
    tenant_b_id = uuid4()

    now = datetime.now(UTC)
    active_agent_token = "valid-agent-reg-token-abc"
    active_user_token = "valid-user-reg-token-xyz"
    expired_token = "expired-reg-token-123"
    consumed_token = "consumed-reg-token-456"
    login_token = "login-token-789"

    with factory.begin() as session:
        ta = Tenant(id=tenant_a_id, slug="tenant-a", status="active")
        tb = Tenant(id=tenant_b_id, slug="tenant-b", status="active")
        session.add_all([ta, tb])
        session.flush()

        # Active Agent Registration Challenge
        session.add(
            OtpChallenge(
                id=uuid4(),
                tenant_id=tenant_a_id,
                mobile_e164="+919876543210",
                purpose="agent_registration",
                otp_hash="testhash",
                otp_salt="testsalt",
                created_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(minutes=10),
                verified_at=now - timedelta(minutes=2),
                verification_token=active_agent_token,
                metadata_json={},
            )
        )

        # Active User/Subscriber Registration Challenge
        session.add(
            OtpChallenge(
                id=uuid4(),
                tenant_id=tenant_a_id,
                mobile_e164="+919876543211",
                purpose="user_registration",
                otp_hash="testhash",
                otp_salt="testsalt",
                created_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(minutes=10),
                verified_at=now - timedelta(minutes=2),
                verification_token=active_user_token,
                metadata_json={},
            )
        )

        # Expired Challenge (> 15 minutes since verified_at)
        session.add(
            OtpChallenge(
                id=uuid4(),
                tenant_id=tenant_a_id,
                mobile_e164="+919876543212",
                purpose="agent_registration",
                otp_hash="testhash",
                otp_salt="testsalt",
                created_at=now - timedelta(minutes=30),
                expires_at=now + timedelta(minutes=10),
                verified_at=now - timedelta(minutes=16),
                verification_token=expired_token,
                metadata_json={},
            )
        )

        # Consumed Challenge
        session.add(
            OtpChallenge(
                id=uuid4(),
                tenant_id=tenant_a_id,
                mobile_e164="+919876543213",
                purpose="agent_registration",
                otp_hash="testhash",
                otp_salt="testsalt",
                created_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(minutes=10),
                verified_at=now - timedelta(minutes=3),
                consumed_at=now - timedelta(minutes=1),
                verification_token=consumed_token,
                metadata_json={},
            )
        )

        # Login Token (not registration)
        session.add(
            OtpChallenge(
                id=uuid4(),
                tenant_id=tenant_a_id,
                mobile_e164="+919876543214",
                purpose="login",
                otp_hash="testhash",
                otp_salt="testsalt",
                created_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(minutes=10),
                verified_at=now - timedelta(minutes=2),
                verification_token=login_token,
                metadata_json={},
            )
        )

    mock_storage = MockedR2Storage(bucket_name="avanflix-r2-reg-test")

    test_settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        r2_bucket_name="avanflix-r2-reg-test",
    )

    app = create_app(test_settings)
    app.state.mock_storage = mock_storage

    auth_repo = SQLAlchemyAuthenticationRepository(factory)
    app.dependency_overrides[get_authentication_repository] = lambda: auth_repo

    def _get_test_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_test_db

    return {
        "app": app,
        "factory": factory,
        "storage": mock_storage,
        "tenant_a_id": tenant_a_id,
        "tenant_b_id": tenant_b_id,
        "active_agent_token": active_agent_token,
        "active_user_token": active_user_token,
        "expired_token": expired_token,
        "consumed_token": consumed_token,
        "login_token": login_token,
    }


def client_for(app):
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:3000"},
    )


@pytest.mark.anyio
async def test_unauthenticated_upload_rejected_with_401(storage_reg_harness):
    app = storage_reg_harness["app"]
    img = create_test_image("JPEG")

    async with client_for(app) as client:
        res = await client.post(
            "/api/v1/storage/upload",
            files={"file": ("photo.jpg", img, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert res.status_code == 401


@pytest.mark.anyio
async def test_registration_token_agent_photo_upload_success(storage_reg_harness):
    app = storage_reg_harness["app"]
    factory = storage_reg_harness["factory"]
    token = storage_reg_harness["active_agent_token"]
    tenant_a_id = storage_reg_harness["tenant_a_id"]
    img = create_test_image("JPEG")

    async with client_for(app) as client:
        res = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": token},
            files={"file": ("agent.jpg", img, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert res.status_code == 201
        body = res.json()
        assert body["category"] == "agent-photo"
        assert body["content_type"] == "image/jpeg"
        key = body["object_key"]
        assert key.startswith("agent-photos/")

        with factory() as session:
            stored = session.query(StoredObject).filter_by(object_key=key).one()
            assert stored.tenant_id == tenant_a_id
            assert stored.uploader_user_id is None
            assert stored.file_size > 0
            assert stored.file_size == body["file_size"]

            challenge = session.query(OtpChallenge).filter_by(verification_token=token).one()
            assert key in challenge.metadata_json["registration_photo_keys"]
            assert challenge.metadata_json["registration_photo_upload_count"] == 1


@pytest.mark.anyio
async def test_registration_token_aadhaar_and_pan_success(storage_reg_harness):
    app = storage_reg_harness["app"]
    token = storage_reg_harness["active_agent_token"]
    img = create_test_image("PNG")

    async with client_for(app) as client:
        # Aadhaar Document
        res_aadhaar = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": token},
            files={"file": ("aadhaar.png", img, "image/png")},
            data={"category": "aadhaar-document"},
        )
        assert res_aadhaar.status_code == 201
        assert res_aadhaar.json()["category"] == "aadhaar-document"

        # PAN Document
        res_pan = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": token},
            files={"file": ("pan.png", img, "image/png")},
            data={"category": "pan-document"},
        )
        assert res_pan.status_code == 201
        assert res_pan.json()["category"] == "pan-document"


@pytest.mark.anyio
async def test_registration_token_bearer_header_and_form_field(storage_reg_harness):
    app = storage_reg_harness["app"]
    token = storage_reg_harness["active_agent_token"]
    img = create_test_image("JPEG")

    async with client_for(app) as client:
        # Via Authorization: Bearer <token>
        res1 = await client.post(
            "/api/v1/storage/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("agent1.jpg", img, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert res1.status_code == 201

        # Via form-data verification_token field
        res2 = await client.post(
            "/api/v1/storage/upload",
            files={"file": ("agent2.jpg", img, "image/jpeg")},
            data={"category": "agent-photo", "verification_token": token},
        )
        assert res2.status_code == 201


@pytest.mark.anyio
async def test_purpose_category_restriction_enforcement(storage_reg_harness):
    app = storage_reg_harness["app"]
    user_token = storage_reg_harness["active_user_token"]
    img = create_test_image("JPEG")

    async with client_for(app) as client:
        # user_registration can upload user-photo
        res_ok = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": user_token},
            files={"file": ("user.jpg", img, "image/jpeg")},
            data={"category": "user-photo"},
        )
        assert res_ok.status_code == 201

        # user_registration CANNOT upload agent-photo
        res_bad = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": user_token},
            files={"file": ("agent.jpg", img, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert res_bad.status_code == 403


@pytest.mark.anyio
async def test_login_purpose_token_rejected_for_upload(storage_reg_harness):
    app = storage_reg_harness["app"]
    login_token = storage_reg_harness["login_token"]
    img = create_test_image("JPEG")

    async with client_for(app) as client:
        res = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": login_token},
            files={"file": ("photo.jpg", img, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert res.status_code == 403


@pytest.mark.anyio
async def test_expired_token_rejected_with_401(storage_reg_harness):
    app = storage_reg_harness["app"]
    expired_token = storage_reg_harness["expired_token"]
    img = create_test_image("JPEG")

    async with client_for(app) as client:
        res = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": expired_token},
            files={"file": ("photo.jpg", img, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert res.status_code == 401


@pytest.mark.anyio
async def test_consumed_token_rejected_with_401(storage_reg_harness):
    app = storage_reg_harness["app"]
    consumed_token = storage_reg_harness["consumed_token"]
    img = create_test_image("JPEG")

    async with client_for(app) as client:
        res = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": consumed_token},
            files={"file": ("photo.jpg", img, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert res.status_code == 401


@pytest.mark.anyio
async def test_oversize_image_rejected_with_422(storage_reg_harness):
    app = storage_reg_harness["app"]
    token = storage_reg_harness["active_agent_token"]
    large_img = create_test_image("JPEG", large=True)
    assert len(large_img) > 500 * 1024

    async with client_for(app) as client:
        res = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": token},
            files={"file": ("large.jpg", large_img, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert res.status_code == 422


@pytest.mark.anyio
async def test_preview_with_registration_token_same_tenant_vs_cross_tenant(storage_reg_harness):
    app = storage_reg_harness["app"]
    token = storage_reg_harness["active_agent_token"]
    tenant_b_id = storage_reg_harness["tenant_b_id"]
    factory = storage_reg_harness["factory"]
    img = create_test_image("JPEG")

    async with client_for(app) as client:
        # 1. Upload an object to tenant A
        upload_res = await client.post(
            "/api/v1/storage/upload",
            headers={"X-Registration-Verification-Token": token},
            files={"file": ("preview_test.jpg", img, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert upload_res.status_code == 201
        object_key = upload_res.json()["object_key"]

        # 2. Preview with matching registration token in header -> 200
        preview_res = await client.get(
            f"/api/v1/storage/{object_key}/preview",
            headers={"X-Registration-Verification-Token": token},
        )
        assert preview_res.status_code == 200
        assert preview_res.headers["cache-control"] == "private, no-store, max-age=0"

        # 3. Preview with matching token in query parameter -> 200
        preview_query_res = await client.get(
            f"/api/v1/storage/{object_key}/preview?verification_token={token}",
        )
        assert preview_query_res.status_code == 200

        # 4. Create a challenge in Tenant B and attempt cross-tenant access to Tenant A's object
        tenant_b_token = "tenant-b-token-xyz"
        with factory.begin() as session:
            session.add(
                OtpChallenge(
                    id=uuid4(),
                    tenant_id=tenant_b_id,
                    mobile_e164="+919876543299",
                    purpose="agent_registration",
                    otp_hash="testhash",
                    otp_salt="testsalt",
                    created_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(minutes=10),
                    verified_at=datetime.now(UTC),
                    verification_token=tenant_b_token,
                    metadata_json={},
                )
            )

        cross_res = await client.get(
            f"/api/v1/storage/{object_key}/preview",
            headers={"X-Registration-Verification-Token": tenant_b_token},
        )
        assert cross_res.status_code == 403
