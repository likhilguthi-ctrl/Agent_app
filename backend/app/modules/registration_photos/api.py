from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.error_responses import error_response
from app.core.request_id import request_id_context
from app.db.session import get_db
from app.modules.authentication.api import (
    get_authentication_repository,
    get_current_resolved_session,
)
from app.modules.authentication.models import OtpChallenge
from app.modules.authentication.repository import (
    ResolvedSession,
    SQLAlchemyAuthenticationRepository,
)
from app.modules.registration_photos.models import StoredObject
from app.modules.registration_photos.schemas import (
    StoragePresignedUrlResponse,
    StorageUploadResponse,
)
from app.modules.registration_photos.storage import (
    InvalidImage,
    ObjectStorageUnavailable,
    build_object_key,
    get_storage_provider,
    validate_image,
)

router = APIRouter(prefix="/registration-photos", tags=["registration photos"])
storage_router = APIRouter(prefix="/storage", tags=["storage"])

ALLOWED_CATEGORIES = {
    "agent-photo",
    "aadhaar-document",
    "pan-document",
    "user-photo",
}

MAX_REGISTRATION_IMAGE_BYTES = 500 * 1024  # 500 KB hard limit for registration photos

REGISTRATION_PURPOSE_CATEGORIES = {
    "agent_registration": {"agent-photo", "aadhaar-document", "pan-document"},
    "user_registration": {"user-photo"},
    "admin_registration": {"agent-photo", "aadhaar-document", "pan-document"},
}


@router.post(
    "",
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    summary="Upload a registration photo (fail-closed legacy boundary)",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "No trusted registration transaction is available to own this photo."
        }
    },
)
async def upload_registration_photo(_: Request) -> JSONResponse:
    """Reject untrusted standalone photo uploads."""
    return error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "REGISTRATION_PHOTO_ASSOCIATION_UNAVAILABLE",
        "Photo upload is unavailable until registration can be securely associated.",
        request_id_context.get(),
    )


class Base64UploadRequest(BaseModel):
    category: str = Field(pattern="^(agent-photo|aadhaar-document|pan-document|user-photo)$")
    image_base64: str = Field(min_length=1)
    verification_token: str | None = None


@dataclass(frozen=True)
class UploadAuthorization:
    tenant_id: UUID
    uploader_user_id: UUID | None
    auth_type: str  # "session" | "registration_token"
    role: str | None
    allowed_categories: set[str]
    challenge: OtpChallenge | None = None


def resolve_upload_authorization(
    request: Request,
    db: Session,
    auth_repo: SQLAlchemyAuthenticationRepository,
    token_candidate: str | None = None,
) -> UploadAuthorization:
    """Resolves authorized identity for storage uploads.

    Supports:
    1. Authenticated session cookies (community_session, community_platform_session)
       or Authorization: Bearer <session_token> header.
    2. Short-lived single-use registration verification token via
       X-Registration-Verification-Token header, Bearer token, or request payload.
    """
    settings: Settings = request.app.state.settings

    # 1. Check for authenticated session token (Cookie or Bearer)
    raw_token = request.cookies.get(settings.auth_session_cookie_name)
    if not raw_token:
        raw_token = request.cookies.get(settings.platform_oidc_session_cookie_name)
    if not raw_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            raw_token = auth_header[7:].strip()

    if raw_token and len(raw_token) <= 128:
        resolved = auth_repo.resolve_session(raw_token)
        if resolved is not None and resolved.user_status == "active":
            if resolved.account_type != "tenant_user" or (
                resolved.tenant_status == "active" and resolved.membership_status == "active"
            ):
                allowed = ALLOWED_CATEGORIES
                if resolved.role in {"AGENT", "CHILD_AGENT"}:
                    allowed = {"user-photo", "agent-photo"}
                elif resolved.role == "MEMBER":
                    allowed = {"user-photo"}
                return UploadAuthorization(
                    tenant_id=resolved.tenant_id,
                    uploader_user_id=resolved.user_id,
                    auth_type="session",
                    role=resolved.role,
                    allowed_categories=allowed,
                )

    # 2. Check for registration verification token
    verification_token = token_candidate or request.headers.get("X-Registration-Verification-Token")
    if not verification_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            possible_reg = auth_header[7:].strip()
            if possible_reg and len(possible_reg) <= 128:
                verification_token = possible_reg

    if verification_token:
        clean_token = verification_token.strip()
        stmt = select(OtpChallenge).where(OtpChallenge.verification_token == clean_token)
        challenge = db.execute(stmt).scalar_one_or_none()

        if challenge is None or challenge.consumed_at is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "REGISTRATION_VERIFICATION_INVALID",
                    "message": "Registration verification token is invalid or has already been consumed.",
                },
            )

        now = datetime.now(UTC)
        verified_at = challenge.verified_at
        if verified_at is not None and verified_at.tzinfo is None:
            verified_at = verified_at.replace(tzinfo=UTC)

        if verified_at is None or (now - verified_at) > timedelta(minutes=15):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "REGISTRATION_VERIFICATION_EXPIRED",
                    "message": "Registration verification token has expired. Please verify your mobile number again.",
                },
            )

        if challenge.purpose not in REGISTRATION_PURPOSE_CATEGORIES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "REGISTRATION_VERIFICATION_UNAUTHORIZED",
                    "message": f"Verification token purpose '{challenge.purpose}' is not authorized for photo uploads.",
                },
            )

        tenant_id = challenge.tenant_id
        if tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "TENANT_REQUIRED",
                    "message": "Challenge is not bound to a valid community.",
                },
            )

        meta = dict(challenge.metadata_json or {})
        upload_count = meta.get("registration_photo_upload_count", 0)
        if upload_count >= 10:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "UPLOAD_LIMIT_EXCEEDED",
                    "message": "Maximum number of photo uploads exceeded for this registration token.",
                },
            )

        return UploadAuthorization(
            tenant_id=tenant_id,
            uploader_user_id=None,
            auth_type="registration_token",
            role=None,
            allowed_categories=REGISTRATION_PURPOSE_CATEGORIES[challenge.purpose],
            challenge=challenge,
        )

    # 3. Neither session nor registration verification token provided
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": "AUTHENTICATION_REQUIRED",
            "message": "Authentication is required.",
        },
    )


def resolve_preview_authorization(
    request: Request,
    db: Session,
    stored: StoredObject,
    auth_repo: SQLAlchemyAuthenticationRepository,
) -> None:
    settings: Settings = request.app.state.settings

    # Check for session
    raw_token = request.cookies.get(settings.auth_session_cookie_name)
    if not raw_token:
        raw_token = request.cookies.get(settings.platform_oidc_session_cookie_name)
    if not raw_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            raw_token = auth_header[7:].strip()

    if raw_token and len(raw_token) <= 128:
        resolved = auth_repo.resolve_session(raw_token)
        if resolved is not None and resolved.user_status == "active":
            if resolved.role == "SUPER_ADMIN":
                return
            if stored.tenant_id is not None and stored.tenant_id != resolved.tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": "FORBIDDEN", "message": "Access to objects outside your community is denied."},
                )
            return

    # Check for registration verification token (header or query param)
    reg_token = (
        request.headers.get("X-Registration-Verification-Token")
        or request.query_params.get("verification_token")
    )
    if reg_token:
        clean_reg = reg_token.strip()
        stmt = select(OtpChallenge).where(OtpChallenge.verification_token == clean_reg)
        challenge = db.execute(stmt).scalar_one_or_none()
        if challenge is not None and challenge.consumed_at is None:
            if stored.tenant_id is not None and challenge.tenant_id != stored.tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": "FORBIDDEN", "message": "Access to objects outside your community is denied."},
                )
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "AUTHENTICATION_REQUIRED", "message": "Authentication is required."},
    )


@storage_router.post(
    "/upload",
    response_model=StorageUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a validated private registration image to Cloudflare R2",
)
async def upload_image(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    auth_repo: Annotated[SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)],
) -> StorageUploadResponse:
    content_type_header = request.headers.get("content-type", "").lower()
    content: bytes
    category: str
    token_candidate: str | None = None

    if "multipart/form-data" in content_type_header:
        form = await request.form()
        uploaded_file = form.get("file")
        category_val = form.get("category")
        token_candidate = form.get("verification_token")
        if token_candidate is not None:
            token_candidate = str(token_candidate)

        if not uploaded_file or not hasattr(uploaded_file, "read"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "FILE_REQUIRED", "message": "A valid image file must be uploaded."},
            )
        category = str(category_val) if category_val else ""
        content = await uploaded_file.read()
    elif "application/json" in content_type_header:
        try:
            body_json = await request.json()
            upload_req = Base64UploadRequest(**body_json)
            category = upload_req.category
            token_candidate = upload_req.verification_token
            raw_b64 = upload_req.image_base64
            if "," in raw_b64:
                raw_b64 = raw_b64.split(",", 1)[1]
            content = base64.b64decode(raw_b64)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_JSON_PAYLOAD", "message": f"Invalid upload payload: {exc}"},
            ) from exc
    else:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"error": "UNSUPPORTED_MEDIA_TYPE", "message": "Expected multipart/form-data or application/json."},
        )

    # 1. Authorize upload via session or registration verification token
    auth = resolve_upload_authorization(
        request,
        db,
        auth_repo=auth_repo,
        token_candidate=token_candidate,
    )

    # 2. Validate category
    if category not in ALLOWED_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "INVALID_CATEGORY",
                "message": f"Category must be one of: {sorted(ALLOWED_CATEGORIES)}.",
            },
        )

    if category not in auth.allowed_categories:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "CATEGORY_NOT_PERMITTED",
                "message": f"Uploading category '{category}' is not permitted for this authorization context.",
            },
        )

    # 3. Enforce maximum upload size limit
    if len(content) > MAX_REGISTRATION_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "IMAGE_TOO_LARGE",
                "message": f"Uploaded image exceeds the maximum allowed size of 500 KB ({len(content)} bytes). Please compress before uploading.",
            },
        )

    # 4. Content & magic-bytes validation
    try:
        validated = validate_image(content)
    except InvalidImage as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_IMAGE", "message": str(exc)},
        ) from exc

    ext = "jpg" if validated.image_format == "JPEG" else validated.image_format.lower()
    object_key = build_object_key(category, ext)

    settings: Settings = request.app.state.settings
    storage = get_storage_provider(settings)

    try:
        await storage.put_private(
            object_key=object_key,
            content=validated.content,
            content_type=validated.content_type,
        )
    except ObjectStorageUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "STORAGE_UNAVAILABLE", "message": "Storage service is temporarily unavailable."},
        ) from exc

    try:
        now = datetime.now(UTC)
        bucket_name = getattr(storage, "bucket_name", "community-ott-private")
        stored = StoredObject(
            id=uuid4(),
            object_key=object_key,
            bucket_name=bucket_name,
            category=category,
            content_type=validated.content_type,
            file_size=len(validated.content),
            width=validated.width,
            height=validated.height,
            tenant_id=auth.tenant_id,
            uploader_user_id=auth.uploader_user_id,
            created_at=now,
        )
        db.add(stored)

        # If upload was authorized via registration challenge, record metadata
        if auth.challenge is not None:
            meta = dict(auth.challenge.metadata_json or {})
            uploaded_keys = list(meta.get("registration_photo_keys", []))
            uploaded_keys.append(object_key)
            meta["registration_photo_keys"] = uploaded_keys
            meta["registration_photo_upload_count"] = meta.get("registration_photo_upload_count", 0) + 1
            auth.challenge.metadata_json = meta

        db.commit()
    except Exception as exc:
        db.rollback()
        # Clean up uploaded bytes in R2 to maintain consistency and prevent orphaned storage
        try:
            await storage.delete_private(object_key=object_key)
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "STORAGE_METADATA_PERSISTENCE_FAILED", "message": "Failed to persist object metadata."},
        ) from exc

    return StorageUploadResponse(
        object_key=stored.object_key,
        category=stored.category,
        content_type=stored.content_type,
        file_size=stored.file_size,
        width=stored.width,
        height=stored.height,
        created_at=stored.created_at,
    )


@storage_router.get(
    "/{object_key:path}/preview",
    summary="Direct authenticated image preview stream",
)
async def preview_image(
    object_key: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    auth_repo: Annotated[SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)],
) -> Response:
    stored = db.execute(
        select(StoredObject).where(StoredObject.object_key == object_key)
    ).scalar_one_or_none()
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "OBJECT_NOT_FOUND", "message": "Object not found."},
        )

    # Enforce community/tenant isolation via session or registration verification
    resolve_preview_authorization(request, db, stored, auth_repo=auth_repo)

    settings: Settings = request.app.state.settings
    storage = get_storage_provider(settings)

    try:
        raw_bytes = await storage.get_private(object_key=object_key)
    except ObjectStorageUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "STORAGE_UNAVAILABLE", "message": "Storage service is temporarily unavailable."},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "OBJECT_READ_ERROR", "message": f"Could not read object: {exc}"},
        ) from exc

    return Response(
        content=raw_bytes,
        media_type=stored.content_type,
        headers={"Cache-Control": "private, no-store, max-age=0"},
    )


@storage_router.get(
    "/{object_key:path}",
    response_model=StoragePresignedUrlResponse,
    summary="Retrieve an authorized short-lived presigned URL for a private object",
)
async def get_presigned_url(
    object_key: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    auth_repo: Annotated[SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)],
    expires_in_seconds: Annotated[int, Query(ge=60, le=900)] = 300,
) -> StoragePresignedUrlResponse:
    stored = db.execute(
        select(StoredObject).where(StoredObject.object_key == object_key)
    ).scalar_one_or_none()
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "OBJECT_NOT_FOUND", "message": "Object not found."},
        )

    # Enforce community/tenant isolation
    resolve_preview_authorization(request, db, stored, auth_repo=auth_repo)

    settings: Settings = request.app.state.settings
    storage = get_storage_provider(settings)

    try:
        presigned = await storage.generate_presigned_url(
            object_key=object_key,
            expires_in_seconds=expires_in_seconds,
        )
    except ObjectStorageUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "STORAGE_UNAVAILABLE", "message": "Storage service is temporarily unavailable."},
        ) from exc

    return StoragePresignedUrlResponse(
        object_key=object_key,
        presigned_url=presigned,
        expires_in_seconds=expires_in_seconds,
    )


@storage_router.delete(
    "/{object_key:path}",
    status_code=status.HTTP_200_OK,
    summary="Delete a stored object from Cloudflare R2 and remove metadata",
)
async def delete_image(
    object_key: str,
    request: Request,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    resolved, _ = session_data

    stored = db.execute(
        select(StoredObject).where(StoredObject.object_key == object_key)
    ).scalar_one_or_none()
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "OBJECT_NOT_FOUND", "message": "Object not found."},
        )

    # Enforce community/tenant & owner authorization:
    # 1. SUPER_ADMIN can delete any object
    # 2. Tenant ADMIN can delete objects in their own community
    # 3. Object uploader can delete their own object
    is_super_admin = resolved.role == "SUPER_ADMIN"
    is_tenant_admin = resolved.role == "ADMIN" and stored.tenant_id == resolved.tenant_id
    is_owner = stored.uploader_user_id == resolved.user_id

    if not (is_super_admin or is_tenant_admin or is_owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Access to delete objects outside your authorization is denied."},
        )

    settings: Settings = request.app.state.settings
    storage = get_storage_provider(settings)

    try:
        await storage.delete_private(object_key=object_key)
    except ObjectStorageUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "STORAGE_UNAVAILABLE", "message": "Storage service is temporarily unavailable."},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "OBJECT_DELETE_ERROR", "message": f"Could not delete object: {exc}"},
        ) from exc

    db.delete(stored)
    db.commit()

    return {"status": "deleted", "object_key": object_key}
