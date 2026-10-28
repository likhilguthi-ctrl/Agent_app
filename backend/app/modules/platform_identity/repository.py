from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.modules.platform_identity.errors import PlatformAuthorizationUnavailable
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformRole,
    PlatformRolePermission,
)


class SQLAlchemyPlatformIdentityRepository:
    """Resolve operator identity and grants from platform-owned database rows only."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    def find_active_operator(
        self, session: Session, *, issuer: str, subject: str
    ) -> PlatformOperator | None:
        try:
            return session.scalar(
                select(PlatformOperator).where(
                    PlatformOperator.issuer == issuer,
                    PlatformOperator.subject == subject,
                    PlatformOperator.status == "active",
                )
            )
        except SQLAlchemyError as exc:
            raise PlatformAuthorizationUnavailable from exc

    def has_platform_permission(
        self,
        session: Session,
        *,
        operator_id: UUID,
        permission_code: str,
        now: datetime,
    ) -> bool:
        statement = (
            select(PlatformOperatorRoleAssignment.id)
            .join(PlatformOperator, PlatformOperator.id == PlatformOperatorRoleAssignment.operator_id)
            .join(PlatformRole, PlatformRole.id == PlatformOperatorRoleAssignment.role_id)
            .join(PlatformRolePermission, PlatformRolePermission.role_id == PlatformRole.id)
            .where(
                PlatformOperator.id == operator_id,
                PlatformOperator.status == "active",
                PlatformRole.status == "active",
                PlatformRolePermission.permission_code == permission_code,
                PlatformRolePermission.status == "active",
                PlatformOperatorRoleAssignment.status == "active",
                PlatformOperatorRoleAssignment.valid_from <= now,
                or_(
                    PlatformOperatorRoleAssignment.valid_until.is_(None),
                    PlatformOperatorRoleAssignment.valid_until > now,
                ),
            )
            .limit(1)
        )
        try:
            return session.scalar(statement) is not None
        except SQLAlchemyError as exc:
            raise PlatformAuthorizationUnavailable from exc

    def has_onboarding_authority(self, session: Session, *, now: datetime) -> bool:
        required_permissions = (
            "platform.onboarding.create",
            "platform.onboarding.read",
            "platform.onboarding.cancel",
            "platform.domain.challenge",
            "platform.domain.verify",
            "platform.tenant.approve",
            "platform.tenant.reject",
            "platform.tenant.activate",
        )
        try:
            operators_by_permission: dict[str, set[UUID]] = {}
            for permission_code in required_permissions:
                statement = (
                    select(PlatformOperatorRoleAssignment.operator_id)
                    .join(PlatformOperator, PlatformOperator.id == PlatformOperatorRoleAssignment.operator_id)
                    .join(PlatformRole, PlatformRole.id == PlatformOperatorRoleAssignment.role_id)
                    .join(PlatformRolePermission, PlatformRolePermission.role_id == PlatformRole.id)
                    .where(
                        PlatformOperator.status == "active",
                        PlatformRole.status == "active",
                        PlatformRolePermission.permission_code == permission_code,
                        PlatformRolePermission.status == "active",
                        PlatformOperatorRoleAssignment.status == "active",
                        PlatformOperatorRoleAssignment.valid_from <= now,
                        or_(
                            PlatformOperatorRoleAssignment.valid_until.is_(None),
                            PlatformOperatorRoleAssignment.valid_until > now,
                        ),
                    )
                    .distinct()
                )
                operators_by_permission[permission_code] = set(session.scalars(statement))
            create_operators = operators_by_permission["platform.onboarding.create"]
            approval_operators = operators_by_permission["platform.tenant.approve"]
            activation_operators = operators_by_permission["platform.tenant.activate"]
            return bool(
                all(operators_by_permission[code] for code in required_permissions)
                and any(
                    creator_id != reviewer_id and reviewer_id != activation_id
                    for creator_id in create_operators
                    for reviewer_id in approval_operators
                    for activation_id in activation_operators
                )
            )
        except SQLAlchemyError as exc:
            raise PlatformAuthorizationUnavailable from exc


def get_platform_identity_repository() -> SQLAlchemyPlatformIdentityRepository:
    return SQLAlchemyPlatformIdentityRepository()
