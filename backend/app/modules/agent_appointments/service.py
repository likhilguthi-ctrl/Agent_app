from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import secrets
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.encryption import (
    encrypt_identity_number,
    mask_aadhaar,
    mask_pan,
    validate_aadhaar_number,
    validate_pan_number,
)
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.agent_appointments.schemas import (
    AppointmentDecisionRequest,
    AppointmentListResponse,
    AppointmentSubmitRequest,
    AppointmentSummary,
    ApprovalDecisionSummary,
)
from app.modules.agents.api import verify_and_bind_stored_object
from app.modules.agents.models import AgentProfile
from app.modules.agents.ref_id import generate_agent_ref_id
from app.modules.approval_workflows.models import ApprovalDecision
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.authentication.models import AccountCredential
from app.modules.authentication.password import hash_password
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.tenants.models import (
    CommissionAuditLog,
    Region,
    RegionAuditLog,
    Tenant,
    UserRegionAssignment,
)


class AgentAppointmentService:
    def __init__(self, audit_service: AuditService | None = None) -> None:
        self._audit = audit_service or AuditService()

    def submit_appointment(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        requested_by_user_id: UUID,
        requested_by_membership_id: UUID | None,
        data: AppointmentSubmitRequest,
        settings: Settings,
    ) -> AppointmentSummary:
        tenant = session.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        ).scalar_one_or_none()
        if tenant is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community does not exist."},
            )

        clean_username = data.username.strip().lower()
        clean_mobile = data.mobile_e164.strip()

        # Check existing credential username
        existing_username = session.execute(
            select(AccountCredential.id).where(
                AccountCredential.username == clean_username,
                AccountCredential.account_type == "tenant_user",
            )
        ).scalar_one_or_none()
        if existing_username is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "USERNAME_ALREADY_EXISTS", "message": "Username is already registered."},
            )

        # Check existing mobile in tenant
        existing_phone = session.execute(
            select(TenantMembership.id).where(
                TenantMembership.tenant_id == tenant.id,
                TenantMembership.mobile_e164 == clean_mobile,
            )
        ).scalar_one_or_none()
        if existing_phone is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "MOBILE_ALREADY_REGISTERED", "message": "Mobile is already registered in this community."},
            )

        # Validate Identity Documents
        try:
            clean_aadhaar = validate_aadhaar_number(data.aadhaar_number)
            masked_aadhaar = mask_aadhaar(clean_aadhaar)
            ciphertext_aadhaar = encrypt_identity_number(clean_aadhaar, settings)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_AADHAAR", "message": str(exc)},
            ) from exc

        try:
            clean_pan = validate_pan_number(data.pan_number)
            masked_pan = mask_pan(clean_pan)
            ciphertext_pan = encrypt_identity_number(clean_pan, settings)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_PAN", "message": str(exc)},
            ) from exc

        # Verify and bind photo and document objects
        verify_and_bind_stored_object(session, data.profile_photo_key, "agent-photo", tenant.id)
        verify_and_bind_stored_object(session, data.aadhaar_document_key, "aadhaar-document", tenant.id)
        verify_and_bind_stored_object(session, data.pan_document_key, "pan-document", tenant.id)

        # Ref ID generation
        ref_id = ""
        for _ in range(10):
            candidate = generate_agent_ref_id()
            exists = session.execute(
                select(AgentProfile.id).where(AgentProfile.ref_id == candidate)
            ).scalar_one_or_none()
            if exists is None:
                ref_id = candidate
                break
        if not ref_id:
            ref_id = generate_agent_ref_id()

        now = datetime.now(UTC)
        user_id = uuid4()
        membership_id = uuid4()
        agent_profile_id = uuid4()
        appointment_id = uuid4()

        # Role resolution - strictly restricted to canonical agent role
        role: Role | None = None
        if data.role_id is not None:
            role = session.execute(
                select(Role).where(
                    Role.id == data.role_id,
                    Role.tenant_id == tenant.id,
                )
            ).scalar_one_or_none()
            if role is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "ROLE_NOT_FOUND", "message": "Specified role not found in this community."},
                )
            if role.code != "agent":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "INVALID_APPOINTMENT_ROLE",
                        "message": "Appointments can only be submitted for the canonical agent role.",
                    },
                )
        else:
            role = session.execute(
                select(Role).where(
                    Role.tenant_id == tenant.id,
                    Role.code == "agent",
                )
            ).scalar_one_or_none()
            if role is None:
                from app.modules.authorization.seeding import seed_tenant_role_permissions
                _, role = seed_tenant_role_permissions(session, tenant.id)

        # 1. User (Pending appointment -> inactive until approved)
        user = User(
            id=user_id,
            status="inactive",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.flush()

        # 2. TenantMembership (Pending appointment -> inactive until approved)
        membership = TenantMembership(
            id=membership_id,
            tenant_id=tenant.id,
            user_id=user_id,
            mobile_e164=clean_mobile,
            status="inactive",
            created_at=now,
            updated_at=now,
        )
        session.add(membership)
        session.flush()

        # 3. UserRoleAssignment (Pending appointment -> inactive until approved)
        assignment = UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant.id,
            membership_id=membership_id,
            role_id=role.id,
            assigned_by_membership_id=requested_by_membership_id or membership_id,
            status="inactive",
            valid_from=now,
            valid_until=None,
            created_at=now,
            updated_at=now,
        )
        session.add(assignment)

        # 4. AccountCredential (Pending appointment -> inactive until approved)
        password_val = data.password or secrets.token_urlsafe(16)
        credential = AccountCredential(
            id=uuid4(),
            username=clean_username,
            password_hash=hash_password(password_val),
            account_type="tenant_user",
            user_id=user_id,
            status="inactive",
            created_at=now,
            updated_at=now,
        )
        session.add(credential)

        # 5. AgentProfile (Associated with pending appointment)
        comm_rate = data.proposed_commission_rate if data.proposed_commission_rate is not None else data.commission_rate_override
        agent_profile = AgentProfile(
            id=agent_profile_id,
            tenant_id=tenant.id,
            membership_id=membership_id,
            user_id=user_id,
            first_name=data.first_name.strip(),
            last_name=data.last_name.strip(),
            email=data.email.strip() if data.email else None,
            address=data.address.strip() if data.address else None,
            ref_id=ref_id,
            commission_rate_override=comm_rate,
            profile_photo_key=data.profile_photo_key.strip(),
            aadhaar_number_ciphertext=ciphertext_aadhaar,
            aadhaar_number_masked=masked_aadhaar,
            aadhaar_document_key=data.aadhaar_document_key.strip(),
            pan_number_ciphertext=ciphertext_pan,
            pan_number_masked=masked_pan,
            pan_document_key=data.pan_document_key.strip(),
            created_by_membership_id=requested_by_membership_id,
            created_at=now,
            updated_at=now,
        )
        session.add(agent_profile)
        session.flush()

        # 6. UserRegionAssignment (Pending appointment -> inactive until approved)
        for r_id in data.region_ids:
            reg = session.execute(
                select(Region).where(Region.id == r_id, Region.tenant_id == tenant.id)
            ).scalar_one_or_none()
            if reg is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "INVALID_REGION", "message": f"Region '{r_id}' does not belong to this community."},
                )
            session.add(
                UserRegionAssignment(
                    id=uuid4(),
                    user_id=user_id,
                    tenant_id=tenant.id,
                    region_id=r_id,
                    status="inactive",
                    assigned_by_user_id=requested_by_user_id,
                    created_at=now,
                    updated_at=now,
                )
            )

        # 7. AgentAppointment (PENDING)
        appointment = AgentAppointment(
            id=appointment_id,
            tenant_id=tenant.id,
            user_id=user_id,
            membership_id=membership_id,
            agent_profile_id=agent_profile_id,
            requested_by_user_id=requested_by_user_id,
            role_id=role.id,
            status="PENDING",
            proposed_commission_rate=comm_rate,
            notes=data.notes,
            created_at=now,
            updated_at=now,
        )
        session.add(appointment)

        # In-app notifications for Super Admin review
        from app.modules.notifications.service import InAppNotificationService
        InAppNotificationService().create_appointment_approval_notifications(
            session=session,
            appointment=appointment,
            profile=agent_profile,
            tenant=tenant,
            requested_by_user_id=requested_by_user_id,
        )

        # 8. Audit event
        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="agent.appointment.submitted",
                actor_category="tenant_member" if requested_by_membership_id else "platform_operator",
                actor_ref=str(requested_by_user_id),
                action="agent.appointment.submitted",
                outcome="success",
                tenant_id=tenant.id,
                target_entity_type="agent_appointment",
                target_entity_ref=str(appointment_id),
            ),
        )

        session.commit()

        return self._build_appointment_summary(
            appointment=appointment,
            profile=agent_profile,
            user=user,
            membership=membership,
            credential=credential,
            decisions=[],
        )

    def list_appointments(
        self,
        session: Session,
        *,
        tenant_id: UUID | None = None,
        status_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AppointmentListResponse:
        query = select(AgentAppointment)
        if tenant_id is not None:
            query = query.where(AgentAppointment.tenant_id == tenant_id)
        if status_filter:
            query = query.where(AgentAppointment.status == status_filter.upper())

        total = session.execute(
            select(func.count()).select_from(query.subquery())
        ).scalar() or 0

        appointments = session.execute(
            query.order_by(AgentAppointment.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()

        items = [self._fetch_summary(session, appt) for appt in appointments]
        return AppointmentListResponse(items=items, total=total)

    def get_appointment(
        self,
        session: Session,
        *,
        appointment_id: UUID,
        caller_tenant_id: UUID | None = None,
    ) -> AppointmentSummary:
        appointment = session.execute(
            select(AgentAppointment).where(AgentAppointment.id == appointment_id)
        ).scalar_one_or_none()
        if appointment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "APPOINTMENT_NOT_FOUND", "message": "Agent appointment not found."},
            )

        if caller_tenant_id is not None and appointment.tenant_id != caller_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "CROSS_TENANT_APPOINTMENT_FORBIDDEN",
                    "message": "Appointment belongs to another community.",
                },
            )

        return self._fetch_summary(session, appointment)

    def approve_appointment(
        self,
        session: Session,
        *,
        appointment_id: UUID,
        reviewer_user_id: UUID,
        reviewer_membership_id: UUID | None,
        caller_tenant_id: UUID | None = None,
        data: AppointmentDecisionRequest | None = None,
    ) -> AppointmentSummary:
        appointment = session.execute(
            select(AgentAppointment).where(AgentAppointment.id == appointment_id).with_for_update()
        ).scalar_one_or_none()
        if appointment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "APPOINTMENT_NOT_FOUND", "message": "Agent appointment not found."},
            )

        # Cross-tenant boundary check
        if caller_tenant_id is not None and appointment.tenant_id != caller_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "CROSS_TENANT_APPOINTMENT_FORBIDDEN",
                    "message": "Appointment belongs to another community.",
                },
            )

        # Self-approval prohibition (Architecture 10.4)
        if appointment.requested_by_user_id == reviewer_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "SELF_APPROVAL_PROHIBITED",
                    "message": "Reviewers cannot approve appointments they requested.",
                },
            )

        # Terminal state conflict check (Architecture 10.5)
        if appointment.status != "PENDING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "INVALID_STATE_TRANSITION",
                    "message": f"Appointment is already {appointment.status.lower()} and cannot be approved.",
                },
            )

        # Verify role eligibility at approval time (SEC-03)
        role = session.execute(
            select(Role).where(
                Role.id == appointment.role_id,
                Role.tenant_id == appointment.tenant_id,
            )
        ).scalar_one_or_none()
        if role is None or role.code != "agent":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "INVALID_APPOINTMENT_ROLE",
                    "message": "Appointment does not reference an eligible agent role.",
                },
            )

        now = datetime.now(UTC)

        # Atomic transition to APPROVED
        appointment.status = "APPROVED"
        appointment.updated_at = now

        decision = ApprovalDecision(
            id=uuid4(),
            tenant_id=appointment.tenant_id,
            appointment_id=appointment.id,
            decision="APPROVED",
            decided_by_user_id=reviewer_user_id,
            reason=data.reason if data else None,
            decided_at=now,
        )
        session.add(decision)

        # Activate User
        user = session.execute(
            select(User).where(User.id == appointment.user_id)
        ).scalar_one_or_none()
        if user:
            user.status = "active"
            user.updated_at = now

        # Activate TenantMembership
        membership = session.execute(
            select(TenantMembership).where(TenantMembership.id == appointment.membership_id)
        ).scalar_one_or_none()
        if membership:
            membership.status = "active"
            membership.updated_at = now

        # Activate AccountCredential
        credential = session.execute(
            select(AccountCredential).where(
                AccountCredential.user_id == appointment.user_id,
                AccountCredential.account_type == "tenant_user",
            )
        ).scalar_one_or_none()
        if credential:
            credential.status = "active"
            credential.updated_at = now

        # Update AgentProfile
        profile = session.execute(
            select(AgentProfile).where(AgentProfile.id == appointment.agent_profile_id)
        ).scalar_one_or_none()
        if profile:
            profile.updated_at = now

        # Activate UserRoleAssignment
        assignment = session.execute(
            select(UserRoleAssignment).where(
                UserRoleAssignment.membership_id == appointment.membership_id,
                UserRoleAssignment.role_id == appointment.role_id,
            )
        ).scalar_one_or_none()
        if assignment:
            assignment.status = "active"
            assignment.updated_at = now

        # Activate UserRegionAssignments and log RegionAuditLog
        region_assignments = session.execute(
            select(UserRegionAssignment).where(
                UserRegionAssignment.user_id == appointment.user_id,
                UserRegionAssignment.tenant_id == appointment.tenant_id,
            )
        ).scalars().all()
        for r_assign in region_assignments:
            r_assign.status = "active"
            r_assign.updated_at = now
            session.add(
                RegionAuditLog(
                    id=uuid4(),
                    tenant_id=appointment.tenant_id,
                    user_id=appointment.user_id,
                    region_id=r_assign.region_id,
                    action="assigned",
                    actor_id=reviewer_user_id,
                    reason="appointment_approved",
                    created_at=now,
                )
            )

        # Commission Audit Log if proposed commission rate was set
        if appointment.proposed_commission_rate is not None and profile:
            session.add(
                CommissionAuditLog(
                    id=uuid4(),
                    tenant_id=appointment.tenant_id,
                    agent_profile_id=profile.id,
                    changed_by_user_id=reviewer_user_id,
                    changed_by_username="reviewer",
                    scope="agent",
                    previous_rate=None,
                    new_rate=appointment.proposed_commission_rate,
                    created_at=now,
                )
            )

        # Audit event
        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="agent.appointment.approved",
                actor_category="tenant_member" if reviewer_membership_id else "platform_operator",
                actor_ref=str(reviewer_user_id),
                action="agent.appointment.approved",
                outcome="success",
                tenant_id=appointment.tenant_id,
                target_entity_type="agent_appointment",
                target_entity_ref=str(appointment.id),
            ),
        )

        # Resolve associated in-app notifications
        from app.modules.notifications.service import InAppNotificationService
        InAppNotificationService().resolve_appointment_notifications(
            session=session,
            appointment_id=appointment.id,
            resolution_status="APPROVED",
        )

        session.commit()
        return self._fetch_summary(session, appointment)

    def reject_appointment(
        self,
        session: Session,
        *,
        appointment_id: UUID,
        reviewer_user_id: UUID,
        reviewer_membership_id: UUID | None,
        caller_tenant_id: UUID | None = None,
        data: AppointmentDecisionRequest | None = None,
    ) -> AppointmentSummary:
        appointment = session.execute(
            select(AgentAppointment).where(AgentAppointment.id == appointment_id).with_for_update()
        ).scalar_one_or_none()
        if appointment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "APPOINTMENT_NOT_FOUND", "message": "Agent appointment not found."},
            )

        if caller_tenant_id is not None and appointment.tenant_id != caller_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "CROSS_TENANT_APPOINTMENT_FORBIDDEN",
                    "message": "Appointment belongs to another community.",
                },
            )

        # Terminal state conflict check
        if appointment.status != "PENDING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "INVALID_STATE_TRANSITION",
                    "message": f"Appointment is already {appointment.status.lower()} and cannot be rejected.",
                },
            )

        now = datetime.now(UTC)

        # Transition to REJECTED
        appointment.status = "REJECTED"
        appointment.updated_at = now

        decision = ApprovalDecision(
            id=uuid4(),
            tenant_id=appointment.tenant_id,
            appointment_id=appointment.id,
            decision="REJECTED",
            decided_by_user_id=reviewer_user_id,
            reason=data.reason if data else None,
            decided_at=now,
        )
        session.add(decision)

        # Explicitly mark records inactive/revoked
        user = session.execute(
            select(User).where(User.id == appointment.user_id)
        ).scalar_one_or_none()
        if user:
            user.status = "inactive"
            user.updated_at = now

        membership = session.execute(
            select(TenantMembership).where(TenantMembership.id == appointment.membership_id)
        ).scalar_one_or_none()
        if membership:
            membership.status = "inactive"
            membership.updated_at = now

        credential = session.execute(
            select(AccountCredential).where(
                AccountCredential.user_id == appointment.user_id,
                AccountCredential.account_type == "tenant_user",
            )
        ).scalar_one_or_none()
        if credential:
            credential.status = "inactive"
            credential.updated_at = now

        profile = session.execute(
            select(AgentProfile).where(AgentProfile.id == appointment.agent_profile_id)
        ).scalar_one_or_none()
        if profile:
            profile.updated_at = now

        assignment = session.execute(
            select(UserRoleAssignment).where(
                UserRoleAssignment.membership_id == appointment.membership_id,
                UserRoleAssignment.role_id == appointment.role_id,
            )
        ).scalar_one_or_none()
        if assignment:
            assignment.status = "revoked"
            assignment.revoked_at = now
            assignment.updated_at = now

        region_assignments = session.execute(
            select(UserRegionAssignment).where(
                UserRegionAssignment.user_id == appointment.user_id,
                UserRegionAssignment.tenant_id == appointment.tenant_id,
            )
        ).scalars().all()
        for r_assign in region_assignments:
            r_assign.status = "inactive"
            r_assign.updated_at = now

        # Audit event
        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="agent.appointment.rejected",
                actor_category="tenant_member" if reviewer_membership_id else "platform_operator",
                actor_ref=str(reviewer_user_id),
                action="agent.appointment.rejected",
                outcome="success",
                tenant_id=appointment.tenant_id,
                target_entity_type="agent_appointment",
                target_entity_ref=str(appointment.id),
            ),
        )

        # Resolve associated in-app notifications
        from app.modules.notifications.service import InAppNotificationService
        InAppNotificationService().resolve_appointment_notifications(
            session=session,
            appointment_id=appointment.id,
            resolution_status="REJECTED",
        )

        session.commit()
        return self._fetch_summary(session, appointment)

    def cancel_appointment(
        self,
        session: Session,
        *,
        appointment_id: UUID,
        user_id: UUID,
        caller_tenant_id: UUID | None = None,
        data: AppointmentDecisionRequest | None = None,
    ) -> AppointmentSummary:
        appointment = session.execute(
            select(AgentAppointment).where(AgentAppointment.id == appointment_id).with_for_update()
        ).scalar_one_or_none()
        if appointment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "APPOINTMENT_NOT_FOUND", "message": "Agent appointment not found."},
            )

        if caller_tenant_id is not None and appointment.tenant_id != caller_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "CROSS_TENANT_APPOINTMENT_FORBIDDEN",
                    "message": "Appointment belongs to another community.",
                },
            )

        if appointment.status != "PENDING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "INVALID_STATE_TRANSITION",
                    "message": f"Appointment is already {appointment.status.lower()} and cannot be cancelled.",
                },
            )

        now = datetime.now(UTC)
        appointment.status = "CANCELLED"
        appointment.updated_at = now

        # Resolve associated in-app notifications
        from app.modules.notifications.service import InAppNotificationService
        InAppNotificationService().resolve_appointment_notifications(
            session=session,
            appointment_id=appointment.id,
            resolution_status="CANCELLED",
        )

        session.commit()
        return self._fetch_summary(session, appointment)

    def _fetch_summary(self, session: Session, appointment: AgentAppointment) -> AppointmentSummary:
        profile = session.execute(
            select(AgentProfile).where(AgentProfile.id == appointment.agent_profile_id)
        ).scalar_one_or_none()
        user = session.execute(
            select(User).where(User.id == appointment.user_id)
        ).scalar_one_or_none()
        credential = session.execute(
            select(AccountCredential).where(
                AccountCredential.user_id == appointment.user_id,
                AccountCredential.account_type == "tenant_user",
            )
        ).scalar_one_or_none()
        membership = session.execute(
            select(TenantMembership).where(TenantMembership.id == appointment.membership_id)
        ).scalar_one_or_none()
        decisions = session.execute(
            select(ApprovalDecision)
            .where(ApprovalDecision.appointment_id == appointment.id)
            .order_by(ApprovalDecision.decided_at.asc())
        ).scalars().all()

        return self._build_appointment_summary(
            appointment=appointment,
            profile=profile,
            user=user,
            membership=membership,
            credential=credential,
            decisions=decisions,
        )

    def _build_appointment_summary(
        self,
        *,
        appointment: AgentAppointment,
        profile: AgentProfile | None,
        user: User | None,
        membership: TenantMembership | None = None,
        credential: AccountCredential | None,
        decisions: list[ApprovalDecision],
    ) -> AppointmentSummary:
        candidate_name = None
        if profile:
            candidate_name = f"{profile.first_name} {profile.last_name}".strip()

        return AppointmentSummary(
            id=appointment.id,
            tenant_id=appointment.tenant_id,
            user_id=appointment.user_id,
            membership_id=appointment.membership_id,
            agent_profile_id=appointment.agent_profile_id,
            requested_by_user_id=appointment.requested_by_user_id,
            role_id=appointment.role_id,
            status=appointment.status,
            proposed_commission_rate=appointment.proposed_commission_rate,
            notes=appointment.notes,
            created_at=appointment.created_at,
            updated_at=appointment.updated_at,
            candidate_name=candidate_name,
            candidate_username=credential.username if credential else None,
            candidate_mobile=membership.mobile_e164 if membership else None,
            candidate_ref_id=profile.ref_id if profile else None,
            decisions=[
                ApprovalDecisionSummary(
                    id=d.id,
                    tenant_id=d.tenant_id,
                    appointment_id=d.appointment_id,
                    decision=d.decision,
                    decided_by_user_id=d.decided_by_user_id,
                    reason=d.reason,
                    decided_at=d.decided_at,
                )
                for d in decisions
            ],
        )
