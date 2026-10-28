"""Profile service handling multi-role profile resolution, authorization, and transactional updates."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.authentication.models import AccountCredential
from app.modules.authentication.otp_service import OtpService
from app.modules.authentication.repository import ResolvedSession
from app.modules.identity.models import TenantMembership, User
from app.modules.locations.models import District, ManagerLocationAssignment, Mandal, State
from app.modules.locations.service import LocationService
from app.modules.platform_identity.models import PlatformOperator
from app.modules.profiles.schemas import (
    LocationScopeItem,
    ProfileUpdateRequest,
    ProfileViewResponse,
)
from app.modules.subscriptions.models import MemberSubscription, SubscriptionStatus
from app.modules.tenants.models import AdminProfile, Tenant


_INDIAN_MOBILE_REGEX = re.compile(r"^\+91[6-9]\d{9}$")
_EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProfileService:
    @staticmethod
    def resolve_target(session: Session, target_id: UUID) -> tuple[str, Any, Any, Any, str]:
        """Resolves target profile across PlatformOperator, AdminProfile, AgentProfile, and SubscriberProfile.

        Returns (target_type, profile_obj, user_or_operator, membership, role_code).
        target_type is one of: "super_admin", "manager", "agent", "subscriber".
        """
        # 1. Platform Operator (Super Admin)
        op = session.execute(
            select(PlatformOperator).where(PlatformOperator.id == target_id)
        ).scalar_one_or_none()
        if op:
            return "super_admin", op, op, None, "SUPER_ADMIN"

        # 2. AdminProfile (Manager)
        admin_prof = session.execute(
            select(AdminProfile).where(
                (AdminProfile.id == target_id) | (AdminProfile.user_id == target_id)
            )
        ).scalar_one_or_none()
        if admin_prof:
            user = session.execute(select(User).where(User.id == admin_prof.user_id)).scalar_one_or_none()
            mem = session.execute(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == admin_prof.tenant_id,
                    TenantMembership.user_id == admin_prof.user_id,
                )
            ).scalar_one_or_none()
            return "manager", admin_prof, user, mem, "ADMIN"

        # 3. AgentProfile (Agent / Child Agent)
        agent_prof = session.execute(
            select(AgentProfile).where(
                (AgentProfile.id == target_id)
                | (AgentProfile.user_id == target_id)
                | (AgentProfile.membership_id == target_id)
            )
        ).scalar_one_or_none()
        if agent_prof:
            user = session.execute(select(User).where(User.id == agent_prof.user_id)).scalar_one_or_none()
            mem = session.execute(select(TenantMembership).where(TenantMembership.id == agent_prof.membership_id)).scalar_one_or_none()
            role = "CHILD_AGENT" if agent_prof.parent_agent_membership_id else "AGENT"
            return "agent", agent_prof, user, mem, role

        # 4. SubscriberProfile (Subscriber / Member)
        sub_prof = session.execute(
            select(SubscriberProfile).where(
                (SubscriberProfile.id == target_id)
                | (SubscriberProfile.user_id == target_id)
                | (SubscriberProfile.membership_id == target_id)
            )
        ).scalar_one_or_none()
        if sub_prof:
            user = session.execute(select(User).where(User.id == sub_prof.user_id)).scalar_one_or_none()
            mem = session.execute(select(TenantMembership).where(TenantMembership.id == sub_prof.membership_id)).scalar_one_or_none()
            return "subscriber", sub_prof, user, mem, "MEMBER"

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "PROFILE_NOT_FOUND", "message": "Target profile could not be found."},
        )

    @staticmethod
    def resolve_caller_profile(session: Session, caller: ResolvedSession) -> tuple[str, Any, Any, Any, str]:
        """Resolves current caller's profile."""
        if caller.account_type == "platform_operator":
            op = session.execute(
                select(PlatformOperator).where(PlatformOperator.id == caller.user_id)
            ).scalar_one_or_none()
            if not op:
                raise HTTPException(status_code=404, detail={"error": "OPERATOR_NOT_FOUND"})
            return "super_admin", op, op, None, "SUPER_ADMIN"

        # Tenant user: check Admin, Agent, or Subscriber
        if caller.role == "ADMIN" or caller.role == "SUPER_ADMIN":
            admin_prof = session.execute(
                select(AdminProfile).where(
                    AdminProfile.user_id == caller.user_id,
                    AdminProfile.tenant_id == caller.tenant_id,
                )
            ).scalar_one_or_none()
            if admin_prof:
                user = session.execute(select(User).where(User.id == caller.user_id)).scalar_one_or_none()
                mem = session.execute(select(TenantMembership).where(TenantMembership.id == caller.membership_id)).scalar_one_or_none()
                return "manager", admin_prof, user, mem, "ADMIN"

        if caller.role in {"AGENT", "CHILD_AGENT"}:
            agent_prof = session.execute(
                select(AgentProfile).where(
                    AgentProfile.user_id == caller.user_id,
                    AgentProfile.tenant_id == caller.tenant_id,
                )
            ).scalar_one_or_none()
            if agent_prof:
                user = session.execute(select(User).where(User.id == caller.user_id)).scalar_one_or_none()
                mem = session.execute(select(TenantMembership).where(TenantMembership.id == caller.membership_id)).scalar_one_or_none()
                role = "CHILD_AGENT" if agent_prof.parent_agent_membership_id else "AGENT"
                return "agent", agent_prof, user, mem, role

        # Subscriber / Member
        sub_prof = session.execute(
            select(SubscriberProfile).where(
                SubscriberProfile.user_id == caller.user_id,
                SubscriberProfile.tenant_id == caller.tenant_id,
            )
        ).scalar_one_or_none()
        if sub_prof:
            user = session.execute(select(User).where(User.id == caller.user_id)).scalar_one_or_none()
            mem = session.execute(select(TenantMembership).where(TenantMembership.id == caller.membership_id)).scalar_one_or_none()
            return "subscriber", sub_prof, user, mem, "MEMBER"

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "PROFILE_NOT_FOUND", "message": "Caller profile record not found."},
        )

    @classmethod
    def can_view(
        cls,
        session: Session,
        caller: ResolvedSession,
        target_type: str,
        profile_obj: Any,
        role_code: str,
    ) -> bool:
        """Determines if the caller has permission to view the target profile."""
        # Self view is always permitted
        if cls._is_self(caller, target_type, profile_obj):
            return True

        # Super Admin can view all profiles
        if caller.role == "SUPER_ADMIN":
            return True

        # Cross-community check for tenant users
        target_tenant_id = getattr(profile_obj, "tenant_id", None)
        if caller.tenant_id is None or target_tenant_id is None or caller.tenant_id != target_tenant_id:
            return False

        # Manager (ADMIN) viewing subordinates
        if caller.role == "ADMIN":
            if target_type == "agent":
                return cls._is_agent_under_manager(session, caller, profile_obj)
            if target_type == "subscriber":
                return cls._is_subscriber_under_manager(session, caller, profile_obj)
            return False

        # Agent / Child Agent viewing subordinates
        if caller.role in {"AGENT", "CHILD_AGENT"}:
            if target_type == "agent":
                # Parent Agent viewing Child Agent
                return profile_obj.parent_agent_membership_id == caller.membership_id
            if target_type == "subscriber":
                return profile_obj.registered_by_agent_membership_id == caller.membership_id
            return False

        return False

    @classmethod
    def can_edit(
        cls,
        session: Session,
        caller: ResolvedSession,
        target_type: str,
        profile_obj: Any,
        role_code: str,
    ) -> bool:
        """Determines if the caller has permission to edit the target profile."""
        is_self = cls._is_self(caller, target_type, profile_obj)
        if is_self:
            return True

        # Super Admin can edit subordinate accounts and peer Super Admins (audited)
        if caller.role == "SUPER_ADMIN":
            return True

        # Cross-community rejection for non-superadmin
        target_tenant_id = getattr(profile_obj, "tenant_id", None)
        if caller.tenant_id is None or target_tenant_id is None or caller.tenant_id != target_tenant_id:
            return False

        # Manager (ADMIN) can edit Agents under their scope
        if caller.role == "ADMIN":
            if target_type == "agent":
                return cls._is_agent_under_manager(session, caller, profile_obj)
            if target_type == "subscriber":
                return cls._is_subscriber_under_manager(session, caller, profile_obj)
            return False

        # Agent can edit subscribers registered under them, or parent Agent editing child Agent
        if caller.role in {"AGENT", "CHILD_AGENT"}:
            if target_type == "subscriber":
                return profile_obj.registered_by_agent_membership_id == caller.membership_id
            if target_type == "agent":
                return profile_obj.parent_agent_membership_id == caller.membership_id
            return False

        return False

    @staticmethod
    def _is_self(caller: ResolvedSession, target_type: str, profile_obj: Any) -> bool:
        if caller.account_type == "platform_operator":
            return target_type == "super_admin" and profile_obj.id == caller.user_id
        return getattr(profile_obj, "user_id", None) == caller.user_id

    @staticmethod
    def _is_agent_under_manager(session: Session, caller: ResolvedSession, agent_prof: AgentProfile) -> bool:
        """Verifies if an Agent is under the Manager's authority via direct assignment or territory."""
        # 1. Direct manager link
        if agent_prof.manager_user_id == caller.user_id or agent_prof.manager_membership_id == caller.membership_id:
            return True

        # 2. Geographic territory authority
        if agent_prof.state_id and agent_prof.district_id:
            return LocationService.is_manager_authorized_for_location(
                session=session,
                tenant_id=caller.tenant_id,
                manager_user_id=caller.user_id,
                state_id=agent_prof.state_id,
                district_id=agent_prof.district_id,
                mandal_id=agent_prof.mandal_id,
            )

        return False

    @classmethod
    def _is_subscriber_under_manager(cls, session: Session, caller: ResolvedSession, sub_prof: SubscriberProfile) -> bool:
        """Verifies if a Subscriber was registered by an Agent under the Manager's authority."""
        reg_agent = session.execute(
            select(AgentProfile).where(
                AgentProfile.membership_id == sub_prof.registered_by_agent_membership_id,
                AgentProfile.tenant_id == caller.tenant_id,
            )
        ).scalar_one_or_none()
        if not reg_agent:
            return False
        return cls._is_agent_under_manager(session, caller, reg_agent)

    @staticmethod
    def get_editable_fields(caller: ResolvedSession, target_type: str, is_self: bool) -> list[str]:
        """Returns the allowlist of fields editable by caller on this target profile."""
        if target_type == "super_admin":
            return ["full_name", "email", "profile_photo_key", "mobile_e164"]

        # For Manager, Agent, Child Agent, Subscriber:
        fields = [
            "first_name",
            "last_name",
            "full_name",
            "email",
            "whatsapp_number",
            "address",
            "profile_photo_key",
            "mobile_e164",
        ]
        return fields

    @classmethod
    def build_profile_response(
        cls,
        session: Session,
        caller: ResolvedSession,
        target_type: str,
        profile_obj: Any,
        user_or_operator: Any,
        membership: TenantMembership | None,
        role_code: str,
    ) -> ProfileViewResponse:
        is_self = cls._is_self(caller, target_type, profile_obj)
        editable_fields = cls.get_editable_fields(caller, target_type, is_self) if cls.can_edit(session, caller, target_type, profile_obj, role_code) else []

        # Resolve username
        username = ""
        account_id = None
        if target_type == "super_admin":
            cred = session.execute(
                select(AccountCredential).where(
                    AccountCredential.operator_id == user_or_operator.id,
                    AccountCredential.account_type == "platform_operator",
                )
            ).scalar_one_or_none()
            username = cred.username if cred else "superadmin"
            account_id = cred.id if cred else None
        else:
            cred = session.execute(
                select(AccountCredential).where(
                    AccountCredential.user_id == user_or_operator.id,
                    AccountCredential.account_type == "tenant_user",
                )
            ).scalar_one_or_none()
            username = cred.username if cred else ""
            account_id = cred.id if cred else None

        # Resolve Community info
        tenant_id = getattr(profile_obj, "tenant_id", None)
        community_name = None
        if tenant_id:
            t = session.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
            community_name = t.slug if t else None

        # Personal names & details
        first_name = getattr(profile_obj, "first_name", None)
        last_name = getattr(profile_obj, "last_name", None)
        full_name = getattr(profile_obj, "full_name", None)
        if not full_name:
            fn = (first_name or "").strip()
            ln = (last_name or "").strip()
            full_name = f"{fn} {ln}".strip() or username or "User"

        mobile_e164 = getattr(membership, "mobile_e164", None) or getattr(user_or_operator, "mobile_e164", "")
        email = getattr(profile_obj, "email", None)
        email_verified = getattr(profile_obj, "email_verified", False)
        whatsapp_number = getattr(profile_obj, "whatsapp_number", None)
        whatsapp_verified = getattr(profile_obj, "whatsapp_verified", False)
        address = getattr(profile_obj, "address", None)
        photo_key = getattr(profile_obj, "profile_photo_key", None) or getattr(profile_obj, "photo_key", None)
        photo_url = f"/api/v1/storage/preview/{photo_key}" if photo_key else None

        # Location details
        state = getattr(profile_obj, "state", None)
        district = getattr(profile_obj, "district", None)
        mandal = getattr(profile_obj, "mandal", None)
        state_id = getattr(profile_obj, "state_id", None)
        district_id = getattr(profile_obj, "district_id", None)
        mandal_id = getattr(profile_obj, "mandal_id", None)

        # For Manager: resolve assigned location scopes
        assigned_locations: list[LocationScopeItem] = []
        if target_type == "manager" and tenant_id:
            loc_rows = session.execute(
                select(ManagerLocationAssignment).where(
                    ManagerLocationAssignment.tenant_id == tenant_id,
                    ManagerLocationAssignment.user_id == user_or_operator.id,
                    ManagerLocationAssignment.status == "active",
                )
            ).scalars().all()
            for a in loc_rows:
                st = session.execute(select(State).where(State.id == a.state_id)).scalar_one_or_none()
                dt = session.execute(select(District).where(District.id == a.district_id)).scalar_one_or_none() if a.district_id else None
                md = session.execute(select(Mandal).where(Mandal.id == a.mandal_id)).scalar_one_or_none() if a.mandal_id else None
                assigned_locations.append(
                    LocationScopeItem(
                        state_id=a.state_id,
                        state_code=st.code if st else None,
                        state_name=st.name if st else None,
                        district_id=a.district_id,
                        district_code=dt.code if dt else None,
                        district_name=dt.name if dt else None,
                        mandal_id=a.mandal_id,
                        mandal_code=md.code if md else None,
                        mandal_name=md.name if md else None,
                    )
                )

        # Hierarchy details
        manager_name = None
        manager_id = getattr(profile_obj, "manager_user_id", None)
        if manager_id and tenant_id:
            m_prof = session.execute(
                select(AdminProfile).where(AdminProfile.tenant_id == tenant_id, AdminProfile.user_id == manager_id)
            ).scalar_one_or_none()
            if m_prof:
                fn = (m_prof.first_name or "").strip()
                ln = (m_prof.last_name or "").strip()
                manager_name = f"{fn} {ln}".strip() or "Manager"

        parent_agent_name = None
        parent_agent_id = getattr(profile_obj, "parent_agent_membership_id", None)
        if parent_agent_id and tenant_id:
            p_prof = session.execute(
                select(AgentProfile).where(AgentProfile.tenant_id == tenant_id, AgentProfile.membership_id == parent_agent_id)
            ).scalar_one_or_none()
            if p_prof:
                fn = (p_prof.first_name or "").strip()
                ln = (p_prof.last_name or "").strip()
                parent_agent_name = f"{fn} {ln}".strip() or "Parent Agent"

        registered_by_agent_name = None
        registered_by_agent_id = getattr(profile_obj, "registered_by_agent_membership_id", None)
        if registered_by_agent_id and tenant_id:
            r_prof = session.execute(
                select(AgentProfile).where(AgentProfile.tenant_id == tenant_id, AgentProfile.membership_id == registered_by_agent_id)
            ).scalar_one_or_none()
            if r_prof:
                fn = (r_prof.first_name or "").strip()
                ln = (r_prof.last_name or "").strip()
                registered_by_agent_name = f"{fn} {ln}".strip() or "Agent"

        # Subscription details (for subscriber)
        package_plan = getattr(profile_obj, "package_plan", None)
        billing_cycle = getattr(profile_obj, "billing_cycle", None)
        subscription_status = None
        payment_status = None
        if target_type == "subscriber" and membership:
            latest_sub = session.execute(
                select(MemberSubscription)
                .where(MemberSubscription.subscriber_membership_id == membership.id)
                .order_by(MemberSubscription.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest_sub:
                subscription_status = latest_sub.status
                payment_status = latest_sub.payment_status

        return ProfileViewResponse(
            profile_id=profile_obj.id,
            user_id=user_or_operator.id,
            account_id=account_id,
            membership_id=membership.id if membership else None,
            tenant_id=tenant_id,
            community_name=community_name,
            username=username,
            role=role_code,
            status=getattr(profile_obj, "status", getattr(user_or_operator, "status", "active")),
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            mobile_e164=mobile_e164,
            email=email,
            email_verified=email_verified,
            whatsapp_number=whatsapp_number,
            whatsapp_verified=whatsapp_verified,
            address=address,
            profile_photo_key=photo_key,
            profile_photo_url=photo_url,
            state=state,
            district=district,
            mandal=mandal,
            state_id=state_id,
            district_id=district_id,
            mandal_id=mandal_id,
            assigned_locations=assigned_locations,
            manager_name=manager_name,
            manager_id=manager_id,
            parent_agent_name=parent_agent_name,
            parent_agent_id=parent_agent_id,
            registered_by_agent_name=registered_by_agent_name,
            registered_by_agent_id=registered_by_agent_id,
            package_plan=package_plan,
            billing_cycle=billing_cycle,
            subscription_status=subscription_status,
            payment_status=payment_status,
            created_at=profile_obj.created_at,
            updated_at=profile_obj.updated_at,
            editable_fields=editable_fields,
        )

    @classmethod
    def update_profile(
        cls,
        session: Session,
        caller: ResolvedSession,
        target_type: str,
        profile_obj: Any,
        user_or_operator: Any,
        membership: TenantMembership | None,
        role_code: str,
        body: ProfileUpdateRequest,
        otp_service: OtpService,
    ) -> ProfileViewResponse:
        """Applies validated changes to target profile with atomic mobile change, duplicate check, and audit trail."""
        if not cls.can_edit(session, caller, target_type, profile_obj, role_code):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "FORBIDDEN", "message": "You are not authorized to edit this profile."},
            )

        now = datetime.now(UTC)
        changed_fields: list[str] = []

        # 1. Mobile Number Update (Requires OTP verification)
        if body.mobile_e164:
            clean_mobile = body.mobile_e164.strip()
            if not _INDIAN_MOBILE_REGEX.match(clean_mobile):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "INVALID_MOBILE", "message": "Mobile number must be a valid 10-digit Indian number (+91...)."},
                )

            current_mobile = getattr(membership, "mobile_e164", None) or getattr(user_or_operator, "mobile_e164", None)
            if clean_mobile != current_mobile:
                if not body.mobile_verification_token:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"error": "MOBILE_VERIFICATION_REQUIRED", "message": "OTP verification token is required to change mobile number."},
                    )

                # Validate and consume token
                valid = otp_service.validate_and_consume_registration_token(
                    session=session,
                    verification_token=body.mobile_verification_token,
                    mobile_number=clean_mobile,
                    expected_purpose="profile_mobile_change",
                )
                if not valid:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"error": "INVALID_VERIFICATION_TOKEN", "message": "Verification token is invalid, expired, or already used."},
                    )

                # Duplicate check
                if target_type == "super_admin":
                    dup_op = session.execute(
                        select(PlatformOperator.id).where(
                            PlatformOperator.mobile_e164 == clean_mobile,
                            PlatformOperator.id != user_or_operator.id,
                            PlatformOperator.status == "active",
                        )
                    ).scalar_one_or_none()
                    if dup_op:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={"error": "DUPLICATE_MOBILE", "message": "This mobile number is already in use by another operator."},
                        )
                    user_or_operator.mobile_e164 = clean_mobile
                else:
                    dup_mem = session.execute(
                        select(TenantMembership.id).where(
                            TenantMembership.tenant_id == membership.tenant_id,
                            TenantMembership.mobile_e164 == clean_mobile,
                            TenantMembership.id != membership.id,
                            TenantMembership.status == "active",
                        )
                    ).scalar_one_or_none()
                    if dup_mem:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={"error": "DUPLICATE_MOBILE", "message": "This mobile number is already in use in this community."},
                        )
                    membership.mobile_e164 = clean_mobile
                    membership.updated_at = now

                changed_fields.append("mobile_e164")

        # 2. Email Address
        if body.email is not None:
            clean_email = body.email.strip().lower() if body.email else None
            if clean_email and not _EMAIL_REGEX.match(clean_email):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "INVALID_EMAIL", "message": "Invalid email address format."},
                )
            if hasattr(profile_obj, "email") and profile_obj.email != clean_email:
                profile_obj.email = clean_email
                if hasattr(profile_obj, "email_verified"):
                    profile_obj.email_verified = False  # Reset verification on email change
                    profile_obj.email_verified_at = None
                changed_fields.append("email")

        # 3. Personal Names
        if body.first_name is not None and hasattr(profile_obj, "first_name"):
            clean_fn = body.first_name.strip()
            if profile_obj.first_name != clean_fn:
                profile_obj.first_name = clean_fn
                changed_fields.append("first_name")

        if body.last_name is not None and hasattr(profile_obj, "last_name"):
            clean_ln = body.last_name.strip()
            if profile_obj.last_name != clean_ln:
                profile_obj.last_name = clean_ln
                changed_fields.append("last_name")

        if body.full_name is not None and hasattr(profile_obj, "full_name"):
            clean_full = body.full_name.strip()
            if profile_obj.full_name != clean_full:
                profile_obj.full_name = clean_full
                changed_fields.append("full_name")

        # 4. WhatsApp Number
        if body.whatsapp_number is not None and hasattr(profile_obj, "whatsapp_number"):
            clean_wa = body.whatsapp_number.strip() if body.whatsapp_number else None
            if clean_wa and not _INDIAN_MOBILE_REGEX.match(clean_wa):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "INVALID_WHATSAPP", "message": "WhatsApp number must be a valid 10-digit Indian number (+91...)."},
                )
            if profile_obj.whatsapp_number != clean_wa:
                profile_obj.whatsapp_number = clean_wa
                changed_fields.append("whatsapp_number")

        # 5. Address
        if body.address is not None and hasattr(profile_obj, "address"):
            clean_addr = body.address.strip() if body.address else None
            if profile_obj.address != clean_addr:
                profile_obj.address = clean_addr
                changed_fields.append("address")

        # 6. Profile Photo Key
        if body.profile_photo_key is not None:
            clean_key = body.profile_photo_key.strip() if body.profile_photo_key else None
            if hasattr(profile_obj, "profile_photo_key"):
                if profile_obj.profile_photo_key != clean_key:
                    profile_obj.profile_photo_key = clean_key
                    changed_fields.append("profile_photo_key")
            elif hasattr(profile_obj, "photo_key"):
                if profile_obj.photo_key != clean_key:
                    profile_obj.photo_key = clean_key
                    changed_fields.append("photo_key")

        profile_obj.updated_at = now
        session.flush()

        # Record Mandatory Audit Event
        if changed_fields:
            audit = AuditService()
            actor_cat = "platform_operator" if caller.account_type == "platform_operator" else "tenant_member"
            tenant_id = getattr(profile_obj, "tenant_id", None) or caller.tenant_id
            audit.record_mandatory(
                session,
                AuditEventDraft(
                    event_type="auth.profile_updated",
                    actor_category=actor_cat,
                    actor_ref=str(caller.user_id),
                    action="profile.updated",
                    outcome="success",
                    tenant_id=tenant_id,
                    target_entity_type="user_profile",
                    target_entity_ref=str(profile_obj.id),
                    decision_metadata={
                        "changed_fields": changed_fields,
                        "target_role": role_code,
                    },
                ),
            )
            session.commit()

        return cls.build_profile_response(
            session=session,
            caller=caller,
            target_type=target_type,
            profile_obj=profile_obj,
            user_or_operator=user_or_operator,
            membership=membership,
            role_code=role_code,
        )

