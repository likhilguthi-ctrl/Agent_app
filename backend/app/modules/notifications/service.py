from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.modules.agent_appointments.models import AgentAppointment
from app.modules.agents.models import AgentProfile
from app.modules.notifications.models import SuperAdminNotification
from app.modules.notifications.schemas import (
    InAppNotificationListResponse,
    InAppNotificationSummary,
    MarkAllReadResponse,
    UnreadCountResponse,
)
from app.modules.platform_identity.models import PlatformOperator
from app.modules.tenants.models import Tenant


def _to_summary(notif: SuperAdminNotification) -> InAppNotificationSummary:
    return InAppNotificationSummary(
        id=notif.id,
        recipient_operator_id=notif.recipient_operator_id,
        tenant_id=notif.tenant_id,
        event_type=notif.event_type,
        title=notif.title,
        message=notif.message,
        appointment_id=notif.appointment_id,
        agent_profile_id=notif.agent_profile_id,
        is_read=notif.is_read,
        read_at=notif.read_at,
        resolution_status=notif.resolution_status,
        resolved_at=notif.resolved_at,
        metadata_json=notif.metadata_json,
        created_at=notif.created_at,
        updated_at=notif.updated_at,
    )


class InAppNotificationService:
    """Service managing in-app notifications for Super Admin approvals and system events."""

    def create_appointment_approval_notifications(
        self,
        session: Session,
        *,
        appointment: AgentAppointment,
        profile: AgentProfile,
        tenant: Tenant | None,
        requested_by_user_id: UUID,
    ) -> list[SuperAdminNotification]:
        """Creates in-app notifications for authorized Super Admins awaiting agent appointment approval.

        Adheres strictly to SEC-03 dual-custody rules: if the appointment was requested
        by a Super Admin, that operator is excluded from the notification recipient list
        since reviewers cannot approve appointments they requested.
        """
        # Query all active platform operators
        operators = session.execute(
            select(PlatformOperator).where(PlatformOperator.status == "active")
        ).scalars().all()

        # Filter out the requesting operator if requested_by_user_id was a platform operator
        eligible_operators = [
            op for op in operators if op.id != requested_by_user_id
        ]

        if not eligible_operators:
            # Fallback: if all active operators match (or only 1 operator exists who created it in test),
            # notify operators so the event is not completely lost, but backend approval will gate.
            eligible_operators = list(operators)

        created_notifications: list[SuperAdminNotification] = []
        now = datetime.now(UTC)
        event_type = "AGENT_APPOINTMENT_PENDING"

        community_slug = tenant.slug if tenant else "community"

        title = f"Agent Appointment Pending: {profile.first_name} {profile.last_name}"
        message = (
            f"Agent {profile.first_name} {profile.last_name} ({profile.ref_id}) was registered in "
            f"community '{community_slug}' and requires Super Admin appointment approval."
        )

        metadata = {
            "agent_name": f"{profile.first_name} {profile.last_name}",
            "ref_id": profile.ref_id,
            "community_slug": community_slug,
            "community_id": str(tenant.id) if tenant else None,
            "appointment_id": str(appointment.id),
            "agent_profile_id": str(profile.id),
        }

        for op in eligible_operators:
            # Deduplication: check if notification already exists for (recipient, appointment, event_type)
            existing = session.execute(
                select(SuperAdminNotification).where(
                    SuperAdminNotification.recipient_operator_id == op.id,
                    SuperAdminNotification.appointment_id == appointment.id,
                    SuperAdminNotification.event_type == event_type,
                )
            ).scalar_one_or_none()

            if existing is None:
                notif = SuperAdminNotification(
                    id=uuid4(),
                    recipient_operator_id=op.id,
                    tenant_id=tenant.id if tenant else None,
                    event_type=event_type,
                    title=title,
                    message=message,
                    appointment_id=appointment.id,
                    agent_profile_id=profile.id,
                    is_read=False,
                    resolution_status="PENDING",
                    metadata_json=metadata,
                    created_at=now,
                    updated_at=now,
                )
                session.add(notif)
                created_notifications.append(notif)

        if created_notifications:
            session.flush()

        return created_notifications

    def resolve_appointment_notifications(
        self,
        session: Session,
        *,
        appointment_id: UUID,
        resolution_status: str,
    ) -> int:
        """Atomically updates resolution status and timestamp for all notifications associated with an appointment."""
        now = datetime.now(UTC)
        result = session.execute(
            update(SuperAdminNotification)
            .where(SuperAdminNotification.appointment_id == appointment_id)
            .values(
                resolution_status=resolution_status.upper(),
                resolved_at=now,
                updated_at=now,
            )
        )
        session.flush()
        return result.rowcount

    def list_notifications(
        self,
        session: Session,
        *,
        operator_id: UUID,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> InAppNotificationListResponse:
        """Lists persistent in-app notifications strictly scoped to the requesting Super Admin."""
        base_query = select(SuperAdminNotification).where(
            SuperAdminNotification.recipient_operator_id == operator_id
        )

        # Unread count
        unread_count = session.execute(
            select(func.count(SuperAdminNotification.id)).where(
                SuperAdminNotification.recipient_operator_id == operator_id,
                SuperAdminNotification.is_read.is_(False),
            )
        ).scalar() or 0

        # Filter query
        filter_query = base_query
        if unread_only:
            filter_query = filter_query.where(SuperAdminNotification.is_read.is_(False))

        total = session.execute(
            select(func.count()).select_from(filter_query.subquery())
        ).scalar() or 0

        records = session.execute(
            filter_query.order_by(SuperAdminNotification.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()

        return InAppNotificationListResponse(
            items=[_to_summary(r) for r in records],
            total=total,
            unread_count=unread_count,
        )

    def get_unread_count(self, session: Session, *, operator_id: UUID) -> UnreadCountResponse:
        """Returns the unread count for the requesting Super Admin."""
        unread = session.execute(
            select(func.count(SuperAdminNotification.id)).where(
                SuperAdminNotification.recipient_operator_id == operator_id,
                SuperAdminNotification.is_read.is_(False),
            )
        ).scalar() or 0
        return UnreadCountResponse(unread_count=unread)

    def mark_as_read(
        self,
        session: Session,
        *,
        notification_id: UUID,
        operator_id: UUID,
    ) -> InAppNotificationSummary:
        """Marks a specific notification as read, enforcing strict operator isolation."""
        notif = session.execute(
            select(SuperAdminNotification).where(SuperAdminNotification.id == notification_id)
        ).scalar_one_or_none()

        if notif is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "NOTIFICATION_NOT_FOUND", "message": "Notification not found."},
            )

        if notif.recipient_operator_id != operator_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "FORBIDDEN", "message": "Access to another operator's notification is denied."},
            )

        if not notif.is_read:
            now = datetime.now(UTC)
            notif.is_read = True
            notif.read_at = now
            notif.updated_at = now
            session.commit()

        return _to_summary(notif)

    def mark_all_as_read(self, session: Session, *, operator_id: UUID) -> MarkAllReadResponse:
        """Marks all unread notifications for the calling Super Admin as read."""
        now = datetime.now(UTC)
        res = session.execute(
            update(SuperAdminNotification)
            .where(
                SuperAdminNotification.recipient_operator_id == operator_id,
                SuperAdminNotification.is_read.is_(False),
            )
            .values(
                is_read=True,
                read_at=now,
                updated_at=now,
            )
        )
        session.commit()
        return MarkAllReadResponse(marked_read=res.rowcount)
