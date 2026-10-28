from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.agents.models import AgentProfile
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.agent_lifecycle.models import (
    AgentLifecycleRequest,
    LifecycleAction,
    LifecycleRequestStatus,
)
from app.modules.agent_lifecycle.schemas import (
    AgentLifecycleListResponse,
    AgentLifecycleSummary,
)
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.authorization.models import UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.relationships.models import MemberRelationship, RelationshipStatus


class AgentLifecycleService:
    def __init__(self, audit_service: AuditService | None = None) -> None:
        self._audit = audit_service or AuditService()

    def submit_lifecycle_request(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        agent_profile_id: UUID,
        action: LifecycleAction,
        reason: str,
        requester_user_id: UUID,
    ) -> AgentLifecycleSummary:
        clean_reason = reason.strip()
        if len(clean_reason) < 5:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "REASON_REQUIRED", "message": "A non-empty reason of at least 5 characters is mandatory."},
            )

        # 1. Verify agent profile exists in tenant
        profile = session.execute(
            select(AgentProfile).where(
                AgentProfile.id == agent_profile_id,
                AgentProfile.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "AGENT_NOT_FOUND", "message": "Agent profile not found in this community."},
            )

        # 2. Check current membership status
        membership = session.execute(
            select(TenantMembership).where(
                TenantMembership.id == profile.membership_id,
                TenantMembership.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "MEMBERSHIP_NOT_FOUND", "message": "Agent membership not found."},
            )

        # 3. Prevent duplicate active pending requests
        existing_pending = session.execute(
            select(AgentLifecycleRequest).where(
                AgentLifecycleRequest.tenant_id == tenant_id,
                AgentLifecycleRequest.agent_profile_id == agent_profile_id,
                AgentLifecycleRequest.status == LifecycleRequestStatus.PENDING.value,
            )
        ).scalar_one_or_none()
        if existing_pending is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "PENDING_REQUEST_EXISTS",
                    "message": f"A pending lifecycle request ({existing_pending.action}) already exists for this agent.",
                },
            )

        # 4. State transition compatibility checks
        current_status = membership.status.lower()
        if action == LifecycleAction.REACTIVATE:
            if current_status == "active":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "AGENT_ALREADY_ACTIVE", "message": "Agent is already active."},
                )
            if current_status == "revoked":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "CANNOT_REACTIVATE_REVOKED", "message": "Revoked agents cannot be reactivated."},
                )
            # Verify initial appointment was approved
            approved_apt = session.execute(
                select(AgentAppointment.id).where(
                    AgentAppointment.agent_profile_id == agent_profile_id,
                    AgentAppointment.status == "APPROVED",
                )
            ).scalar_one_or_none()
            if approved_apt is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "APPOINTMENT_NOT_APPROVED", "message": "Cannot reactivate an agent whose initial appointment was never approved."},
                )
        elif action in (LifecycleAction.SUSPEND, LifecycleAction.DEACTIVATE):
            if current_status == "revoked":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "AGENT_ALREADY_REVOKED", "message": "Agent is already revoked."},
                )
            if action == LifecycleAction.SUSPEND and current_status == "suspended":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "AGENT_ALREADY_SUSPENDED", "message": "Agent is already suspended."},
                )
            if action == LifecycleAction.DEACTIVATE and current_status == "inactive":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "AGENT_ALREADY_INACTIVE", "message": "Agent is already inactive."},
                )
        elif action == LifecycleAction.REVOKE:
            if current_status == "revoked":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "AGENT_ALREADY_REVOKED", "message": "Agent is already revoked."},
                )

        now = datetime.now(UTC)
        req_id = uuid4()
        req = AgentLifecycleRequest(
            id=req_id,
            tenant_id=tenant_id,
            agent_profile_id=agent_profile_id,
            membership_id=profile.membership_id,
            user_id=profile.user_id,
            requested_by_user_id=requester_user_id,
            action=action.value,
            status=LifecycleRequestStatus.PENDING.value,
            reason=clean_reason,
            created_at=now,
            updated_at=now,
        )
        session.add(req)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="agent.lifecycle.requested",
                actor_category="tenant_member",
                actor_ref=str(requester_user_id),
                action=f"lifecycle.{action.value.lower()}.request",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="agent_lifecycle_request",
                target_entity_ref=str(req_id),
            ),
        )

        return self._to_summary(req, profile)

    def list_tenant_requests(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        status_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AgentLifecycleListResponse:
        stmt = (
            select(AgentLifecycleRequest, AgentProfile)
            .join(AgentProfile, AgentProfile.id == AgentLifecycleRequest.agent_profile_id)
            .where(AgentLifecycleRequest.tenant_id == tenant_id)
        )
        if status_filter:
            stmt = stmt.where(AgentLifecycleRequest.status == status_filter.upper())
        stmt = stmt.order_by(AgentLifecycleRequest.created_at.desc()).limit(limit).offset(offset)
        rows = session.execute(stmt).all()

        summaries = [self._to_summary(req, prof) for req, prof in rows]
        return AgentLifecycleListResponse(requests=summaries, total=len(summaries))

    def list_platform_requests(
        self,
        session: Session,
        *,
        tenant_id: UUID | None = None,
        status_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AgentLifecycleListResponse:
        stmt = (
            select(AgentLifecycleRequest, AgentProfile)
            .join(AgentProfile, AgentProfile.id == AgentLifecycleRequest.agent_profile_id)
        )
        if tenant_id is not None:
            stmt = stmt.where(AgentLifecycleRequest.tenant_id == tenant_id)
        if status_filter:
            stmt = stmt.where(AgentLifecycleRequest.status == status_filter.upper())
        stmt = stmt.order_by(AgentLifecycleRequest.created_at.desc()).limit(limit).offset(offset)
        rows = session.execute(stmt).all()

        summaries = [self._to_summary(req, prof) for req, prof in rows]
        return AgentLifecycleListResponse(requests=summaries, total=len(summaries))

    def decide_lifecycle_request(
        self,
        session: Session,
        *,
        request_id: UUID,
        reviewer_user_id: UUID,
        decision: str,
        decision_reason: str | None = None,
    ) -> AgentLifecycleSummary:
        # Pessimistic locking to prevent conflicting concurrent decisions
        req = session.execute(
            select(AgentLifecycleRequest)
            .where(AgentLifecycleRequest.id == request_id)
            .with_for_update()
        ).scalar_one_or_none()
        if req is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "REQUEST_NOT_FOUND", "message": "Agent lifecycle request not found."},
            )

        # Terminal state conflict check
        if req.status != LifecycleRequestStatus.PENDING.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "INVALID_STATE_TRANSITION",
                    "message": f"Lifecycle request is already {req.status.lower()} and cannot be decided again.",
                },
            )

        # Anti-tampering: Prevent self-approval
        if reviewer_user_id == req.requested_by_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "SELF_DECISION_FORBIDDEN",
                    "message": "Reviewers cannot decide their own submitted lifecycle requests.",
                },
            )

        now = datetime.now(UTC)
        profile = session.execute(
            select(AgentProfile).where(AgentProfile.id == req.agent_profile_id)
        ).scalar_one_or_none()

        if decision == "APPROVED":
            req.status = LifecycleRequestStatus.APPROVED.value
            req.reviewed_by_user_id = reviewer_user_id
            req.decision_reason = decision_reason.strip() if decision_reason else None
            req.decided_at = now
            req.updated_at = now

            # Fetch target entities under lock
            user = session.execute(
                select(User).where(User.id == req.user_id).with_for_update()
            ).scalar_one_or_none()
            membership = session.execute(
                select(TenantMembership).where(TenantMembership.id == req.membership_id).with_for_update()
            ).scalar_one_or_none()
            role_assignment = session.execute(
                select(UserRoleAssignment).where(
                    UserRoleAssignment.tenant_id == req.tenant_id,
                    UserRoleAssignment.membership_id == req.membership_id,
                ).with_for_update()
            ).scalar_one_or_none()

            # Execute atomic state transition
            # User and TenantMembership schema constraints: status IN ('active', 'inactive')
            # UserRoleAssignment schema constraint: status IN ('active', 'inactive', 'revoked')
            target_membership_status = "active"
            target_role_status = "active"
            is_revoked = False

            if req.action == LifecycleAction.SUSPEND.value:
                target_membership_status = "inactive"
                target_role_status = "inactive"
            elif req.action == LifecycleAction.DEACTIVATE.value:
                target_membership_status = "inactive"
                target_role_status = "inactive"
            elif req.action == LifecycleAction.REACTIVATE.value:
                target_membership_status = "active"
                target_role_status = "active"
            elif req.action == LifecycleAction.REVOKE.value:
                target_membership_status = "inactive"
                target_role_status = "revoked"
                is_revoked = True

            if user:
                user.status = target_membership_status
                user.updated_at = now
            if membership:
                membership.status = target_membership_status
                membership.updated_at = now
            if role_assignment:
                role_assignment.status = target_role_status
                role_assignment.updated_at = now
                if is_revoked:
                    role_assignment.revoked_at = now
                else:
                    role_assignment.revoked_at = None

            # If revoked, soft-revoke all active relationships for this member
            if req.action == LifecycleAction.REVOKE.value:
                active_rels = session.execute(
                    select(MemberRelationship).where(
                        MemberRelationship.tenant_id == req.tenant_id,
                        (MemberRelationship.parent_member_id == req.membership_id)
                        | (MemberRelationship.child_member_id == req.membership_id),
                        MemberRelationship.status == RelationshipStatus.ACTIVE.value,
                    )
                ).scalars().all()
                for r in active_rels:
                    r.status = RelationshipStatus.REVOKED.value
                    r.ended_at = now
                    r.ended_by_user_id = reviewer_user_id
                    r.end_reason = f"Agent revoked via lifecycle request {req.id}"

            self._audit.record_mandatory(
                session,
                AuditEventDraft(
                    event_type=f"agent.lifecycle.{req.action.lower()}.approved",
                    actor_category="platform_operator",
                    actor_ref=str(reviewer_user_id),
                    action=f"agent.lifecycle.{req.action.lower()}.approved",
                    outcome="success",
                    tenant_id=req.tenant_id,
                    target_entity_type="agent_profile",
                    target_entity_ref=str(req.agent_profile_id),
                ),
            )
        elif decision == "REJECTED":
            req.status = LifecycleRequestStatus.REJECTED.value
            req.reviewed_by_user_id = reviewer_user_id
            req.decision_reason = decision_reason.strip() if decision_reason else None
            req.decided_at = now
            req.updated_at = now

            self._audit.record_mandatory(
                session,
                AuditEventDraft(
                    event_type=f"agent.lifecycle.{req.action.lower()}.rejected",
                    actor_category="platform_operator",
                    actor_ref=str(reviewer_user_id),
                    action=f"agent.lifecycle.{req.action.lower()}.rejected",
                    outcome="success",
                    tenant_id=req.tenant_id,
                    target_entity_type="agent_profile",
                    target_entity_ref=str(req.agent_profile_id),
                ),
            )

        session.flush()
        return self._to_summary(req, profile)

    def _to_summary(self, req: AgentLifecycleRequest, profile: AgentProfile | None) -> AgentLifecycleSummary:
        agent_name = None
        agent_ref = None
        if profile:
            agent_name = f"{profile.first_name} {profile.last_name}".strip()
            agent_ref = profile.ref_id

        return AgentLifecycleSummary(
            id=req.id,
            tenant_id=req.tenant_id,
            agent_profile_id=req.agent_profile_id,
            membership_id=req.membership_id,
            user_id=req.user_id,
            requested_by_user_id=req.requested_by_user_id,
            action=req.action,
            status=req.status,
            reason=req.reason,
            created_at=req.created_at,
            updated_at=req.updated_at,
            reviewed_by_user_id=req.reviewed_by_user_id,
            decision_reason=req.decision_reason,
            decided_at=req.decided_at,
            agent_ref_id=agent_ref,
            agent_name=agent_name,
        )


_agent_lifecycle_service: AgentLifecycleService | None = None


def get_agent_lifecycle_service() -> AgentLifecycleService:
    global _agent_lifecycle_service
    if _agent_lifecycle_service is None:
        _agent_lifecycle_service = AgentLifecycleService()
    return _agent_lifecycle_service
