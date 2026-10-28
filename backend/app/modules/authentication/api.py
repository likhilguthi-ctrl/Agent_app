from __future__ import annotations

import ipaddress
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.tenant_context import TenantContext
from app.db.session import get_db
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.authentication.abuse_control import OtpRateLimited, SQLAlchemyOtpAbuseGuard
from app.modules.authentication.errors import TenantMembershipRequired
from app.modules.authentication.models import AccountCredential, AuthSession, OtpChallenge
from app.modules.authentication.otp_login import LoginRejected
from app.modules.authentication.otp_service import (
    OtpRateLimitExceeded,
    OtpService,
    OtpVerificationFailed,
)
from app.modules.authentication.password import hash_password
from app.modules.authentication.repository import (
    AuthenticatedSession,
    ResolvedSession,
    SQLAlchemyAuthenticationRepository,
)
from app.modules.authentication.service import (
    AuthenticationConfigurationUnavailable,
    OtpAuthenticationService,
)
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.authorization.service import AuthorizationService
from app.modules.identity.models import TenantMembership, User
from app.modules.identity.repository import get_tenant_membership_lookup
from app.modules.messaging.errors import MessagingError, MessagingProviderUnavailable
from app.modules.messaging.notification_service import NotificationServiceClient
from app.modules.messaging.service import MessagingService, normalize_mobile_e164
from app.modules.platform_identity.models import PlatformOperator, PlatformOperatorSession
from app.modules.relationships.models import MemberRelationship, RelationshipStatus, RelationshipType
from app.modules.tenants.dependencies import get_tenant_context
from app.modules.tenants.models import Tenant
from sqlalchemy import select, update, or_
from sqlalchemy.orm import Session

router = APIRouter(prefix="/auth", tags=["authentication"])


def require_local_development_otp(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not settings.development_static_otp_enabled:
        return
    client_host = request.client.host if request.client is not None else None
    try:
        client_is_loopback = client_host is not None and ipaddress.ip_address(
            client_host
        ).is_loopback
    except ValueError:
        client_is_loopback = False
    if not client_is_loopback or request.headers.get("origin") not in settings.frontend_origin_list:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def format_masked_mobile(mobile: str) -> str:
    clean = mobile.strip()
    if clean.startswith("+91") and len(clean) == 13:
        return f"+91 {clean[3:8]} ****{clean[-2:]}"
    if len(clean) >= 10:
        prefix = clean[:-6]
        suffix = clean[-2:]
        return f"{prefix} ****{suffix}"
    return clean


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)
    challenge_id: UUID | None = None
    otp: str | None = None
    two_factor: bool | None = None


class TenantSummary(BaseModel):
    id: UUID
    slug: str
    name: str | None = None


class LoginResponse(BaseModel):
    user_id: UUID
    username: str
    role: str
    status: str
    tenant_id: UUID | None = None
    membership_id: UUID | None = None
    tenant: TenantSummary | None = None
    csrf_token: str
    expires_at: datetime
    must_change_password: bool = False
    context: dict[str, Any] = Field(default_factory=dict)


class OtpRequiredResponse(BaseModel):
    status: str = "otp_required"
    challenge_id: UUID
    masked_mobile: str
    cooldown_seconds: int = 60


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class PasswordChangeResponse(BaseModel):
    status: str = "success"
    message: str = "Password changed successfully."


class OtpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mobile_number: str = Field(min_length=9, max_length=32)
    purpose: str = Field(
        default="login",
        pattern="^(login|admin_registration|agent_registration|user_registration|whatsapp_verification|password_reset)$",
    )
    community_id: UUID | None = None


class OtpVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mobile_number: str = Field(min_length=9, max_length=32)
    otp: str = Field(min_length=1, max_length=128)
    challenge_id: UUID
    purpose: str = Field(
        default="login",
        pattern="^(login|admin_registration|agent_registration|user_registration|whatsapp_verification|password_reset)$",
    )
    community_id: UUID | None = None


class PasswordResetOtpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mobile_number: str = Field(min_length=9, max_length=32)


class PasswordResetOtpResponse(BaseModel):
    status: str = "accepted"
    challenge_id: UUID | None = None
    masked_mobile: str
    cooldown_seconds: int = 60
    message: str = (
        "If an eligible account is associated with this mobile number, "
        "verification instructions have been dispatched."
    )


class PasswordResetVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mobile_number: str = Field(min_length=9, max_length=32)
    challenge_id: UUID
    otp: str = Field(min_length=1, max_length=128)


class PasswordResetAccountSummary(BaseModel):
    account_id: UUID
    username: str
    role: str
    account_type: str
    community_id: UUID | None = None
    community_name: str | None = None


class PasswordResetVerifyResponse(BaseModel):
    status: str = "verified"
    reset_token: str
    requires_account_selection: bool = False
    account: PasswordResetAccountSummary | None = None
    accounts: list[PasswordResetAccountSummary] = Field(default_factory=list)


class PasswordResetCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mobile_number: str = Field(min_length=9, max_length=32)
    reset_token: str = Field(min_length=16, max_length=128)
    new_password: str = Field(min_length=8, max_length=256)
    confirm_password: str = Field(min_length=8, max_length=256)
    account_id: UUID | None = None


class PasswordResetCompleteResponse(BaseModel):
    status: str = "success"
    message: str = (
        "Password has been reset successfully. You can now log in with your new password."
    )


class EmailOtpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=5, max_length=255)
    purpose: str = Field(default="email_verification", max_length=32)
    community_id: UUID | None = None


class EmailOtpRequestAccepted(BaseModel):
    status: str = "accepted"
    challenge_id: UUID
    cooldown_seconds: int = 60


class EmailOtpVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=5, max_length=255)
    otp: str = Field(min_length=4, max_length=12)
    challenge_id: UUID
    purpose: str = Field(default="email_verification", max_length=32)


class ContextSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    selection_token: str
    tenant_id: UUID | None = None
    role: str | None = None


class OtpRequestAccepted(BaseModel):
    status: str = "accepted"
    challenge_id: UUID
    cooldown_seconds: int = 60


class AuthenticatedIdentity(BaseModel):
    user_id: UUID
    username: str | None = None
    role: str = "MEMBER"
    status: str = "active"
    tenant_id: UUID | None = None
    membership_id: UUID | None = None
    csrf_token: str
    expires_at: datetime
    must_change_password: bool = False
    tenant: TenantSummary | None = None
    context: dict[str, Any] | None = None


class AuthorizationContextCommunity(BaseModel):
    id: UUID
    slug: str


class AuthorizationContextPrincipal(BaseModel):
    type: Literal["tenant_member"]
    id: UUID
    membership_id: UUID


class AuthorizationContextAuthentication(BaseModel):
    status: Literal["authenticated"]
    session_expires_at: datetime


class AuthorizationContextScope(BaseModel):
    status: Literal["resolved"]
    resolution: Literal["current_trusted_tenant"]
    communities: list[AuthorizationContextCommunity]


class AuthorizationContextAuthorization(BaseModel):
    status: Literal["resolved"]
    effective_permissions: list[str]
    community_scope: AuthorizationContextScope


class AuthorizationContextUnavailable(BaseModel):
    status: Literal["unavailable"] = "unavailable"
    reason_code: str


class TrustedAuthorizationContext(BaseModel):
    """Supported tenant-member context; employee and product roles are not inferred."""

    authentication: AuthorizationContextAuthentication
    principal: AuthorizationContextPrincipal
    authorization: AuthorizationContextAuthorization
    employee_authority: AuthorizationContextUnavailable
    product_role: AuthorizationContextUnavailable


def get_authentication_repository() -> SQLAlchemyAuthenticationRepository:
    return SQLAlchemyAuthenticationRepository()


def get_authorization_context_service(
    repository: Annotated[
        SQLAlchemyAuthorizationRepository, Depends(get_authorization_repository)
    ],
) -> AuthorizationService:
    return AuthorizationService(repository)


def get_otp_abuse_guard(request: Request) -> SQLAlchemyOtpAbuseGuard:
    return SQLAlchemyOtpAbuseGuard(request.app.state.settings)


def get_otp_authentication_service(
    request: Request,
    memberships=Depends(get_tenant_membership_lookup),
    repository: SQLAlchemyAuthenticationRepository = Depends(get_authentication_repository),
    abuse_guard: SQLAlchemyOtpAbuseGuard = Depends(get_otp_abuse_guard),
) -> OtpAuthenticationService:
    settings: Settings = request.app.state.settings
    messaging: MessagingService = request.app.state.messaging_service
    return OtpAuthenticationService(
        settings=settings,
        messaging=messaging,
        memberships=memberships,
        abuse_guard=abuse_guard,
        repository=repository,
    )


def get_current_resolved_session(
    request: Request,
    repository: Annotated[
        SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)
    ],
) -> tuple[ResolvedSession, str]:
    settings: Settings = request.app.state.settings
    raw_token = request.cookies.get(settings.auth_session_cookie_name)
    if not raw_token:
        raw_token = request.cookies.get(settings.platform_oidc_session_cookie_name)
    if not raw_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            raw_token = auth_header[7:].strip()
    if not raw_token or len(raw_token) > 128:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    resolved = repository.resolve_session(raw_token)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if resolved.user_status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if resolved.account_type == "tenant_user":
        if resolved.tenant_status != "active" or resolved.membership_status != "active":
            raise TenantMembershipRequired
    if resolved.must_change_password:
        allowed_paths = {
            "/api/v1/auth/me",
            "/api/v1/auth/password/change",
            "/api/v1/auth/logout",
        }
        if request.url.path not in allowed_paths:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "PASSWORD_CHANGE_REQUIRED",
                    "message": "Password change required before accessing resources.",
                },
            )
    return resolved, raw_token


def get_authenticated_session(
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_tenant_context)],
    repository: Annotated[
        SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)
    ],
) -> AuthenticatedSession:
    settings: Settings = request.app.state.settings
    raw_token = request.cookies.get(settings.auth_session_cookie_name)
    if not raw_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            raw_token = auth_header[7:].strip()
    if not raw_token or len(raw_token) > 128:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    authenticated = repository.find_session_for_tenant(
        tenant_id=tenant.tenant_id,
        raw_session_token=raw_token,
    )
    if authenticated is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if authenticated.user_status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if authenticated.tenant_status != "active" or authenticated.membership_status != "active":
        raise TenantMembershipRequired
    if authenticated.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "PASSWORD_CHANGE_REQUIRED",
                "message": "Password change required before accessing resources.",
            },
        )
    return authenticated


def get_otp_service(
    request: Request,
    abuse_guard: Annotated[SQLAlchemyOtpAbuseGuard, Depends(get_otp_abuse_guard)],
) -> OtpService:
    settings: Settings = request.app.state.settings
    test_factory = getattr(request.app.state, "test_factory", None)
    notification_client = getattr(request.app.state, "notification_client", None)
    if notification_client is None:
        notification_client = NotificationServiceClient(settings)
        request.app.state.notification_client = notification_client

    mock_codes = None
    messaging_service = getattr(request.app.state, "messaging_service", None)
    if messaging_service and hasattr(messaging_service, "_provider"):
        mock_codes = getattr(messaging_service._provider, "_accepted_otps", None) or getattr(messaging_service._provider, "_codes", None)
    if not mock_codes and hasattr(request.app.state, "test_provider"):
        mock_codes = getattr(request.app.state.test_provider, "_accepted_otps", None) or getattr(request.app.state.test_provider, "_codes", None)

    return OtpService(
        settings=settings,
        session_factory=test_factory,
        notification_client=notification_client,
        mock_codes=mock_codes,
        abuse_guard=abuse_guard,
        messaging_service=messaging_service,
    )


def get_email_otp_service(request: Request) -> Any:
    from app.modules.authentication.email_otp_service import EmailOtpService
    settings: Settings = request.app.state.settings
    notification_client = getattr(request.app.state, "notification_client", None)
    if notification_client is None:
        notification_client = NotificationServiceClient(settings)
        request.app.state.notification_client = notification_client
    return EmailOtpService(settings=settings, notification_client=notification_client)



@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    repository: Annotated[
        SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)
    ],
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
) -> Any:
    settings: Settings = request.app.state.settings

    # Step 2 / 3: Verify OTP and finalize session establishment
    if body.otp and body.challenge_id:
        cred_res = await run_in_threadpool(
            repository.verify_credentials_for_otp,
            username=body.username,
            password=body.password,
        )
        if cred_res is None or not cred_res.mobile_e164:
            raise LoginRejected()

        try:
            otp_service.verify_otp(
                challenge_id=body.challenge_id,
                mobile_number=cred_res.mobile_e164,
                otp=body.otp,
                purpose="login",
            )
        except OtpVerificationFailed as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "OTP_VERIFICATION_FAILED",
                    "message": str(exc),
                    "attempts_remaining": exc.attempts_remaining,
                },
            )

        raw_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_hex(32)
        result = await run_in_threadpool(
            repository.authenticate_with_credentials,
            username=body.username,
            password=body.password,
            raw_session_token=raw_token,
            csrf_token=csrf_token,
            session_ttl_seconds=settings.auth_session_ttl_seconds,
        )
        if result is None:
            raise LoginRejected()

        response.set_cookie(
            key=settings.auth_session_cookie_name,
            value=raw_token,
            max_age=settings.auth_session_ttl_seconds,
            expires=settings.auth_session_ttl_seconds,
            path=settings.auth_session_cookie_path,
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite=settings.auth_session_cookie_samesite,
        )
        if result.role == "SUPER_ADMIN":
            response.set_cookie(
                key=settings.platform_oidc_session_cookie_name,
                value=raw_token,
                max_age=settings.platform_oidc_session_ttl_seconds,
                expires=settings.platform_oidc_session_ttl_seconds,
                path=settings.platform_oidc_session_cookie_path,
                secure=settings.session_cookie_secure,
                httponly=True,
                samesite=settings.platform_oidc_session_cookie_samesite,
            )

        tenant_summary = None
        if result.tenant is not None:
            tenant_summary = TenantSummary(
                id=result.tenant["id"],
                slug=result.tenant["slug"],
            )

        return LoginResponse(
            user_id=result.user_id,
            username=result.username,
            role=result.role,
            status=result.status,
            tenant_id=result.tenant_id,
            membership_id=result.membership_id,
            tenant=tenant_summary,
            csrf_token=result.csrf_token,
            expires_at=result.expires_at,
            must_change_password=result.must_change_password,
            context=result.context or {},
        )

    # Step 1: Credentials check & OTP dispatch
    if body.two_factor is True or request.headers.get("X-2FA-Login") == "true" or body.challenge_id:
        cred_res = await run_in_threadpool(
            repository.verify_credentials_for_otp,
            username=body.username,
            password=body.password,
        )
        if cred_res is None or not cred_res.mobile_e164:
            raise LoginRejected()

        try:
            challenge_id, cooldown = await otp_service.request_otp(
                mobile_number=cred_res.mobile_e164,
                purpose="login",
                tenant_id=cred_res.tenant_id,
            )
            masked = format_masked_mobile(cred_res.mobile_e164)
            return OtpRequiredResponse(
                challenge_id=challenge_id,
                masked_mobile=masked,
                cooldown_seconds=cooldown,
            )
        except (OtpRateLimitExceeded, OtpRateLimited) as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": "RATE_LIMIT_EXCEEDED", "message": str(exc), "retry_after": exc.retry_after_seconds},
            )
        except MessagingProviderUnavailable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "MESSAGING_PROVIDER_UNAVAILABLE", "message": "Notification provider is temporarily unavailable."},
            )
        except MessagingError:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "MESSAGING_PROVIDER_ERROR", "message": "Notification delivery failed."},
            )

    # Default / legacy path: single-factor direct login (for backwards compatibility with existing test suites)
    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_hex(32)
    result = await run_in_threadpool(
        repository.authenticate_with_credentials,
        username=body.username,
        password=body.password,
        raw_session_token=raw_token,
        csrf_token=csrf_token,
        session_ttl_seconds=settings.auth_session_ttl_seconds,
    )
    if result is None:
        raise LoginRejected()

    response.set_cookie(
        key=settings.auth_session_cookie_name,
        value=raw_token,
        max_age=settings.auth_session_ttl_seconds,
        expires=settings.auth_session_ttl_seconds,
        path=settings.auth_session_cookie_path,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.auth_session_cookie_samesite,
    )
    if result.role == "SUPER_ADMIN":
        response.set_cookie(
            key=settings.platform_oidc_session_cookie_name,
            value=raw_token,
            max_age=settings.platform_oidc_session_ttl_seconds,
            expires=settings.platform_oidc_session_ttl_seconds,
            path=settings.platform_oidc_session_cookie_path,
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite=settings.platform_oidc_session_cookie_samesite,
        )

    tenant_summary = None
    if result.tenant is not None:
        tenant_summary = TenantSummary(
            id=result.tenant["id"],
            slug=result.tenant["slug"],
        )

    return LoginResponse(
        user_id=result.user_id,
        username=result.username,
        role=result.role,
        status=result.status,
        tenant_id=result.tenant_id,
        membership_id=result.membership_id,
        tenant=tenant_summary,
        csrf_token=result.csrf_token,
        expires_at=result.expires_at,
        must_change_password=result.must_change_password,
        context=result.context or {},
    )


@router.post("/password/change", response_model=PasswordChangeResponse)
async def change_password(
    body: PasswordChangeRequest,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    repository: Annotated[SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)],
) -> PasswordChangeResponse:
    resolved, _ = session_data
    if body.current_password == body.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "PASSWORD_UNCHANGED", "message": "New password cannot be the same as current password."},
        )
    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "PASSWORD_TOO_SHORT", "message": "New password must be at least 8 characters long."},
        )
    success = await run_in_threadpool(
        repository.change_password,
        user_id=resolved.user_id,
        account_type=resolved.account_type,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_PASSWORD", "message": "Current password is incorrect."},
        )
    return PasswordChangeResponse(status="success", message="Password changed successfully.")


def _find_accounts_by_mobile(session: Session, mobile_e164: str) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []

    # 1. Platform Operator accounts (Super Admin)
    ops = session.execute(
        select(PlatformOperator, AccountCredential)
        .join(
            AccountCredential,
            (AccountCredential.operator_id == PlatformOperator.id)
            & (AccountCredential.account_type == "platform_operator"),
        )
        .where(
            PlatformOperator.mobile_e164 == mobile_e164,
            PlatformOperator.status == "active",
            AccountCredential.status == "active",
        )
    ).all()

    for op, cred in ops:
        accounts.append({
            "account_id": cred.id,
            "credential_id": cred.id,
            "target_id": op.id,
            "operator_id": op.id,
            "user_id": None,
            "username": cred.username,
            "role": "SUPER_ADMIN",
            "account_type": "platform_operator",
            "community_id": None,
            "community_name": None,
        })

    # 2. Tenant User accounts (Admin / Manager, Agent, Child Agent, Subscriber / Member)
    memberships = session.execute(
        select(TenantMembership, User, AccountCredential, Tenant)
        .join(User, User.id == TenantMembership.user_id)
        .join(
            AccountCredential,
            (AccountCredential.user_id == User.id)
            & (AccountCredential.account_type == "tenant_user"),
        )
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .where(
            TenantMembership.mobile_e164 == mobile_e164,
            TenantMembership.status == "active",
            User.status == "active",
            AccountCredential.status == "active",
            Tenant.status == "active",
        )
    ).all()

    for tm, user, cred, tenant in memberships:
        # Determine role from UserRoleAssignment
        role_row = session.execute(
            select(Role.code)
            .join(UserRoleAssignment, UserRoleAssignment.role_id == Role.id)
            .where(
                UserRoleAssignment.tenant_id == tm.tenant_id,
                UserRoleAssignment.membership_id == tm.id,
                UserRoleAssignment.status == "active",
                Role.status == "active",
            )
        ).first()

        resolved_role = "MEMBER"
        if role_row is not None:
            raw_role = role_row[0].lower()
            if raw_role in {"admin", "tenant_admin", "community_admin"}:
                resolved_role = "ADMIN"
            elif raw_role in {"agent", "employee"}:
                child_rel = session.execute(
                    select(MemberRelationship.id).where(
                        MemberRelationship.tenant_id == tm.tenant_id,
                        MemberRelationship.child_member_id == tm.id,
                        MemberRelationship.relationship_type == RelationshipType.REPORTS_TO.value,
                        MemberRelationship.status == RelationshipStatus.ACTIVE.value,
                    ).limit(1)
                ).scalar_one_or_none()
                resolved_role = "CHILD_AGENT" if child_rel else "AGENT"
            else:
                resolved_role = role_row[0].upper()

        accounts.append({
            "account_id": cred.id,
            "credential_id": cred.id,
            "target_id": user.id,
            "operator_id": None,
            "user_id": user.id,
            "membership_id": tm.id,
            "username": cred.username,
            "role": resolved_role,
            "account_type": "tenant_user",
            "community_id": tenant.id,
            "community_name": tenant.slug,
        })

    return accounts


@router.post(
    "/password-reset/request",
    response_model=PasswordResetOtpResponse,
    summary="Request mobile OTP verification to reset account password",
)
async def request_password_reset_otp(
    body: PasswordResetOtpRequest,
    session: Annotated[Session, Depends(get_db)],
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
) -> PasswordResetOtpResponse:
    try:
        clean_mobile = normalize_mobile_e164(body.mobile_number)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_MOBILE", "message": str(exc)},
        )

    # Search for eligible accounts
    accounts = _find_accounts_by_mobile(session, clean_mobile)

    # Non-enumeration security: return 200 with generic response if no active account exists
    if not accounts:
        return PasswordResetOtpResponse(
            status="accepted",
            challenge_id=None,
            masked_mobile=format_masked_mobile(clean_mobile),
            cooldown_seconds=60,
            message="If an eligible account is associated with this mobile number, verification instructions have been dispatched.",
        )

    # Account exists: dispatch OTP
    try:
        challenge_id, cooldown = await otp_service.request_otp(
            mobile_number=clean_mobile,
            purpose="password_reset",
            tenant_id=accounts[0].get("community_id"),
        )
        return PasswordResetOtpResponse(
            status="accepted",
            challenge_id=challenge_id,
            masked_mobile=format_masked_mobile(clean_mobile),
            cooldown_seconds=cooldown,
            message="Verification code dispatched to your registered mobile number.",
        )
    except (OtpRateLimitExceeded, OtpRateLimited) as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "RATE_LIMIT_EXCEEDED", "message": str(exc), "retry_after": getattr(exc, "retry_after_seconds", 60)},
        )
    except MessagingProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "MESSAGING_PROVIDER_UNAVAILABLE", "message": str(exc)},
        )


@router.post(
    "/password-reset/verify",
    response_model=PasswordResetVerifyResponse,
    summary="Verify OTP for password reset and resolve eligible accounts",
)
async def verify_password_reset_otp(
    body: PasswordResetVerifyRequest,
    session: Annotated[Session, Depends(get_db)],
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
) -> PasswordResetVerifyResponse:
    try:
        clean_mobile = normalize_mobile_e164(body.mobile_number)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_MOBILE", "message": str(exc)},
        )

    try:
        success, reset_token = otp_service.verify_otp(
            challenge_id=body.challenge_id,
            mobile_number=clean_mobile,
            otp=body.otp,
            purpose="password_reset",
        )
    except OtpVerificationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "OTP_VERIFICATION_FAILED",
                "message": str(exc),
                "attempts_remaining": exc.attempts_remaining,
            },
        )

    if not success or not reset_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "OTP_VERIFICATION_FAILED", "message": "Verification failed."},
        )

    raw_accounts = _find_accounts_by_mobile(session, clean_mobile)
    if not raw_accounts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "ACCOUNT_NOT_FOUND", "message": "No eligible active accounts found for this mobile number."},
        )

    summaries = [
        PasswordResetAccountSummary(
            account_id=acc["account_id"],
            username=acc["username"],
            role=acc["role"],
            account_type=acc["account_type"],
            community_id=acc["community_id"],
            community_name=acc["community_name"],
        )
        for acc in raw_accounts
    ]

    requires_selection = len(summaries) > 1
    single_account = summaries[0] if len(summaries) == 1 else None

    return PasswordResetVerifyResponse(
        status="verified",
        reset_token=reset_token,
        requires_account_selection=requires_selection,
        account=single_account,
        accounts=summaries,
    )


@router.post(
    "/password-reset/complete",
    response_model=PasswordResetCompleteResponse,
    summary="Complete password reset with verified reset token",
)
async def complete_password_reset(
    body: PasswordResetCompleteRequest,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
) -> PasswordResetCompleteResponse:
    if body.new_password != body.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "PASSWORD_MISMATCH", "message": "New password and confirmation password do not match."},
        )

    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "PASSWORD_TOO_SHORT", "message": "New password must be at least 8 characters long."},
        )

    try:
        clean_mobile = normalize_mobile_e164(body.mobile_number)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_MOBILE", "message": str(exc)},
        )

    # 1. Atomically validate and consume the single-use reset token
    valid_token = otp_service.validate_and_consume_registration_token(
        session=session,
        verification_token=body.reset_token,
        mobile_number=clean_mobile,
        expected_purpose="password_reset",
    )
    if not valid_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_RESET_TOKEN", "message": "Password reset token is invalid, expired, or already used."},
        )

    # 2. Find eligible accounts for this mobile
    raw_accounts = _find_accounts_by_mobile(session, clean_mobile)
    if not raw_accounts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "ACCOUNT_NOT_FOUND", "message": "No eligible active accounts found for this mobile number."},
        )

    # 3. Match target account
    target: dict[str, Any] | None = None
    if body.account_id:
        for acc in raw_accounts:
            if acc["account_id"] == body.account_id or acc["target_id"] == body.account_id:
                target = acc
                break
        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "ACCOUNT_NOT_FOUND", "message": "Selected account does not belong to this verified mobile number."},
            )
    else:
        if len(raw_accounts) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "ACCOUNT_SELECTION_REQUIRED", "message": "Multiple accounts found. Please specify which account to reset."},
            )
        target = raw_accounts[0]

    # 4. Fetch and update the target credential
    cred = session.execute(
        select(AccountCredential)
        .where(
            AccountCredential.id == target["credential_id"],
            AccountCredential.status == "active",
        )
        .with_for_update()
    ).scalar_one_or_none()

    if not cred:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "CREDENTIAL_NOT_FOUND", "message": "Active credential record not found."},
        )

    now = datetime.now(UTC)
    cred.password_hash = hash_password(body.new_password)
    cred.must_change_password = False
    cred.updated_at = now

    # 5. Revoke active sessions across AuthSession and PlatformOperatorSession
    if target["account_type"] == "platform_operator":
        session.execute(
            update(PlatformOperatorSession)
            .where(
                PlatformOperatorSession.operator_id == target["operator_id"],
                PlatformOperatorSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
    else:
        session.execute(
            update(AuthSession)
            .where(
                AuthSession.membership_id.in_(
                    select(TenantMembership.id).where(TenantMembership.user_id == target["user_id"])
                ),
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    # 6. Record Audit Trail
    audit = AuditService()
    audit.record_mandatory(
        session,
        AuditEventDraft(
            event_type="auth.password_reset",
            actor_category="system",
            actor_ref="system:auth_service",
            action="account.password.reset",
            outcome="success",
            tenant_id=target.get("community_id"),
            target_entity_type="account_credential",
            target_entity_ref=str(cred.id),
        ),
    )

    session.commit()

    # 7. Security notification dispatch
    settings: Settings = getattr(request.app.state, "settings", None) or Settings()
    notif_client = getattr(request.app.state, "notification_client", None) or NotificationServiceClient(settings)
    try:
        await notif_client.send_text_message(
            recipient=clean_mobile,
            message_text="Your Community OTT account password was successfully reset. If you did not make this change, please contact support immediately.",
            tenant_id=target.get("community_id"),
            priority="P0",
        )
    except Exception:
        pass

    return PasswordResetCompleteResponse(
        status="success",
        message="Password has been reset successfully. You can now log in with your new password.",
    )


@router.post(
    "/otp/request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OtpRequestAccepted,
    dependencies=[Depends(require_local_development_otp)],
)
async def request_otp(
    body: OtpRequest,
    request: Request,
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
    repository: Annotated[SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)],
) -> OtpRequestAccepted:
    clean_mobile = normalize_mobile_e164(body.mobile_number)
    settings: Settings = request.app.state.settings

    target_tenant_id = body.community_id
    if body.purpose == "login":
        contexts = repository.resolve_contexts_for_mobile(clean_mobile)
        if not contexts:
            return OtpRequestAccepted(
                challenge_id=uuid4(),
                cooldown_seconds=settings.otp_send_cooldown_seconds,
            )
        if target_tenant_id is None:
            for ctx in contexts:
                if ctx.get("tenant_id"):
                    target_tenant_id = ctx["tenant_id"]
                    break

    try:
        challenge_id, cooldown = await otp_service.request_otp(
            mobile_number=clean_mobile,
            purpose=body.purpose,
            tenant_id=target_tenant_id,
        )
        return OtpRequestAccepted(challenge_id=challenge_id, cooldown_seconds=cooldown)
    except (OtpRateLimitExceeded, OtpRateLimited) as exc:
        if body.purpose == "login":
            return OtpRequestAccepted(
                challenge_id=uuid4(),
                cooldown_seconds=exc.retry_after_seconds,
            )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "RATE_LIMIT_EXCEEDED", "message": str(exc), "retry_after": exc.retry_after_seconds},
        )
    except AuthenticationConfigurationUnavailable:
        raise
    except MessagingProviderUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "MESSAGING_PROVIDER_UNAVAILABLE", "message": "Notification provider is temporarily unavailable."},
        )
    except MessagingError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "MESSAGING_PROVIDER_ERROR", "message": "Notification delivery failed."},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "OTP_REQUEST_FAILED", "message": str(exc)},
        )


@router.post(
    "/otp/verify",
    dependencies=[Depends(require_local_development_otp)],
)
async def verify_otp(
    body: OtpVerification,
    request: Request,
    response: Response,
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
    repository: Annotated[SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)],
) -> Any:
    clean_mobile = normalize_mobile_e164(body.mobile_number)
    settings: Settings = request.app.state.settings

    try:
        success, v_token = otp_service.verify_otp(
            challenge_id=body.challenge_id,
            mobile_number=clean_mobile,
            otp=body.otp,
            purpose=body.purpose,
        )
    except OtpVerificationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "OTP_VERIFICATION_FAILED", "message": str(exc), "attempts_remaining": exc.attempts_remaining},
        )

    if body.purpose in {"admin_registration", "agent_registration", "user_registration", "whatsapp_verification"}:
        return {
            "status": "verified",
            "verification_token": v_token,
            "mobile_number": clean_mobile,
            "purpose": body.purpose,
        }

    contexts = repository.resolve_contexts_for_mobile(clean_mobile)
    if not contexts:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "ACCOUNT_NOT_FOUND", "message": "No active account found for this mobile number."},
        )

    if len(contexts) > 1 and body.community_id is None:
        selection_token = secrets.token_urlsafe(32)
        if not hasattr(request.app.state, "context_selection_tokens"):
            request.app.state.context_selection_tokens = {}
        request.app.state.context_selection_tokens[selection_token] = {
            "mobile_e164": clean_mobile,
            "contexts": contexts,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        }
        return {
            "status": "select_context",
            "selection_token": selection_token,
            "contexts": [
                {
                    "tenant_id": str(c["tenant_id"]) if c["tenant_id"] else None,
                    "tenant_slug": c["tenant_slug"],
                    "role": c["role"],
                    "username": c["username"],
                }
                for c in contexts
            ],
        }

    target_tenant = body.community_id
    target_role = None
    if target_tenant is None and contexts:
        target_tenant = contexts[0]["tenant_id"]
        target_role = contexts[0]["role"]

    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_hex(32)

    result = repository.authenticate_with_mobile_otp(
        mobile_e164=clean_mobile,
        raw_session_token=raw_token,
        csrf_token=csrf_token,
        session_ttl_seconds=settings.auth_session_ttl_seconds,
        target_tenant_id=target_tenant,
        target_role=target_role,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTHENTICATION_FAILED", "message": "Failed to establish authenticated session."},
        )

    response.set_cookie(
        key=settings.auth_session_cookie_name,
        value=raw_token,
        max_age=settings.auth_session_ttl_seconds,
        expires=settings.auth_session_ttl_seconds,
        path=settings.auth_session_cookie_path,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.auth_session_cookie_samesite,
    )
    if result.role == "SUPER_ADMIN":
        response.set_cookie(
            key=settings.platform_oidc_session_cookie_name,
            value=raw_token,
            max_age=settings.platform_oidc_session_ttl_seconds,
            expires=settings.platform_oidc_session_ttl_seconds,
            path=settings.platform_oidc_session_cookie_path,
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite=settings.platform_oidc_session_cookie_samesite,
        )

    tenant_summary = None
    if result.tenant is not None:
        tenant_summary = TenantSummary(
            id=result.tenant["id"],
            slug=result.tenant["slug"],
        )

    return LoginResponse(
        user_id=result.user_id,
        username=result.username,
        role=result.role,
        status=result.status,
        tenant_id=result.tenant_id,
        membership_id=result.membership_id,
        tenant=tenant_summary,
        csrf_token=result.csrf_token,
        expires_at=result.expires_at,
        must_change_password=result.must_change_password,
        context=result.context or {},
    )


@router.post("/otp/select-context", response_model=LoginResponse)
async def select_login_context(
    body: ContextSelectRequest,
    request: Request,
    response: Response,
    repository: Annotated[SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)],
) -> LoginResponse:
    tokens = getattr(request.app.state, "context_selection_tokens", {})
    entry = tokens.get(body.selection_token)
    now = datetime.now(UTC)
    if entry is None or entry["expires_at"] < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "SELECTION_EXPIRED", "message": "Context selection token expired or invalid."},
        )

    clean_mobile = entry["mobile_e164"]
    settings: Settings = request.app.state.settings
    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_hex(32)

    result = repository.authenticate_with_mobile_otp(
        mobile_e164=clean_mobile,
        raw_session_token=raw_token,
        csrf_token=csrf_token,
        session_ttl_seconds=settings.auth_session_ttl_seconds,
        target_tenant_id=body.tenant_id,
        target_role=body.role,
    )

    if result is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    tokens.pop(body.selection_token, None)

    response.set_cookie(
        key=settings.auth_session_cookie_name,
        value=raw_token,
        max_age=settings.auth_session_ttl_seconds,
        expires=settings.auth_session_ttl_seconds,
        path=settings.auth_session_cookie_path,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.auth_session_cookie_samesite,
    )
    if result.role == "SUPER_ADMIN":
        response.set_cookie(
            key=settings.platform_oidc_session_cookie_name,
            value=raw_token,
            max_age=settings.platform_oidc_session_ttl_seconds,
            expires=settings.platform_oidc_session_ttl_seconds,
            path=settings.platform_oidc_session_cookie_path,
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite=settings.platform_oidc_session_cookie_samesite,
        )

    tenant_summary = None
    if result.tenant is not None:
        tenant_summary = TenantSummary(
            id=result.tenant["id"],
            slug=result.tenant["slug"],
        )

    return LoginResponse(
        user_id=result.user_id,
        username=result.username,
        role=result.role,
        status=result.status,
        tenant_id=result.tenant_id,
        membership_id=result.membership_id,
        tenant=tenant_summary,
        csrf_token=result.csrf_token,
        expires_at=result.expires_at,
        must_change_password=result.must_change_password,
        context=result.context or {},
    )


@router.post(
    "/email-otp/request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EmailOtpRequestAccepted,
    dependencies=[Depends(require_local_development_otp)],
)
async def request_email_otp(
    body: EmailOtpRequest,
    email_otp_service: Annotated[Any, Depends(get_email_otp_service)],
) -> EmailOtpRequestAccepted:
    try:
        res = await email_otp_service.request_otp(
            email=body.email,
            purpose=body.purpose,
            tenant_id=body.community_id,
        )
        return EmailOtpRequestAccepted(
            challenge_id=UUID(res["challenge_id"]),
            cooldown_seconds=res.get("cooldown_seconds", 60),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_EMAIL", "message": str(exc)},
        )
    except MessagingProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "MESSAGING_PROVIDER_UNAVAILABLE", "message": str(exc)},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "EMAIL_OTP_REQUEST_FAILED", "message": str(exc)},
        )


@router.post(
    "/email-otp/verify",
    dependencies=[Depends(require_local_development_otp)],
)
async def verify_email_otp(
    body: EmailOtpVerification,
    email_otp_service: Annotated[Any, Depends(get_email_otp_service)],
) -> Any:
    from app.modules.authentication.email_otp_service import EmailOtpVerificationFailed, normalize_email
    try:
        clean_email = normalize_email(body.email)
        success, v_token = email_otp_service.verify_otp(
            challenge_id=body.challenge_id,
            email=clean_email,
            otp=body.otp,
            purpose=body.purpose,
        )
        return {
            "status": "verified",
            "verification_token": v_token,
            "email": clean_email,
            "purpose": body.purpose,
        }
    except EmailOtpVerificationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "EMAIL_OTP_VERIFICATION_FAILED", "message": str(exc), "attempts_remaining": exc.attempts_remaining},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_EMAIL", "message": str(exc)},
        )



@router.get("/me", response_model=AuthenticatedIdentity)
async def current_identity(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
) -> AuthenticatedIdentity:
    resolved, _ = session_data
    tenant_summary = None
    if resolved.tenant is not None:
        tenant_summary = TenantSummary(
            id=resolved.tenant["id"],
            slug=resolved.tenant["slug"],
        )
    return AuthenticatedIdentity(
        user_id=resolved.user_id,
        username=resolved.username,
        role=resolved.role,
        status=resolved.status,
        tenant_id=resolved.tenant_id,
        membership_id=resolved.membership_id,
        csrf_token=resolved.csrf_token,
        expires_at=resolved.expires_at,
        must_change_password=resolved.must_change_password,
        tenant=tenant_summary,
        context=resolved.context,
    )


@router.get("/context", response_model=TrustedAuthorizationContext)
async def current_authorization_context(
    response: Response,
    tenant: Annotated[TenantContext, Depends(get_tenant_context)],
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    service: Annotated[AuthorizationService, Depends(get_authorization_context_service)],
) -> TrustedAuthorizationContext:
    """Return only grants and community scope resolved for this active tenant session."""
    permissions = service.resolve_effective_permissions(
        tenant=tenant,
        authenticated=authenticated,
    )
    response.headers["Cache-Control"] = "no-store"
    return TrustedAuthorizationContext(
        authentication=AuthorizationContextAuthentication(
            status="authenticated",
            session_expires_at=authenticated.expires_at,
        ),
        principal=AuthorizationContextPrincipal(
            type="tenant_member",
            id=authenticated.user_id,
            membership_id=authenticated.membership_id,
        ),
        authorization=AuthorizationContextAuthorization(
            status="resolved",
            effective_permissions=sorted(permission.value for permission in permissions),
            community_scope=AuthorizationContextScope(
                status="resolved",
                resolution="current_trusted_tenant",
                communities=[
                    AuthorizationContextCommunity(
                        id=tenant.tenant_id,
                        slug=tenant.tenant_slug,
                    )
                ],
            ),
        ),
        employee_authority=AuthorizationContextUnavailable(
            reason_code="EMPLOYEE_LINK_NOT_ESTABLISHED"
        ),
        product_role=AuthorizationContextUnavailable(
            reason_code="PRODUCT_ROLE_MAPPING_NOT_APPROVED"
        ),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    repository: Annotated[
        SQLAlchemyAuthenticationRepository, Depends(get_authentication_repository)
    ],
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token", max_length=64)] = None,
) -> Response:
    settings: Settings = request.app.state.settings
    resolved, raw_token = session_data
    origin = request.headers.get("origin")
    if (
        csrf_header is None
        or not secrets.compare_digest(csrf_header, resolved.csrf_token)
        or origin not in settings.frontend_origin_list
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    await run_in_threadpool(
        repository.revoke_session_by_token,
        raw_token,
    )
    response.delete_cookie(
        key=settings.auth_session_cookie_name,
        path=settings.auth_session_cookie_path,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.auth_session_cookie_samesite,
    )
    if resolved.account_type == "platform_operator":
        response.delete_cookie(
            key=settings.platform_oidc_session_cookie_name,
            path=settings.platform_oidc_session_cookie_path,
            secure=settings.session_cookie_secure,
            httponly=True,
            samesite=settings.platform_oidc_session_cookie_samesite,
        )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
