"""Stage 2: Comprehensive verification tests for Cloudflare R2 photo storage.

Covers:
- Backend-only S3-compatible R2 client initialization and endpoint derivation
- Magic bytes and signature validation (rejecting disguised executables, HTML, polyglots)
- Oversize image rejection (> 5MB) and dimension bounds
- Backend-controlled random object key generation (never client paths)
- Private Cloudflare R2 upload with PostgreSQL metadata tracking
- Transactional upload error handling and R2 cleanup on DB failure (zero orphaned objects)
- Authenticated preview streaming with Cache-Control no-store
- Short-lived presigned URL generation and expiry validation (60s to 900s)
- Cross-tenant isolation (Tenant B cannot read Tenant A's objects)
- Role-based authorization (Super Admin global access, Tenant Admin scoped access)
- Tenant/Owner object deletion with metadata removal
- Unconfigured private storage fail-closed behavior
"""
from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.tenant_context import TenantContext
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.modules.authentication.api import get_authentication_repository
from app.modules.authentication.models import AccountCredential, AuthSession
from app.modules.authentication.password import hash_password
from app.modules.authentication.repository import SQLAlchemyAuthenticationRepository
from app.modules.authorization.api import get_authorization_repository
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.repository import SQLAlchemyAuthorizationRepository
from app.modules.identity.models import TenantMembership, User
from app.modules.identity.repository import (
    SQLAlchemyTenantMembershipRepository,
    get_tenant_membership_lookup,
)
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.registration_photos.models import StoredObject
from app.modules.registration_photos.storage import (
    MAX_IMAGE_BYTES,
    CloudflareR2Storage,
    InvalidImage,
    MockedR2Storage,
    ObjectStorageUnavailable,
    UnconfiguredPrivateObjectStorage,
    build_object_key,
    check_magic_signature,
    get_storage_provider,
    validate_image,
)
from app.modules.tenants.dependencies import get_tenant_context
from app.modules.tenants.models import Tenant


def create_test_image(format: str = "JPEG", size: tuple[int, int] = (64, 64)) -> bytes:
    buf = BytesIO()
    img = Image.new("RGB", size, color=(45, 85, 125))
    img.save(buf, format=format)
    return buf.getvalue()


@pytest.fixture
def r2_harness(tmp_path):
    database_path = tmp_path / "r2-stage2-test.sqlite"
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
    super_admin_id = uuid4()
    admin_a_id = uuid4()
    admin_b_id = uuid4()

    super_token = "test-super-token-r2"
    admin_a_token = "test-admin-a-token-r2"
    admin_b_token = "test-admin-b-token-r2"

    super_digest = __import__("hashlib").sha256(super_token.encode("utf-8")).hexdigest()
    super_csrf = __import__("hashlib").sha256(b"super-csrf-r2-stage2").hexdigest()
    admin_a_digest = __import__("hashlib").sha256(admin_a_token.encode("utf-8")).hexdigest()
    admin_a_csrf = __import__("hashlib").sha256(b"admin-a-csrf-r2").hexdigest()
    admin_b_digest = __import__("hashlib").sha256(admin_b_token.encode("utf-8")).hexdigest()
    admin_b_csrf = __import__("hashlib").sha256(b"admin-b-csrf-r2").hexdigest()
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    delta_days = __import__("datetime").timedelta(days=1)
    delta_hours = __import__("datetime").timedelta(hours=2)

    with factory.begin() as session:
        # Tenants
        ta = Tenant(id=tenant_a_id, slug="community-alpha", status="active")
        tb = Tenant(id=tenant_b_id, slug="community-beta", status="active")
        session.add_all([ta, tb])
        session.flush()

        # Platform Roles
        sr = PlatformRole(id=uuid4(), code="super_admin", display_name="Super Administrator", status="active")
        session.add(sr)

        # Super Admin Operator & Session
        root_op = PlatformOperator(id=uuid4(), issuer="local", subject="system:root", status="active")
        op = PlatformOperator(id=super_admin_id, issuer="local", subject="super_admin", status="active")
        session.add_all([root_op, op])
        session.flush()
        session.add(
            PlatformOperatorRoleAssignment(
                id=uuid4(),
                operator_id=super_admin_id,
                role_id=sr.id,
                assigned_by_operator_id=root_op.id,
                status="active",
                valid_from=now - delta_days,
            )
        )
        session.add(
            PlatformOperatorSession(
                id=uuid4(),
                operator_id=super_admin_id,
                token_digest=super_digest,
                csrf_token=super_csrf,
                authenticated_at=now,
                created_at=now,
                expires_at=now + delta_hours,
            )
        )
        session.add(
            AccountCredential(
                id=uuid4(),
                operator_id=super_admin_id,
                username="super_admin",
                password_hash=hash_password("SuperAdmin@123"),
                status="active",
                account_type="platform_operator",
            )
        )

        from app.modules.authorization.seeding import seed_tenant_role_permissions
        role_a, _ = seed_tenant_role_permissions(session, tenant_a_id)
        role_b, _ = seed_tenant_role_permissions(session, tenant_b_id)

        # Community A Admin
        ua = User(id=admin_a_id, status="active")
        session.add(ua)
        session.flush()
        ma = TenantMembership(id=uuid4(), tenant_id=tenant_a_id, user_id=admin_a_id, mobile_e164="+919876543210", status="active")
        session.add(ma)
        session.flush()

        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=ma.id,
                role_id=role_a.id,
                assigned_by_membership_id=ma.id,
                status="active",
                valid_from=now - delta_days,
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_a_id,
                membership_id=ma.id,
                token_digest=admin_a_digest,
                csrf_token=admin_a_csrf,
                created_at=now,
                expires_at=now + delta_hours,
            )
        )
        session.add(
            AccountCredential(
                id=uuid4(),
                user_id=admin_a_id,
                username="admin_alpha",
                password_hash=hash_password("AdminAlpha@123"),
                status="active",
                account_type="tenant_user",
            )
        )

        # Community B Admin
        ub = User(id=admin_b_id, status="active")
        session.add(ub)
        session.flush()
        mb = TenantMembership(id=uuid4(), tenant_id=tenant_b_id, user_id=admin_b_id, mobile_e164="+919876543211", status="active")
        session.add(mb)
        session.flush()

        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=mb.id,
                role_id=role_b.id,
                assigned_by_membership_id=mb.id,
                status="active",
                valid_from=now - delta_days,
            )
        )
        session.add(
            AuthSession(
                id=uuid4(),
                tenant_id=tenant_b_id,
                membership_id=mb.id,
                token_digest=admin_b_digest,
                csrf_token=admin_b_csrf,
                created_at=now,
                expires_at=now + delta_hours,
            )
        )
        session.add(
            AccountCredential(
                id=uuid4(),
                user_id=admin_b_id,
                username="admin_beta",
                password_hash=hash_password("AdminBeta@123"),
                status="active",
                account_type="tenant_user",
            )
        )

    mock_storage = MockedR2Storage(bucket_name="avanflix-r2-test-bucket")

    test_settings = Settings(
        _env_file=None,
        environment="test",
        database_url=None,
        frontend_origins="http://localhost:3000",
        r2_bucket_name="avanflix-r2-test-bucket",
    )

    app = create_app(test_settings)
    app.state.mock_storage = mock_storage

    auth_repo = SQLAlchemyAuthenticationRepository(factory)
    authz_repo = SQLAlchemyAuthorizationRepository(factory)
    tenant_lookup = SQLAlchemyTenantMembershipRepository(factory)

    app.dependency_overrides[get_authentication_repository] = lambda: auth_repo
    app.dependency_overrides[get_authorization_repository] = lambda: authz_repo
    app.dependency_overrides[get_tenant_membership_lookup] = lambda: tenant_lookup

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
        "super_token": super_token,
        "admin_a_token": admin_a_token,
        "admin_b_token": admin_b_token,
        "admin_a_id": admin_a_id,
        "admin_b_id": admin_b_id,
    }


def client_for(app):
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:3000"},
    )


# ---------------------------------------------------------------------------
# Test 1: Magic bytes, file signatures, and payload bounds
# ---------------------------------------------------------------------------
def test_magic_signature_and_image_validation():
    # 1. Valid JPEG
    jpeg_bytes = create_test_image("JPEG")
    assert check_magic_signature(jpeg_bytes) == "JPEG"
    v_jpeg = validate_image(jpeg_bytes)
    assert v_jpeg.image_format == "JPEG"
    assert v_jpeg.content_type == "image/jpeg"

    # 2. Valid PNG
    png_bytes = create_test_image("PNG")
    assert check_magic_signature(png_bytes) == "PNG"
    v_png = validate_image(png_bytes)
    assert v_png.image_format == "PNG"
    assert v_png.content_type == "image/png"

    # 3. Valid WebP
    webp_bytes = create_test_image("WEBP")
    assert check_magic_signature(webp_bytes) == "WEBP"
    v_webp = validate_image(webp_bytes)
    assert v_webp.image_format == "WEBP"
    assert v_webp.content_type == "image/webp"

    # 4. Disguised executable / PE binary (MZ header)
    pe_header = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00" + b"\x00" * 100
    with pytest.raises(InvalidImage, match="signature does not match"):
        check_magic_signature(pe_header)
    with pytest.raises(InvalidImage):
        validate_image(pe_header)

    # 5. HTML / SVG polyglot
    html_payload = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    with pytest.raises(InvalidImage, match="signature does not match"):
        check_magic_signature(html_payload)
    with pytest.raises(InvalidImage):
        validate_image(html_payload)

    # 6. Truncated payload (< 12 bytes)
    with pytest.raises(InvalidImage, match="too short"):
        check_magic_signature(b"\xff\xd8\xff")

    # 7. Oversize payload (> 5MB)
    with pytest.raises(InvalidImage, match="maximum permitted size"):
        validate_image(b"x" * (MAX_IMAGE_BYTES + 1))


# ---------------------------------------------------------------------------
# Test 2: R2 Provider resolution, Account ID derivation, and Fail-Closed behavior
# ---------------------------------------------------------------------------
def test_r2_provider_derivation_and_fail_closed():
    # Case 1: All credentials + explicit endpoint
    explicit_settings = SimpleNamespace(
        environment="production",
        r2_endpoint_url="https://custom-r2.example.com",
        r2_bucket_name="my-bucket",
        r2_access_key_id=SecretStr("mock-key-id"),
        r2_secret_access_key=SecretStr("mock-secret-key"),
        r2_account_id=None,
        r2_region="auto",
    )
    with patch("boto3.client") as mock_boto:
        provider = get_storage_provider(explicit_settings)
        assert isinstance(provider, CloudflareR2Storage)
        assert provider.bucket_name == "my-bucket"
        mock_boto.assert_called_once()
        _, kwargs = mock_boto.call_args
        assert kwargs["endpoint_url"] == "https://custom-r2.example.com"

    # Case 2: Endpoint derived from R2_ACCOUNT_ID
    account_id_settings = SimpleNamespace(
        environment="production",
        r2_account_id="0123456789abcdef0123456789abcdef",
        r2_bucket_name="my-bucket",
        r2_access_key_id=SecretStr("mock-key-id"),
        r2_secret_access_key=SecretStr("mock-secret-key"),
        r2_endpoint_url=None,
        r2_region="auto",
    )
    with patch("boto3.client") as mock_boto:
        provider = get_storage_provider(account_id_settings)
        assert isinstance(provider, CloudflareR2Storage)
        mock_boto.assert_called_once()
        _, kwargs = mock_boto.call_args
        assert kwargs["endpoint_url"] == "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"

    # Case 3: Missing credentials in production -> Fails closed with UnconfiguredPrivateObjectStorage
    unconfigured_prod = SimpleNamespace(
        environment="production",
        r2_bucket_name=None,
        r2_endpoint_url=None,
        r2_access_key_id=None,
        r2_secret_access_key=None,
        r2_account_id=None,
    )
    prod_provider = get_storage_provider(unconfigured_prod)
    assert isinstance(prod_provider, UnconfiguredPrivateObjectStorage)


# ---------------------------------------------------------------------------
# Test 3: Backend-generated random keys and path traversal resistance
# ---------------------------------------------------------------------------
def test_backend_controlled_keys():
    k1 = build_object_key("agent-photo", "jpg")
    k2 = build_object_key("agent-photo", "jpg")
    k3 = build_object_key("aadhaar-document", "png")

    assert k1.startswith("agent-photos/")
    assert k3.startswith("aadhaar-documents/")
    assert k1 != k2
    assert ".." not in k1
    assert "\\" not in k1
    assert len(k1) <= 64  # Conforms to PostgreSQL stored_objects.object_key String(64)


# ---------------------------------------------------------------------------
# Test 4: End-to-end Upload, PostgreSQL metadata persistence, and Preview stream
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_r2_upload_and_preview_stream(r2_harness):
    app = r2_harness["app"]
    factory = r2_harness["factory"]
    admin_a_token = r2_harness["admin_a_token"]
    tenant_a_id = r2_harness["tenant_a_id"]
    admin_a_id = r2_harness["admin_a_id"]

    img_bytes = create_test_image("JPEG")
    img_b64 = base64.b64encode(img_bytes).decode("ascii")

    async with client_for(app) as client:
        # 1. Upload via multipart form
        form_res = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("my-photo.jpg", img_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        assert form_res.status_code == 201
        form_data = form_res.json()
        photo_key = form_data["object_key"]
        assert photo_key.startswith("agent-photos/")
        assert form_data["content_type"] == "image/jpeg"
        assert form_data["file_size"] > 0

        # Verify metadata persisted in PostgreSQL
        with factory() as session:
            obj = session.execute(
                select(StoredObject).where(StoredObject.object_key == photo_key)
            ).scalar_one()
            assert obj.category == "agent-photo"
            assert obj.tenant_id == tenant_a_id
            assert obj.uploader_user_id == admin_a_id
            assert obj.content_type == "image/jpeg"

        # 2. Upload via JSON base64
        json_res = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            json={
                "category": "pan-document",
                "image_base64": f"data:image/jpeg;base64,{img_b64}",
            },
        )
        assert json_res.status_code == 201
        doc_key = json_res.json()["object_key"]
        assert doc_key.startswith("pan-documents/")

        # 3. Preview stream
        preview_res = await client.get(
            f"/api/v1/storage/{photo_key}/preview",
            cookies={"community_session": admin_a_token},
        )
        assert preview_res.status_code == 200
        assert preview_res.headers["content-type"] == "image/jpeg"
        assert preview_res.headers["cache-control"] == "private, no-store, max-age=0"
        assert len(preview_res.content) > 0


# ---------------------------------------------------------------------------
# Test 5: Cross-tenant isolation and Super Admin global authorization
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_r2_cross_tenant_isolation_and_rbac(r2_harness):
    app = r2_harness["app"]
    admin_a_token = r2_harness["admin_a_token"]
    admin_b_token = r2_harness["admin_b_token"]
    super_token = r2_harness["super_token"]

    img_bytes = create_test_image("PNG")

    async with client_for(app) as client:
        # Admin A uploads photo to Community Alpha
        res = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("alpha.png", img_bytes, "image/png")},
            data={"category": "user-photo"},
        )
        assert res.status_code == 201
        key = res.json()["object_key"]

        # Admin A accesses own object -> Allowed (200)
        a_view = await client.get(
            f"/api/v1/storage/{key}/preview",
            cookies={"community_session": admin_a_token},
        )
        assert a_view.status_code == 200

        # Admin B (Community Beta) attempts to view Admin A's object -> FORBIDDEN (403)
        b_view = await client.get(
            f"/api/v1/storage/{key}/preview",
            cookies={"community_session": admin_b_token},
        )
        assert b_view.status_code == 403

        # Admin B attempts to get presigned URL for Admin A's object -> FORBIDDEN (403)
        b_presign = await client.get(
            f"/api/v1/storage/{key}",
            cookies={"community_session": admin_b_token},
        )
        assert b_presign.status_code == 403

        # Super Admin accesses Community Alpha's object -> Allowed (200)
        super_view = await client.get(
            f"/api/v1/storage/{key}/preview",
            cookies={"community_platform_session": super_token},
        )
        assert super_view.status_code == 200


# ---------------------------------------------------------------------------
# Test 6: Presigned URL generation, bounds, and expiration
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_r2_presigned_url_expiration_bounds(r2_harness):
    app = r2_harness["app"]
    admin_a_token = r2_harness["admin_a_token"]
    img_bytes = create_test_image("JPEG")

    async with client_for(app) as client:
        res = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("doc.jpg", img_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        key = res.json()["object_key"]

        # Default 300s
        p1 = await client.get(
            f"/api/v1/storage/{key}",
            cookies={"community_session": admin_a_token},
        )
        assert p1.status_code == 200
        assert p1.json()["expires_in_seconds"] == 300
        assert "presigned_url" in p1.json()

        # Custom valid expiry (600s)
        p2 = await client.get(
            f"/api/v1/storage/{key}?expires_in_seconds=600",
            cookies={"community_session": admin_a_token},
        )
        assert p2.status_code == 200
        assert p2.json()["expires_in_seconds"] == 600

        # Too short (< 60s) -> Rejected 422
        bad_short = await client.get(
            f"/api/v1/storage/{key}?expires_in_seconds=30",
            cookies={"community_session": admin_a_token},
        )
        assert bad_short.status_code == 422

        # Too long (> 900s) -> Rejected 422
        bad_long = await client.get(
            f"/api/v1/storage/{key}?expires_in_seconds=1800",
            cookies={"community_session": admin_a_token},
        )
        assert bad_long.status_code == 422


# ---------------------------------------------------------------------------
# Test 7: Object deletion authorization and metadata removal
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_r2_object_deletion(r2_harness):
    app = r2_harness["app"]
    factory = r2_harness["factory"]
    admin_a_token = r2_harness["admin_a_token"]
    admin_b_token = r2_harness["admin_b_token"]
    super_token = r2_harness["super_token"]

    img_bytes = create_test_image("JPEG")

    async with client_for(app) as client:
        res = await client.post(
            "/api/v1/storage/upload",
            cookies={"community_session": admin_a_token},
            files={"file": ("del.jpg", img_bytes, "image/jpeg")},
            data={"category": "agent-photo"},
        )
        key = res.json()["object_key"]

        # 1. Admin B cannot delete Admin A's object -> 403
        b_del = await client.delete(
            f"/api/v1/storage/{key}",
            cookies={"community_session": admin_b_token},
        )
        assert b_del.status_code == 403

        # 2. Admin A (owner/tenant admin) deletes object -> 200
        a_del = await client.delete(
            f"/api/v1/storage/{key}",
            cookies={"community_session": admin_a_token},
        )
        assert a_del.status_code == 200
        assert a_del.json()["status"] == "deleted"

        # 3. Subsequent preview returns 404
        preview_after = await client.get(
            f"/api/v1/storage/{key}/preview",
            cookies={"community_session": admin_a_token},
        )
        assert preview_after.status_code == 404

        # 4. Verified removed from PostgreSQL
        with factory() as session:
            count = session.execute(
                select(StoredObject).where(StoredObject.object_key == key)
            ).scalars().all()
            assert len(count) == 0


# ---------------------------------------------------------------------------
# Test 8: Transactional cleanup on metadata persistence failure
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_r2_upload_transactional_cleanup_on_db_error(r2_harness):
    app = r2_harness["app"]
    admin_a_token = r2_harness["admin_a_token"]
    storage = r2_harness["storage"]
    img_bytes = create_test_image("JPEG")

    async with client_for(app) as client:
        # Simulate database failure during commit (after upload to R2)
        with patch("sqlalchemy.orm.Session.commit", side_effect=Exception("Simulated commit failure")):
            res = await client.post(
                "/api/v1/storage/upload",
                cookies={"community_session": admin_a_token},
                files={"file": ("fail.jpg", img_bytes, "image/jpeg")},
                data={"category": "agent-photo"},
            )
            assert res.status_code == 500

        # Assert no orphaned objects in storage
        assert len(storage._store) == 0

