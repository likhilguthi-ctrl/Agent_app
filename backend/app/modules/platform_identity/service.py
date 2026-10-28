from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.platform_identity.errors import (
    InvalidPlatformIdentity,
    PlatformAuthorizationUnavailable,
    PlatformIdentityNotConfigured,
    PlatformIdentityUnavailable,
    PlatformOperatorNotAuthorized,
)
from app.modules.platform_identity.provider import ExternalOperatorIdentity, PlatformIdentityProvider
from app.modules.platform_identity.repository import SQLAlchemyPlatformIdentityRepository


@dataclass(frozen=True, slots=True)
class PlatformPrincipal:
    """Server-resolved platform identity and verified authentication strength."""

    operator_id: UUID
    issuer: str
    subject: str
    authenticated_at: datetime
    acr: str | None
    amr: tuple[str, ...]
    session_id: UUID | None = None
    csrf_token: str | None = None
    expires_at: datetime | None = None


class PlatformPermissionChecker(Protocol):
    def has_platform_permission(
        self,
        session: Session,
        *,
        operator_id: UUID,
        permission_code: str,
        now: datetime,
    ) -> bool: ...


class PlatformOperatorAuthenticationService:
    """Validate external OIDC identity, resolve an existing operator, and audit the result."""

    def __init__(
        self,
        provider: PlatformIdentityProvider,
        *,
        session_factory: sessionmaker[Session] | None = None,
        repository: SQLAlchemyPlatformIdentityRepository | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory or get_session_factory()
        self._repository = repository or SQLAlchemyPlatformIdentityRepository(self._session_factory)
        self._audit = audit_service or AuditService()

    async def authenticate(
        self,
        identity_token: str,
        *,
        expected_nonce: str,
        request_id: str | None = None,
    ) -> PlatformPrincipal:
        try:
            identity = await self._provider.validate_identity_token(
                identity_token, expected_nonce=expected_nonce
            )
        except PlatformIdentityNotConfigured:
            raise
        except InvalidPlatformIdentity:
            self._record_authentication_event(
                actor_ref=None,
                actor_category="unauthenticated",
                outcome="denied",
                reason_code="invalid_external_identity",
                request_id=request_id,
            )
            raise
        except PlatformIdentityUnavailable:
            self._record_authentication_event(
                actor_ref=None,
                actor_category="unauthenticated",
                outcome="failure",
                reason_code="identity_provider_unavailable",
                request_id=request_id,
            )
            raise

        operator_id: UUID | None = None
        with self._session_factory() as session, session.begin():
            operator = self._repository.find_active_operator(
                session, issuer=identity.issuer, subject=identity.subject
            )
            if operator is not None:
                operator_id = operator.id
                self._audit.record_mandatory(
                    session,
                    AuditEventDraft(
                        event_type="platform.operator.authentication",
                        actor_category="platform_operator",
                        actor_ref=str(operator.id),
                        action="operator.authenticate",
                        outcome="success",
                        request_id=request_id,
                        decision_metadata={"auth_strength": self._auth_strength(identity)},
                    ),
                )
            else:
                self._audit.record_mandatory(
                    session,
                    AuditEventDraft(
                        event_type="platform.operator.authentication",
                        actor_category="unauthenticated",
                        actor_ref=None,
                        action="operator.authenticate",
                        outcome="denied",
                        request_id=request_id,
                        decision_metadata={"reason_code": "operator_not_provisioned"},
                    ),
                )

        if operator_id is None:
            raise PlatformOperatorNotAuthorized
        return PlatformPrincipal(
            operator_id=operator_id,
            issuer=identity.issuer,
            subject=identity.subject,
            authenticated_at=identity.auth_time,
            acr=identity.acr,
            amr=identity.amr,
        )

    def _record_authentication_event(
        self,
        *,
        actor_ref: str | None,
        actor_category: str,
        outcome: str,
        reason_code: str,
        request_id: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            self._audit.record_mandatory(
                session,
                AuditEventDraft(
                    event_type="platform.operator.authentication",
                    actor_category=actor_category,
                    actor_ref=actor_ref,
                    action="operator.authenticate",
                    outcome=outcome,
                    request_id=request_id,
                    decision_metadata={"reason_code": reason_code},
                ),
            )

    @staticmethod
    def _auth_strength(identity: ExternalOperatorIdentity) -> str:
        return "mfa_verified" if identity.acr or identity.amr else "verified"


class PlatformAuthorizationService:
    """Evaluate only platform grants; community sessions/roles are not inputs."""

    def __init__(
        self,
        checker: PlatformPermissionChecker,
        *,
        session_factory: sessionmaker[Session] | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._checker = checker
        self._session_factory = session_factory or get_session_factory()
        self._audit = audit_service or AuditService()

    def require(
        self,
        principal: PlatformPrincipal,
        permission_code: str,
        *,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        evaluated_at = now or datetime.now(UTC)
        if not permission_code.startswith("platform."):
            raise PlatformOperatorNotAuthorized
        allowed = False
        with self._session_factory() as session, session.begin():
            allowed = self._checker.has_platform_permission(
                session,
                operator_id=principal.operator_id,
                permission_code=permission_code,
                now=evaluated_at,
            )
            if not allowed:
                self._audit.record_mandatory(
                    session,
                    AuditEventDraft(
                        event_type="platform.authorization.denied",
                        actor_category="platform_operator",
                        actor_ref=str(principal.operator_id),
                        action="platform.permission.require",
                        outcome="denied",
                        request_id=request_id,
                        decision_metadata={"reason_code": "permission_not_granted"},
                    ),
                )
        if not allowed:
            raise PlatformOperatorNotAuthorized

    def authorize_in_transaction(
        self,
        session: Session,
        principal: PlatformPrincipal,
        permission_code: str,
        *,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Evaluate and durably audit a denial in the caller's state-change transaction.

        Callers must commit the transaction before raising their public 403 response.
        This lets an allowed decision and mutation share one transaction, while a
        denied decision is still retained as a mandatory audit event.
        """
        evaluated_at = now or datetime.now(UTC)
        allowed = permission_code.startswith("platform.") and self._checker.has_platform_permission(
            session,
            operator_id=principal.operator_id,
            permission_code=permission_code,
            now=evaluated_at,
        )
        if not allowed:
            self._audit.record_mandatory(
                session,
                AuditEventDraft(
                    event_type="platform.authorization.denied",
                    actor_category="platform_operator",
                    actor_ref=str(principal.operator_id),
                    action="platform.permission.require",
                    outcome="denied",
                    request_id=request_id,
                    decision_metadata={"reason_code": "permission_not_granted"},
                ),
            )
        return allowed


def get_platform_authorization_service() -> PlatformAuthorizationService:
    return PlatformAuthorizationService(SQLAlchemyPlatformIdentityRepository())
