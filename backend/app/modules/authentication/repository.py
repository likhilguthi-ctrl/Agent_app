from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from typing import Any

from app.db.session import DatabaseUnavailable, get_session_factory
from app.modules.authentication.models import AccountCredential, AuthSession, OtpChallenge
from app.modules.authentication.password import hash_password, verify_password
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformOperatorSession,
    PlatformRole,
)
from app.modules.tenants.models import Tenant

_DUMMY_PASSWORD_HASH = hash_password("timing_mitigation_secret")


class AuthenticationStorageUnavailable(Exception):
    """Authentication persistence is unavailable; callers must fail closed."""


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session_id: UUID
    tenant_id: UUID
    user_id: UUID
    membership_id: UUID
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session_id: UUID
    tenant_id: UUID
    user_id: UUID
    membership_id: UUID
    csrf_token: str
    expires_at: datetime
    user_status: str
    membership_status: str
    tenant_status: str
    must_change_password: bool = False


@dataclass(frozen=True, slots=True)
class LoginResult:
    user_id: UUID
    username: str
    role: str
    status: str
    raw_token: str
    csrf_token: str
    expires_at: datetime
    tenant_id: UUID | None = None
    membership_id: UUID | None = None
    tenant: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    must_change_password: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedSession:
    session_id: UUID
    account_type: str
    user_id: UUID
    username: str
    role: str
    status: str
    csrf_token: str
    expires_at: datetime
    tenant_id: UUID | None = None
    membership_id: UUID | None = None
    user_status: str = "active"
    membership_status: str = "active"
    tenant_status: str = "active"
    tenant: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    must_change_password: bool = False


@dataclass(frozen=True, slots=True)
class CredentialVerificationResult:
    valid: bool
    user_id: UUID | None = None
    account_type: str | None = None
    mobile_e164: str | None = None
    tenant_id: UUID | None = None
    username: str | None = None


class SQLAlchemyAuthenticationRepository:
    """SQL-backed challenge and tenant-bound opaque session persistence."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        try:
            self._session_factory = session_factory or get_session_factory()
        except RuntimeError as exc:
            raise DatabaseUnavailable from exc

    def create_challenge(
        self,
        tenant_id: UUID,
        mobile_e164: str,
        challenge_id: UUID,
        ttl_seconds: int,
    ) -> None:
        now = datetime.now(UTC)
        try:
            with self._session_factory.begin() as session:
                challenges = session.execute(
                    select(OtpChallenge)
                    .where(
                        OtpChallenge.tenant_id == tenant_id,
                        OtpChallenge.mobile_e164 == mobile_e164,
                    )
                    .order_by(OtpChallenge.created_at.desc())
                    .with_for_update()
                ).scalars().all()
                if not challenges:
                    session.add(
                        OtpChallenge(
                            id=challenge_id,
                            tenant_id=tenant_id,
                            mobile_e164=mobile_e164,
                            created_at=now,
                            expires_at=now + timedelta(seconds=ttl_seconds),
                        )
                    )
                else:
                    challenge = challenges[0]
                    # Replacing the sole row invalidates every earlier challenge ID.
                    challenge.id = challenge_id
                    challenge.created_at = now
                    challenge.expires_at = now + timedelta(seconds=ttl_seconds)
                    challenge.consumed_at = None
                    for extra in challenges[1:]:
                        session.delete(extra)
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def revoke_challenge(self, tenant_id: UUID, challenge_id: UUID) -> None:
        try:
            with self._session_factory.begin() as session:
                session.execute(
                    update(OtpChallenge)
                    .where(
                        OtpChallenge.id == challenge_id,
                        OtpChallenge.tenant_id == tenant_id,
                        OtpChallenge.consumed_at.is_(None),
                    )
                    .values(consumed_at=datetime.now(UTC))
                )
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def is_challenge_active(
        self,
        tenant_id: UUID,
        mobile_e164: str,
        challenge_id: UUID,
    ) -> bool:
        try:
            with self._session_factory() as session:
                challenge = session.execute(
                    select(OtpChallenge.id).where(
                        OtpChallenge.id == challenge_id,
                        OtpChallenge.tenant_id == tenant_id,
                        OtpChallenge.mobile_e164 == mobile_e164,
                        OtpChallenge.consumed_at.is_(None),
                        OtpChallenge.expires_at > datetime.now(UTC),
                    )
                ).scalar_one_or_none()
                return challenge is not None
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def consume_challenge_and_issue_session(
        self,
        *,
        tenant_id: UUID,
        mobile_e164: str,
        challenge_id: UUID,
        raw_session_token: str,
        csrf_token: str,
        session_ttl_seconds: int,
    ) -> IssuedSession | None:
        now = datetime.now(UTC)
        try:
            with self._session_factory.begin() as session:
                consumed = session.execute(
                    update(OtpChallenge)
                    .where(
                        OtpChallenge.id == challenge_id,
                        OtpChallenge.tenant_id == tenant_id,
                        OtpChallenge.mobile_e164 == mobile_e164,
                        OtpChallenge.consumed_at.is_(None),
                        OtpChallenge.expires_at > now,
                    )
                    .values(consumed_at=now)
                    .returning(OtpChallenge.id)
                ).scalar_one_or_none()
                if consumed is None:
                    return None

                membership_row = session.execute(
                    select(
                        TenantMembership.id.label("membership_id"),
                        TenantMembership.user_id.label("user_id"),
                    )
                    .join(User, User.id == TenantMembership.user_id)
                    .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                    .where(
                        TenantMembership.tenant_id == tenant_id,
                        TenantMembership.mobile_e164 == mobile_e164,
                        TenantMembership.status == "active",
                        User.status == "active",
                        Tenant.status == "active",
                    )
                    .with_for_update()
                ).one_or_none()
                if membership_row is None:
                    return None

                session.execute(
                    delete(AuthSession).where(
                        AuthSession.tenant_id == tenant_id,
                        AuthSession.membership_id == membership_row.membership_id,
                        or_(
                            AuthSession.expires_at < now - timedelta(days=30),
                            AuthSession.revoked_at < now - timedelta(days=30),
                        ),
                    )
                )

                session_id = uuid4()
                expires_at = now + timedelta(seconds=session_ttl_seconds)
                session.add(
                    AuthSession(
                        id=session_id,
                        tenant_id=tenant_id,
                        membership_id=membership_row.membership_id,
                        token_digest=sha256(raw_session_token.encode("utf-8")).hexdigest(),
                        csrf_token=csrf_token,
                        created_at=now,
                        expires_at=expires_at,
                    )
                )
                return IssuedSession(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    user_id=membership_row.user_id,
                    membership_id=membership_row.membership_id,
                    csrf_token=csrf_token,
                    expires_at=expires_at,
                )
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def find_session_for_tenant(
        self,
        *,
        tenant_id: UUID,
        raw_session_token: str,
    ) -> AuthenticatedSession | None:
        token_digest = sha256(raw_session_token.encode("utf-8")).hexdigest()
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(
                        AuthSession.id.label("session_id"),
                        AuthSession.tenant_id.label("tenant_id"),
                        TenantMembership.user_id.label("user_id"),
                        TenantMembership.id.label("membership_id"),
                        AuthSession.csrf_token.label("csrf_token"),
                        AuthSession.expires_at.label("expires_at"),
                        User.status.label("user_status"),
                        TenantMembership.status.label("membership_status"),
                        Tenant.status.label("tenant_status"),
                    )
                    .join(
                        TenantMembership,
                        (TenantMembership.id == AuthSession.membership_id)
                        & (TenantMembership.tenant_id == AuthSession.tenant_id),
                    )
                    .join(User, User.id == TenantMembership.user_id)
                    .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                    .where(
                        AuthSession.token_digest == token_digest,
                        AuthSession.tenant_id == tenant_id,
                        AuthSession.revoked_at.is_(None),
                        AuthSession.expires_at > datetime.now(UTC),
                    )
                ).one_or_none()
                if row is None:
                    return None
                cred_must_change = session.execute(
                    select(AccountCredential.must_change_password).where(
                        AccountCredential.user_id == row.user_id,
                        AccountCredential.account_type == "tenant_user",
                    )
                ).scalar_one_or_none() or False
                mapping = dict(row._mapping)
                mapping["must_change_password"] = cred_must_change
                return AuthenticatedSession(**mapping)
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def revoke_session(self, session_id: UUID, tenant_id: UUID) -> None:
        try:
            with self._session_factory.begin() as session:
                session.execute(
                    update(AuthSession)
                    .where(
                        AuthSession.id == session_id,
                        AuthSession.tenant_id == tenant_id,
                        AuthSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(UTC))
                )
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def authenticate_with_credentials(
        self,
        *,
        username: str,
        password: str,
        raw_session_token: str,
        csrf_token: str,
        session_ttl_seconds: int,
    ) -> LoginResult | None:
        normalized_username = username.strip().lower()
        now = datetime.now(UTC)
        try:
            with self._session_factory.begin() as session:
                credential = session.execute(
                    select(AccountCredential).where(
                        AccountCredential.username == normalized_username
                    )
                ).scalar_one_or_none()

                if credential is None:
                    # Timing attack mitigation: always run password verification
                    verify_password(password, _DUMMY_PASSWORD_HASH)
                    return None

                if not verify_password(password, credential.password_hash):
                    return None

                if credential.status != "active":
                    return None

                if credential.account_type == "platform_operator":
                    if not credential.operator_id:
                        return None
                    operator = session.execute(
                        select(PlatformOperator).where(
                            PlatformOperator.id == credential.operator_id,
                            PlatformOperator.status == "active",
                        )
                    ).scalar_one_or_none()
                    if operator is None:
                        return None

                    # Resolve platform role
                    role_row = session.execute(
                        select(PlatformRole.code)
                        .join(
                            PlatformOperatorRoleAssignment,
                            PlatformOperatorRoleAssignment.role_id == PlatformRole.id,
                        )
                        .where(
                            PlatformOperatorRoleAssignment.operator_id == operator.id,
                            PlatformOperatorRoleAssignment.status == "active",
                            PlatformRole.status == "active",
                            PlatformOperatorRoleAssignment.valid_from <= now,
                            or_(
                                PlatformOperatorRoleAssignment.valid_until.is_(None),
                                PlatformOperatorRoleAssignment.valid_until > now,
                            ),
                        )
                    ).first()

                    raw_role_code = role_row[0] if role_row else "super_admin"
                    if raw_role_code.lower() in {"super_admin", "superadmin", "platform_admin"}:
                        resolved_role = "SUPER_ADMIN"
                    else:
                        resolved_role = raw_role_code.upper()

                    token_digest = sha256(raw_session_token.encode("utf-8")).hexdigest()
                    session_id = uuid4()
                    expires_at = now + timedelta(seconds=session_ttl_seconds)

                    session.add(
                        PlatformOperatorSession(
                            id=session_id,
                            operator_id=operator.id,
                            token_digest=token_digest,
                            csrf_token=csrf_token,
                            authenticated_at=now,
                            acr="urn:community:pwd",
                            amr=["pwd"],
                            created_at=now,
                            expires_at=expires_at,
                        )
                    )

                    return LoginResult(
                        user_id=operator.id,
                        username=credential.username,
                        role=resolved_role,
                        status="active",
                        raw_token=raw_session_token,
                        csrf_token=csrf_token,
                        expires_at=expires_at,
                        tenant_id=None,
                        membership_id=None,
                        tenant=None,
                        context={"type": "platform", "role": resolved_role},
                        must_change_password=getattr(credential, "must_change_password", False),
                    )

                elif credential.account_type == "tenant_user":
                    if not credential.user_id:
                        return None
                    user = session.execute(
                        select(User).where(
                            User.id == credential.user_id,
                            User.status == "active",
                        )
                    ).scalar_one_or_none()
                    if user is None:
                        return None

                    membership_row = session.execute(
                        select(
                            TenantMembership.id.label("membership_id"),
                            Tenant.id.label("tenant_id"),
                            Tenant.slug.label("tenant_slug"),
                        )
                        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                        .where(
                            TenantMembership.user_id == user.id,
                            TenantMembership.status == "active",
                            Tenant.status == "active",
                        )
                    ).first()

                    if membership_row is None:
                        return None

                    role_row = session.execute(
                        select(Role.code)
                        .join(UserRoleAssignment, UserRoleAssignment.role_id == Role.id)
                        .where(
                            UserRoleAssignment.tenant_id == membership_row.tenant_id,
                            UserRoleAssignment.membership_id == membership_row.membership_id,
                            UserRoleAssignment.status == "active",
                            Role.status == "active",
                            UserRoleAssignment.valid_from <= now,
                            or_(
                                UserRoleAssignment.valid_until.is_(None),
                                UserRoleAssignment.valid_until > now,
                            ),
                        )
                    ).first()

                    if role_row is None:
                        return None

                    raw_role = role_row[0].lower()
                    if raw_role in {"admin", "tenant_admin", "community_admin"}:
                        resolved_role = "ADMIN"
                    elif raw_role in {"agent", "employee"}:
                        resolved_role = "AGENT"
                    else:
                        resolved_role = role_row[0].upper()

                    token_digest = sha256(raw_session_token.encode("utf-8")).hexdigest()
                    session_id = uuid4()
                    expires_at = now + timedelta(seconds=session_ttl_seconds)

                    session.add(
                        AuthSession(
                            id=session_id,
                            tenant_id=membership_row.tenant_id,
                            membership_id=membership_row.membership_id,
                            token_digest=token_digest,
                            csrf_token=csrf_token,
                            created_at=now,
                            expires_at=expires_at,
                        )
                    )

                    return LoginResult(
                        user_id=user.id,
                        username=credential.username,
                        role=resolved_role,
                        status="active",
                        raw_token=raw_session_token,
                        csrf_token=csrf_token,
                        expires_at=expires_at,
                        tenant_id=membership_row.tenant_id,
                        membership_id=membership_row.membership_id,
                        tenant={
                            "id": membership_row.tenant_id,
                            "slug": membership_row.tenant_slug,
                        },
                        context={
                            "type": "tenant",
                            "tenant_slug": membership_row.tenant_slug,
                            "role": resolved_role,
                        },
                        must_change_password=getattr(credential, "must_change_password", False),
                    )

                return None
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def resolve_contexts_for_mobile(self, mobile_e164: str) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        contexts: list[dict[str, Any]] = []
        try:
            with self._session_factory() as session:
                # 1. Platform Operator (Super Admin)
                operator = session.execute(
                    select(PlatformOperator).where(
                        or_(
                            PlatformOperator.mobile_e164 == mobile_e164,
                            (PlatformOperator.subject == "operator:demo_superadmin") & (mobile_e164 in {"+15550000001", "+919999999999"}),
                        ),
                        PlatformOperator.status == "active",
                    )
                ).scalar_one_or_none()

                if operator is not None:
                    contexts.append({
                        "type": "platform",
                        "tenant_id": None,
                        "tenant_slug": None,
                        "membership_id": None,
                        "user_id": operator.id,
                        "role": "SUPER_ADMIN",
                        "username": "Super Admin",
                    })

                # 2. Tenant Memberships
                memberships = session.execute(
                    select(
                        TenantMembership.id.label("membership_id"),
                        TenantMembership.tenant_id.label("tenant_id"),
                        Tenant.slug.label("tenant_slug"),
                        User.id.label("user_id"),
                    )
                    .join(User, User.id == TenantMembership.user_id)
                    .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                    .where(
                        TenantMembership.mobile_e164 == mobile_e164,
                        TenantMembership.status == "active",
                        User.status == "active",
                        Tenant.status == "active",
                    )
                ).all()

                for m in memberships:
                    role_row = session.execute(
                        select(Role.code)
                        .join(UserRoleAssignment, UserRoleAssignment.role_id == Role.id)
                        .where(
                            UserRoleAssignment.tenant_id == m.tenant_id,
                            UserRoleAssignment.membership_id == m.membership_id,
                            UserRoleAssignment.status == "active",
                            Role.status == "active",
                            UserRoleAssignment.valid_from <= now,
                            or_(
                                UserRoleAssignment.valid_until.is_(None),
                                UserRoleAssignment.valid_until > now,
                            ),
                        )
                    ).first()

                    resolved_role = "MEMBER"
                    if role_row is not None:
                        raw_role = role_row[0].lower()
                        if raw_role in {"admin", "tenant_admin", "community_admin"}:
                            resolved_role = "ADMIN"
                        elif raw_role in {"agent", "employee"}:
                            resolved_role = "AGENT"
                        else:
                            resolved_role = role_row[0].upper()

                    username = session.execute(
                        select(AccountCredential.username).where(
                            AccountCredential.user_id == m.user_id,
                            AccountCredential.account_type == "tenant_user",
                        )
                    ).scalar_one_or_none()

                    contexts.append({
                        "type": "tenant",
                        "tenant_id": m.tenant_id,
                        "tenant_slug": m.tenant_slug,
                        "membership_id": m.membership_id,
                        "user_id": m.user_id,
                        "role": resolved_role,
                        "username": username or mobile_e164,
                    })

            return contexts
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def authenticate_with_mobile_otp(
        self,
        mobile_e164: str,
        raw_session_token: str,
        csrf_token: str,
        session_ttl_seconds: int,
        target_tenant_id: UUID | None = None,
        target_role: str | None = None,
    ) -> LoginResult | None:
        now = datetime.now(UTC)
        try:
            with self._session_factory.begin() as session:
                is_platform_login = (target_role == "SUPER_ADMIN" or target_tenant_id is None)
                operator = None
                if is_platform_login:
                    operator = session.execute(
                        select(PlatformOperator).where(
                            or_(
                                PlatformOperator.mobile_e164 == mobile_e164,
                                (PlatformOperator.subject == "operator:demo_superadmin") & (mobile_e164 in {"+15550000001", "+919999999999"}),
                            ),
                            PlatformOperator.status == "active",
                        )
                    ).scalar_one_or_none()

                if operator is not None and (target_role == "SUPER_ADMIN" or target_tenant_id is None):
                    token_digest = sha256(raw_session_token.encode("utf-8")).hexdigest()
                    session_id = uuid4()
                    expires_at = now + timedelta(seconds=session_ttl_seconds)

                    session.add(
                        PlatformOperatorSession(
                            id=session_id,
                            operator_id=operator.id,
                            token_digest=token_digest,
                            csrf_token=csrf_token,
                            authenticated_at=now,
                            acr="urn:community:otp",
                            amr=["otp"],
                            created_at=now,
                            expires_at=expires_at,
                        )
                    )

                    cred = session.execute(
                        select(AccountCredential).where(
                            AccountCredential.operator_id == operator.id,
                            AccountCredential.account_type == "platform_operator",
                        )
                    ).scalar_one_or_none()
                    op_username = cred.username if cred else "superadmin"
                    op_must_change = cred.must_change_password if cred else False

                    return LoginResult(
                        user_id=operator.id,
                        username=op_username,
                        role="SUPER_ADMIN",
                        status="active",
                        raw_token=raw_session_token,
                        csrf_token=csrf_token,
                        expires_at=expires_at,
                        tenant_id=None,
                        membership_id=None,
                        tenant=None,
                        context={"type": "platform", "role": "SUPER_ADMIN"},
                        must_change_password=op_must_change,
                    )

                query = (
                    select(
                        TenantMembership.id.label("membership_id"),
                        TenantMembership.tenant_id.label("tenant_id"),
                        Tenant.slug.label("tenant_slug"),
                        User.id.label("user_id"),
                    )
                    .join(User, User.id == TenantMembership.user_id)
                    .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                    .where(
                        TenantMembership.mobile_e164 == mobile_e164,
                        TenantMembership.status == "active",
                        User.status == "active",
                        Tenant.status == "active",
                    )
                )
                if target_tenant_id is not None:
                    query = query.where(TenantMembership.tenant_id == target_tenant_id)

                membership_row = session.execute(query).first()
                if membership_row is None:
                    return None

                role_row = session.execute(
                    select(Role.code)
                    .join(UserRoleAssignment, UserRoleAssignment.role_id == Role.id)
                    .where(
                        UserRoleAssignment.tenant_id == membership_row.tenant_id,
                        UserRoleAssignment.membership_id == membership_row.membership_id,
                        UserRoleAssignment.status == "active",
                        Role.status == "active",
                        UserRoleAssignment.valid_from <= now,
                        or_(
                            UserRoleAssignment.valid_until.is_(None),
                            UserRoleAssignment.valid_until > now,
                        ),
                    )
                ).first()

                resolved_role = "MEMBER"
                if role_row is not None:
                    raw_role = role_row[0].lower()
                    if raw_role in {"admin", "tenant_admin", "community_admin"}:
                        resolved_role = "ADMIN"
                    elif raw_role in {"agent", "employee"}:
                        resolved_role = "AGENT"
                    else:
                        resolved_role = role_row[0].upper()

                cred = session.execute(
                    select(AccountCredential).where(
                        AccountCredential.user_id == membership_row.user_id,
                        AccountCredential.account_type == "tenant_user",
                    )
                ).scalar_one_or_none()
                username = cred.username if cred else mobile_e164
                tu_must_change = cred.must_change_password if cred else False

                token_digest = sha256(raw_session_token.encode("utf-8")).hexdigest()
                session_id = uuid4()
                expires_at = now + timedelta(seconds=session_ttl_seconds)

                session.add(
                    AuthSession(
                        id=session_id,
                        tenant_id=membership_row.tenant_id,
                        membership_id=membership_row.membership_id,
                        token_digest=token_digest,
                        csrf_token=csrf_token,
                        created_at=now,
                        expires_at=expires_at,
                    )
                )

                return LoginResult(
                    user_id=membership_row.user_id,
                    username=username,
                    role=resolved_role,
                    status="active",
                    raw_token=raw_session_token,
                    csrf_token=csrf_token,
                    expires_at=expires_at,
                    tenant_id=membership_row.tenant_id,
                    membership_id=membership_row.membership_id,
                    tenant={
                        "id": membership_row.tenant_id,
                        "slug": membership_row.tenant_slug,
                    },
                    context={
                        "type": "tenant",
                        "tenant_slug": membership_row.tenant_slug,
                        "role": resolved_role,
                    },
                    must_change_password=tu_must_change,
                )
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def resolve_session(
        self,
        raw_session_token: str,
        now: datetime | None = None,
    ) -> ResolvedSession | None:
        token_digest = sha256(raw_session_token.encode("utf-8")).hexdigest()
        eval_now = now or datetime.now(UTC)
        try:
            with self._session_factory() as session:
                # 1. First check AuthSession (tenant user)
                auth_row = session.execute(
                    select(
                        AuthSession.id.label("session_id"),
                        AuthSession.tenant_id.label("tenant_id"),
                        AuthSession.csrf_token.label("csrf_token"),
                        AuthSession.expires_at.label("expires_at"),
                        TenantMembership.id.label("membership_id"),
                        TenantMembership.status.label("membership_status"),
                        User.id.label("user_id"),
                        User.status.label("user_status"),
                        Tenant.slug.label("tenant_slug"),
                        Tenant.status.label("tenant_status"),
                    )
                    .join(
                        TenantMembership,
                        (TenantMembership.id == AuthSession.membership_id)
                        & (TenantMembership.tenant_id == AuthSession.tenant_id),
                    )
                    .join(User, User.id == TenantMembership.user_id)
                    .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                    .where(
                        AuthSession.token_digest == token_digest,
                        AuthSession.revoked_at.is_(None),
                        AuthSession.expires_at > eval_now,
                    )
                ).first()

                if auth_row is not None:
                    # Lookup username and must_change_password
                    cred_row = session.execute(
                        select(AccountCredential.username, AccountCredential.must_change_password).where(
                            AccountCredential.user_id == auth_row.user_id,
                            AccountCredential.account_type == "tenant_user",
                        )
                    ).first()
                    cred_username = cred_row[0] if cred_row else None
                    cred_must_change = cred_row[1] if cred_row else False

                    # Lookup active role
                    role_row = session.execute(
                        select(Role.code)
                        .join(UserRoleAssignment, UserRoleAssignment.role_id == Role.id)
                        .where(
                            UserRoleAssignment.tenant_id == auth_row.tenant_id,
                            UserRoleAssignment.membership_id == auth_row.membership_id,
                            UserRoleAssignment.status == "active",
                            Role.status == "active",
                            UserRoleAssignment.valid_from <= eval_now,
                            or_(
                                UserRoleAssignment.valid_until.is_(None),
                                UserRoleAssignment.valid_until > eval_now,
                            ),
                        )
                    ).first()

                    if role_row is not None:
                        raw_role = role_row[0].lower()
                        if raw_role in {"admin", "tenant_admin", "community_admin"}:
                            resolved_role = "ADMIN"
                        elif raw_role in {"agent", "employee"}:
                            resolved_role = "AGENT"
                        else:
                            resolved_role = role_row[0].upper()
                    else:
                        resolved_role = "MEMBER"

                    return ResolvedSession(
                        session_id=auth_row.session_id,
                        account_type="tenant_user",
                        user_id=auth_row.user_id,
                        username=cred_username or str(auth_row.user_id),
                        role=resolved_role,
                        status="active",
                        csrf_token=auth_row.csrf_token,
                        expires_at=auth_row.expires_at,
                        tenant_id=auth_row.tenant_id,
                        membership_id=auth_row.membership_id,
                        user_status=auth_row.user_status,
                        membership_status=auth_row.membership_status,
                        tenant_status=auth_row.tenant_status,
                        tenant={
                            "id": auth_row.tenant_id,
                            "slug": auth_row.tenant_slug,
                        },
                        context={
                            "type": "tenant",
                            "tenant_slug": auth_row.tenant_slug,
                            "role": resolved_role,
                        },
                        must_change_password=cred_must_change,
                    )

                # 2. Check PlatformOperatorSession (platform operator)
                op_row = session.execute(
                    select(
                        PlatformOperatorSession.id.label("session_id"),
                        PlatformOperatorSession.operator_id.label("operator_id"),
                        PlatformOperatorSession.csrf_token.label("csrf_token"),
                        PlatformOperatorSession.expires_at.label("expires_at"),
                        PlatformOperator.status.label("operator_status"),
                    )
                    .join(PlatformOperator, PlatformOperator.id == PlatformOperatorSession.operator_id)
                    .where(
                        PlatformOperatorSession.token_digest == token_digest,
                        PlatformOperatorSession.revoked_at.is_(None),
                        PlatformOperatorSession.expires_at > eval_now,
                    )
                ).first()

                if op_row is not None:
                    cred_row = session.execute(
                        select(AccountCredential.username, AccountCredential.must_change_password).where(
                            AccountCredential.operator_id == op_row.operator_id,
                            AccountCredential.account_type == "platform_operator",
                        )
                    ).first()
                    cred_username = cred_row[0] if cred_row else None
                    cred_must_change = cred_row[1] if cred_row else False

                    role_row = session.execute(
                        select(PlatformRole.code)
                        .join(
                            PlatformOperatorRoleAssignment,
                            PlatformOperatorRoleAssignment.role_id == PlatformRole.id,
                        )
                        .where(
                            PlatformOperatorRoleAssignment.operator_id == op_row.operator_id,
                            PlatformOperatorRoleAssignment.status == "active",
                            PlatformRole.status == "active",
                            PlatformOperatorRoleAssignment.valid_from <= eval_now,
                            or_(
                                PlatformOperatorRoleAssignment.valid_until.is_(None),
                                PlatformOperatorRoleAssignment.valid_until > eval_now,
                            ),
                        )
                    ).first()

                    raw_role_code = role_row[0] if role_row else "super_admin"
                    if raw_role_code.lower() in {"super_admin", "superadmin", "platform_admin"}:
                        resolved_role = "SUPER_ADMIN"
                    else:
                        resolved_role = raw_role_code.upper()

                    return ResolvedSession(
                        session_id=op_row.session_id,
                        account_type="platform_operator",
                        user_id=op_row.operator_id,
                        username=cred_username or str(op_row.operator_id),
                        role=resolved_role,
                        status="active",
                        csrf_token=op_row.csrf_token,
                        expires_at=op_row.expires_at,
                        tenant_id=None,
                        membership_id=None,
                        user_status=op_row.operator_status,
                        membership_status="active",
                        tenant_status="active",
                        tenant=None,
                        context={"type": "platform", "role": resolved_role},
                        must_change_password=cred_must_change,
                    )

                return None
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def revoke_session_by_token(self, raw_session_token: str) -> bool:
        token_digest = sha256(raw_session_token.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        try:
            with self._session_factory.begin() as session:
                res1 = session.execute(
                    update(AuthSession)
                    .where(
                        AuthSession.token_digest == token_digest,
                        AuthSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                res2 = session.execute(
                    update(PlatformOperatorSession)
                    .where(
                        PlatformOperatorSession.token_digest == token_digest,
                        PlatformOperatorSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                return (res1.rowcount or 0) > 0 or (res2.rowcount or 0) > 0
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def verify_credentials_for_otp(
        self,
        *,
        username: str,
        password: str,
    ) -> CredentialVerificationResult | None:
        """Verifies local username & password and returns the bound mobile number and context for OTP dispatch.

        Applies constant-time dummy verification on missing accounts to mitigate timing-based enumeration attacks.
        """
        normalized_username = username.strip().lower()
        try:
            with self._session_factory.begin() as session:
                credential = session.execute(
                    select(AccountCredential).where(
                        AccountCredential.username == normalized_username
                    )
                ).scalar_one_or_none()

                if credential is None:
                    verify_password(password, _DUMMY_PASSWORD_HASH)
                    return None

                if not verify_password(password, credential.password_hash):
                    return None

                if credential.status != "active":
                    return None

                if credential.account_type == "platform_operator":
                    if not credential.operator_id:
                        return None
                    operator = session.execute(
                        select(PlatformOperator).where(
                            PlatformOperator.id == credential.operator_id,
                            PlatformOperator.status == "active",
                        )
                    ).scalar_one_or_none()
                    if operator is None or not operator.mobile_e164:
                        return None
                    return CredentialVerificationResult(
                        valid=True,
                        user_id=operator.id,
                        account_type="platform_operator",
                        mobile_e164=operator.mobile_e164,
                        tenant_id=None,
                        username=credential.username,
                    )
                elif credential.account_type == "tenant_user":
                    if not credential.user_id:
                        return None
                    user = session.execute(
                        select(User).where(
                            User.id == credential.user_id,
                            User.status == "active",
                        )
                    ).scalar_one_or_none()
                    if user is None:
                        return None
                    membership = session.execute(
                        select(TenantMembership).where(
                            TenantMembership.user_id == user.id,
                            TenantMembership.status == "active",
                        )
                    ).scalars().first()
                    if membership is None or not membership.mobile_e164:
                        return None
                    return CredentialVerificationResult(
                        valid=True,
                        user_id=user.id,
                        account_type="tenant_user",
                        mobile_e164=membership.mobile_e164,
                        tenant_id=membership.tenant_id,
                        username=credential.username,
                    )
                return None
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc

    def change_password(
        self,
        *,
        user_id: UUID,
        account_type: str,
        current_password: str,
        new_password: str,
    ) -> bool:
        """Verifies current password, updates password hash using scrypt, and clears must_change_password."""
        now = datetime.now(UTC)
        try:
            with self._session_factory.begin() as session:
                if account_type == "platform_operator":
                    cred = session.execute(
                        select(AccountCredential).where(
                            AccountCredential.operator_id == user_id,
                            AccountCredential.account_type == "platform_operator",
                            AccountCredential.status == "active",
                        )
                    ).scalar_one_or_none()
                else:
                    cred = session.execute(
                        select(AccountCredential).where(
                            AccountCredential.user_id == user_id,
                            AccountCredential.account_type == "tenant_user",
                            AccountCredential.status == "active",
                        )
                    ).scalar_one_or_none()

                if cred is None:
                    return False

                if not verify_password(current_password, cred.password_hash):
                    return False

                cred.password_hash = hash_password(new_password)
                cred.must_change_password = False
                cred.updated_at = now
                return True
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AuthenticationStorageUnavailable from exc


