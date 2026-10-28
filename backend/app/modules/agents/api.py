from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.encryption import (
    encrypt_identity_number,
    mask_aadhaar,
    mask_pan,
    validate_aadhaar_number,
    validate_pan_number,
)
import secrets
from app.db.session import get_db
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.models import AccountCredential
from app.modules.authentication.otp_service import OtpService
from app.modules.authentication.password import hash_password
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.relationships.models import MemberRelationship, RelationshipStatus, RelationshipType
from app.modules.agent_appointments.models import AgentAppointment
from app.modules.agents.ref_id import generate_agent_ref_id
from app.modules.agents.schemas import (
    AgentCollectionSummary,
    AgentCreateRequest,
    AgentListResponse,
    AgentSummary,
    AgentUpdateRequest,
    CommissionAuditListResponse,
    CommissionAuditSummary,
    CommissionUpdateRequest,
    CommunityPolicyResponse,
    PaymentConfirmRequest,
    PaymentConfirmResponse,
    PhotoPolicyUpdateRequest,
    SubscriberPaymentInitiationResponse,
    UserListResponse,
    UserRegistrationRequest,
    UserSummary,
)
from app.modules.subscriptions.models import (
    MemberSubscription,
    SubscriptionPaymentStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.payments.models import PaymentOrder, PaymentOrderStatus
from app.modules.payments.microservice_client import PaymentMicroserviceClient
from app.modules.messaging.notification_service import NotificationServiceClient
from pydantic import BaseModel
from app.modules.registration_photos.models import StoredObject
from app.modules.tenants.locations import (
    CANONICAL_LOCATIONS,
    SUPPORTED_STATES,
    get_or_create_region_for_district,
    normalize_district_name,
    seed_canonical_regions_for_tenant,
    validate_state_district,
)
from app.modules.tenants.models import (
    AdminProfile,
    CommissionAuditLog,
    Region,
    RegionAuditLog,
    Tenant,
    UserRegionAssignment,
)
from app.modules.locations.models import District, Mandal, ManagerLocationAssignment, State
from app.modules.locations.service import LocationService

router = APIRouter(tags=["agents and subscribers"])
audit_service = AuditService()


def verify_and_bind_stored_object(
    session: Session,
    object_key: str,
    expected_category: str,
    tenant_id: UUID,
) -> StoredObject:
    stored = session.execute(
        select(StoredObject).where(StoredObject.object_key == object_key)
    ).scalar_one_or_none()
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "OBJECT_NOT_FOUND",
                "message": f"Uploaded object key '{object_key}' does not exist.",
            },
        )
    if stored.category != expected_category:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "INVALID_OBJECT_CATEGORY",
                "message": f"Expected object category '{expected_category}', found '{stored.category}'.",
            },
        )
    if stored.tenant_id is not None and stored.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "CROSS_TENANT_OBJECT_FORBIDDEN",
                "message": "Uploaded object belongs to another community.",
            },
        )
    if stored.tenant_id is None:
        stored.tenant_id = tenant_id
    return stored


class RegionSummary(BaseModel):
    id: UUID
    code: str
    name: str
    state: str = "Andhra Pradesh"
    description: str | None = None
    status: str = "active"


class RegionListResponse(BaseModel):
    items: list[RegionSummary]
    total: int


class StateListResponse(BaseModel):
    items: list[str]
    total: int


@router.get(
    "/agents/regions",
    response_model=RegionListResponse,
    summary="List authorized operating regions for agent registration",
)
@router.get(
    "/community/regions",
    response_model=RegionListResponse,
    summary="List operating regions in current community",
)
def list_available_regions(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.REGION_READ))],
    session: Annotated[Session, Depends(get_db)],
    community_id: UUID | None = Query(None),
    state: str | None = Query(None),
) -> RegionListResponse:
    if community_id is not None and caller.role != "SUPER_ADMIN":
        if caller.tenant_id != community_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_TENANT_FORBIDDEN", "message": "Cannot access another community's regions."},
            )
    tenant_id = caller.tenant_id if caller.role != "SUPER_ADMIN" else (community_id or caller.tenant_id)
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "COMMUNITY_REQUIRED", "message": "Community ID is required."},
        )

    # Ensure canonical regions exist
    reg_count = session.execute(
        select(func.count(Region.id)).where(Region.tenant_id == tenant_id)
    ).scalar() or 0
    if reg_count == 0:
        seed_canonical_regions_for_tenant(session, tenant_id)

    allowed_region_ids: set[UUID] | None = None
    if caller.role == "ADMIN":
        admin_regions = session.execute(
            select(UserRegionAssignment.region_id).where(
                UserRegionAssignment.user_id == caller.user_id,
                UserRegionAssignment.tenant_id == tenant_id,
                UserRegionAssignment.status == "active",
            )
        ).scalars().all()
        allowed_region_ids = set(admin_regions)

    query = select(Region).where(Region.tenant_id == tenant_id, Region.status == "active")
    if allowed_region_ids is not None:
        if not allowed_region_ids:
            return RegionListResponse(items=[], total=0)
        query = query.where(Region.id.in_(allowed_region_ids))

    if state:
        query = query.where(Region.state == state)

    regions = session.execute(query.order_by(Region.name.asc())).scalars().all()
    items = [
        RegionSummary(
            id=r.id,
            code=r.code,
            name=r.name,
            state=r.state,
            description=r.description,
            status=r.status,
        )
        for r in regions
    ]
    return RegionListResponse(items=items, total=len(items))


@router.get(
    "/agents/states",
    response_model=StateListResponse,
    summary="List authorized operating states for agent registration",
)
@router.get(
    "/community/states",
    response_model=StateListResponse,
    summary="List operating states in current community",
)
def list_available_states(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.REGION_READ))],
    session: Annotated[Session, Depends(get_db)],
    community_id: UUID | None = Query(None),
) -> StateListResponse:
    if community_id is not None and caller.role != "SUPER_ADMIN":
        if caller.tenant_id != community_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_TENANT_FORBIDDEN", "message": "Cannot access another community's regions."},
            )
    tenant_id = caller.tenant_id if caller.role != "SUPER_ADMIN" else (community_id or caller.tenant_id)
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "COMMUNITY_REQUIRED", "message": "Community ID is required."},
        )

    reg_count = session.execute(
        select(func.count(Region.id)).where(Region.tenant_id == tenant_id)
    ).scalar() or 0
    if reg_count == 0:
        seed_canonical_regions_for_tenant(session, tenant_id)

    if caller.role == "SUPER_ADMIN":
        return StateListResponse(items=SUPPORTED_STATES, total=len(SUPPORTED_STATES))

    # For ADMIN, distinct states from their active region assignments
    admin_states = session.execute(
        select(Region.state)
        .join(
            UserRegionAssignment,
            (UserRegionAssignment.region_id == Region.id) & (UserRegionAssignment.tenant_id == tenant_id),
        )
        .where(
            UserRegionAssignment.user_id == caller.user_id,
            UserRegionAssignment.status == "active",
            Region.status == "active",
        )
        .distinct()
        .order_by(Region.state.asc())
    ).scalars().all()

    return StateListResponse(items=list(admin_states), total=len(admin_states))


@router.get(
    "/platform/communities/{community_id}/regions",
    response_model=RegionListResponse,
    summary="Super Admin: List regions in a community",
)
def superadmin_list_community_regions(
    community_id: UUID,
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    state: str | None = Query(None),
) -> RegionListResponse:
    reg_count = session.execute(
        select(func.count(Region.id)).where(Region.tenant_id == community_id)
    ).scalar() or 0
    if reg_count == 0:
        seed_canonical_regions_for_tenant(session, community_id)

    query = select(Region).where(Region.tenant_id == community_id, Region.status == "active")
    if state:
        query = query.where(Region.state == state)

    regions = session.execute(query.order_by(Region.name.asc())).scalars().all()
    items = [
        RegionSummary(
            id=r.id,
            code=r.code,
            name=r.name,
            state=r.state,
            description=r.description,
            status=r.status,
        )
        for r in regions
    ]
    return RegionListResponse(items=items, total=len(items))


@router.get(
    "/platform/communities/{community_id}/states",
    response_model=StateListResponse,
    summary="Super Admin: List states in a community",
)
def superadmin_list_community_states(
    community_id: UUID,
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> StateListResponse:
    return StateListResponse(items=SUPPORTED_STATES, total=len(SUPPORTED_STATES))


def require_super_admin(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
) -> ResolvedSession:
    resolved, _ = session_data
    if resolved.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "SUPER_ADMIN_REQUIRED", "message": "Super Admin privileges required."},
        )
    return resolved


def require_admin_or_super(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
) -> ResolvedSession:
    resolved, _ = session_data
    if resolved.role not in ("SUPER_ADMIN", "ADMIN"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "ADMIN_REQUIRED", "message": "Admin privileges required."},
        )
    return resolved


def require_agent(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
) -> ResolvedSession:
    resolved, _ = session_data
    if resolved.role != "AGENT":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "AGENT_REQUIRED", "message": "Field Agent privileges required."},
        )
    return resolved


def get_effective_commission(override: Decimal | None, community_default: Decimal | None) -> Decimal:
    if override is not None:
        return override
    if community_default is not None:
        return community_default
    return Decimal("10.00")


def build_agent_summary(
    profile: AgentProfile,
    username: str,
    mobile: str,
    status_val: str,
    slug: str,
    default_comm: Decimal,
    assigned_region_ids: list[UUID] | None = None,
    session: Session | None = None,
    pending_appointment_id: UUID | None = None,
    state: str | None = None,
) -> AgentSummary:
    effective = get_effective_commission(profile.commission_rate_override, default_comm)
    if assigned_region_ids is None and session is not None:
        assigned_region_ids = list(
            session.execute(
                select(UserRegionAssignment.region_id).where(
                    UserRegionAssignment.user_id == profile.user_id,
                    UserRegionAssignment.tenant_id == profile.tenant_id,
                    UserRegionAssignment.status == "active",
                )
            ).scalars().all()
        )
    if pending_appointment_id is None and session is not None:
        pending_apt_id = session.execute(
            select(AgentAppointment.id).where(
                AgentAppointment.agent_profile_id == profile.id,
                AgentAppointment.status == "PENDING",
            )
        ).scalar_one_or_none()
        if pending_apt_id is not None:
            pending_appointment_id = pending_apt_id

    if pending_appointment_id is not None:
        status_val = "pending"

    if state is None and session is not None and assigned_region_ids:
        state = session.execute(
            select(Region.state).where(Region.id.in_(assigned_region_ids)).limit(1)
        ).scalar_one_or_none()

    manager_display_name = None
    if getattr(profile, "manager_user_id", None) and session is not None:
        m_admin = session.execute(
            select(AdminProfile).where(
                AdminProfile.tenant_id == profile.tenant_id,
                AdminProfile.user_id == profile.manager_user_id,
            )
        ).scalar_one_or_none()
        if m_admin:
            manager_display_name = f"{m_admin.first_name or ''} {m_admin.last_name or ''}".strip() or "Manager"

    return AgentSummary(
        id=profile.id,
        tenant_id=profile.tenant_id,
        community_slug=slug,
        membership_id=profile.membership_id,
        user_id=profile.user_id,
        username=username,
        first_name=profile.first_name,
        last_name=profile.last_name,
        mobile_e164=mobile,
        whatsapp_number=getattr(profile, "whatsapp_number", None) or mobile,
        whatsapp_verified=getattr(profile, "whatsapp_verified", False) or False,
        email=profile.email,
        email_verified=getattr(profile, "email_verified", False) or False,
        address=profile.address,
        state=getattr(profile, "state", None) or state,
        district=getattr(profile, "district", None),
        mandal=getattr(profile, "mandal", None),
        state_id=getattr(profile, "state_id", None),
        district_id=getattr(profile, "district_id", None),
        mandal_id=getattr(profile, "mandal_id", None),
        manager_user_id=getattr(profile, "manager_user_id", None),
        manager_name=manager_display_name,
        parent_agent_membership_id=getattr(profile, "parent_agent_membership_id", None),
        ref_id=profile.ref_id,
        status=status_val,
        effective_commission_rate=effective,
        commission_rate_override=profile.commission_rate_override,
        profile_photo_key=profile.profile_photo_key,
        aadhaar_number_masked=profile.aadhaar_number_masked,
        aadhaar_document_key=profile.aadhaar_document_key,
        pan_number_masked=profile.pan_number_masked,
        pan_document_key=profile.pan_document_key,
        assigned_region_ids=assigned_region_ids or [],
        created_at=profile.created_at,
        pending_appointment_id=pending_appointment_id,
    )


# -----------------------------------------------------------------------------
# 1. Super Admin: Create Agent for a Community
# -----------------------------------------------------------------------------
@router.post(
    "/platform/communities/{community_id}/agents",
    response_model=AgentSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Super Admin: Register a new Agent in a community",
)
def superadmin_create_agent(
    community_id: UUID,
    body: AgentCreateRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> AgentSummary:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    settings = getattr(request.app.state, "settings", None)
    enc_key = getattr(settings, "identity_encryption_key", None) if settings else None

    return _create_agent_in_community(
        session=session,
        tenant=tenant,
        body=body,
        created_by_user_id=operator.user_id,
        created_by_username=operator.username or "superadmin",
        created_by_membership_id=None,
        encryption_key=enc_key,
        settings=settings,
    )


# -----------------------------------------------------------------------------
# 2. Tenant Admin: Create Agent in their own Community
# -----------------------------------------------------------------------------
@router.post(
    "/agents",
    response_model=AgentSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Tenant Admin: Register a new Agent in their community",
)
def admin_create_agent(
    body: AgentCreateRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_CREATE))],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> AgentSummary:
    if caller.role == "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "TENANT_REQUIRED", "message": "Super Admin must register agents via /platform/communities/{id}/agents."},
        )

    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Admin has no active community context."},
        )

    tenant = session.execute(
        select(Tenant).where(Tenant.id == caller.tenant_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    settings = getattr(request.app.state, "settings", None)
    enc_key = getattr(settings, "identity_encryption_key", None) if settings else None

    return _create_agent_in_community(
        session=session,
        tenant=tenant,
        body=body,
        created_by_user_id=caller.user_id,
        created_by_username=caller.username or "admin",
        created_by_membership_id=caller.membership_id,
        encryption_key=enc_key,
        settings=settings,
    )


def _create_agent_in_community(
    *,
    session: Session,
    tenant: Tenant,
    body: AgentCreateRequest,
    created_by_user_id: UUID,
    created_by_username: str,
    created_by_membership_id: UUID | None = None,
    encryption_key: Any | None = None,
    settings: Settings | None = None,
    forced_manager_user_id: UUID | None = None,
    forced_manager_membership_id: UUID | None = None,
    parent_agent_membership_id: UUID | None = None,
    caller_role: str | None = None,
) -> AgentSummary:
    clean_username = body.username.strip().lower()
    existing_username = session.execute(
        select(AccountCredential).where(AccountCredential.username == clean_username)
    ).scalar_one_or_none()
    if existing_username is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "USERNAME_CONFLICT", "message": f"Username '{clean_username}' is already taken."},
        )

    existing_mobile = session.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == tenant.id,
            TenantMembership.mobile_e164 == body.mobile_e164,
        )
    ).scalar_one_or_none()
    if existing_mobile is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "MOBILE_CONFLICT", "message": f"Mobile '{body.mobile_e164}' is already registered in this community."},
        )

    # Verify registration token if supplied
    if body.verification_token:
        otp_service = OtpService(settings=settings)
        valid = otp_service.validate_and_consume_registration_token(
            session=session,
            verification_token=body.verification_token,
            mobile_number=body.mobile_e164,
            expected_purpose="agent_registration",
        )
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_OTP_TOKEN", "message": "Mobile number must be verified with a valid registration OTP."},
            )

    # Location hierarchy resolution
    st_id = body.state_id
    dist_id = body.district_id
    m_id = body.mandal_id
    clean_mandal = (body.mandal or "").strip() or None

    state_obj: State | None = None
    district_obj: District | None = None
    mandal_obj: Mandal | None = None

    if st_id:
        state_obj = session.execute(select(State).where(State.id == st_id, State.is_active == True)).scalar_one_or_none()
        if not state_obj:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_STATE", "message": f"State ID '{st_id}' is invalid or inactive."},
            )
        clean_state = state_obj.name
    else:
        clean_state = (body.state or "").strip()
        if not clean_state:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_STATE", "message": f"State is mandatory and must be one of: {', '.join(SUPPORTED_STATES)}"},
            )
        state_obj = session.execute(
            select(State).where(func.lower(State.name) == clean_state.lower(), State.is_active == True)
        ).scalar_one_or_none()
        if state_obj:
            st_id = state_obj.id
            clean_state = state_obj.name
        elif clean_state not in SUPPORTED_STATES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_STATE", "message": f"State is mandatory and must be one of: {', '.join(SUPPORTED_STATES)}"},
            )

    if dist_id:
        district_obj = session.execute(
            select(District).where(
                District.id == dist_id,
                District.state_id == st_id if st_id else True,
                District.is_active == True,
            )
        ).scalar_one_or_none()
        if not district_obj:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_DISTRICT", "message": f"District ID '{dist_id}' is invalid or does not belong to state."},
            )
        canonical_district = district_obj.name
    else:
        clean_district = (body.district or "").strip()
        if not clean_district:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "DISTRICT_REQUIRED", "message": "District is mandatory for agent registration."},
            )
        canonical_district = normalize_district_name(clean_district)
        if state_obj:
            district_obj = session.execute(
                select(District).where(
                    District.state_id == state_obj.id,
                    (func.lower(District.name) == clean_district.lower()) | (func.lower(District.name) == canonical_district.lower()),
                    District.is_active == True,
                )
            ).scalar_one_or_none()
            if district_obj:
                dist_id = district_obj.id
                canonical_district = district_obj.name
            elif not validate_state_district(clean_state, clean_district):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "INVALID_DISTRICT", "message": f"District '{clean_district}' is not valid for state '{clean_state}'."},
                )
        elif not validate_state_district(clean_state, clean_district):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_DISTRICT", "message": f"District '{clean_district}' is not valid for state '{clean_state}'."},
            )

    if m_id:
        mandal_obj = session.execute(
            select(Mandal).where(
                Mandal.id == m_id,
                Mandal.district_id == dist_id if dist_id else True,
                Mandal.is_active == True,
            )
        ).scalar_one_or_none()
        if not mandal_obj:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_MANDAL", "message": f"Mandal ID '{m_id}' is invalid or does not belong to district."},
            )
        clean_mandal = mandal_obj.name
    elif clean_mandal and district_obj:
        mandal_obj = session.execute(
            select(Mandal).where(
                Mandal.district_id == district_obj.id,
                func.lower(Mandal.name) == clean_mandal.lower(),
                Mandal.is_active == True,
            )
        ).scalar_one_or_none()
        if mandal_obj:
            m_id = mandal_obj.id
            clean_mandal = mandal_obj.name

    # Manager Allocation & Scope Verification
    target_manager_user_id: UUID | None = None
    target_manager_membership_id: UUID | None = None

    if forced_manager_user_id:
        target_manager_user_id = forced_manager_user_id
        target_manager_membership_id = forced_manager_membership_id
        if target_manager_user_id and not target_manager_membership_id:
            m_mem = session.execute(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == tenant.id,
                    TenantMembership.user_id == target_manager_user_id,
                )
            ).scalar_one_or_none()
            target_manager_membership_id = m_mem.id if m_mem else None

    elif created_by_membership_id is not None and (caller_role is None or caller_role not in ("SUPER_ADMIN", "super_admin")):
        has_mla = session.execute(
            select(ManagerLocationAssignment.id).where(
                ManagerLocationAssignment.tenant_id == tenant.id,
                ManagerLocationAssignment.user_id == created_by_user_id,
                ManagerLocationAssignment.status == "active",
            )
        ).scalars().first() is not None

        if has_mla and st_id and dist_id:
            if not LocationService.is_manager_authorized_for_location(
                session=session,
                tenant_id=tenant.id,
                manager_user_id=created_by_user_id,
                state_id=st_id,
                district_id=dist_id,
                mandal_id=m_id,
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "LOCATION_OUT_OF_SCOPE",
                        "message": f"Manager is not authorized to register agents in {canonical_district}, {clean_state}.",
                    },
                )
        target_manager_user_id = created_by_user_id
        target_manager_membership_id = created_by_membership_id

    else:
        # Created by Super Admin
        if body.manager_user_id:
            m_mem = session.execute(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == tenant.id,
                    TenantMembership.user_id == body.manager_user_id,
                )
            ).scalar_one_or_none()
            if not m_mem:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "INVALID_MANAGER", "message": "Selected Manager does not belong to this community."},
                )

            if st_id and dist_id:
                has_target_mla = session.execute(
                    select(ManagerLocationAssignment.id).where(
                        ManagerLocationAssignment.tenant_id == tenant.id,
                        ManagerLocationAssignment.user_id == body.manager_user_id,
                        ManagerLocationAssignment.status == "active",
                    )
                ).scalars().first() is not None

                if has_target_mla and not LocationService.is_manager_authorized_for_location(
                    session=session,
                    tenant_id=tenant.id,
                    manager_user_id=body.manager_user_id,
                    state_id=st_id,
                    district_id=dist_id,
                    mandal_id=m_id,
                ):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "error": "MANAGER_SCOPE_MISMATCH",
                            "message": f"Selected Manager does not have authorization for {canonical_district}, {clean_state}.",
                        },
                    )

            target_manager_user_id = body.manager_user_id
            target_manager_membership_id = m_mem.id
        else:
            # Automatic allocation based on location scope
            if st_id and dist_id:
                eligible = LocationService.find_eligible_managers_for_location(
                    session=session,
                    tenant_id=tenant.id,
                    state_id=st_id,
                    district_id=dist_id,
                    mandal_id=m_id,
                )
                if len(eligible) == 1:
                    target_manager_user_id = eligible[0][0].id
                    target_manager_membership_id = eligible[0][1].id
                elif len(eligible) > 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "error": "AMBIGUOUS_MANAGER_ASSIGNMENT",
                            "message": f"Multiple managers ({len(eligible)}) cover this location. Please explicitly select a manager.",
                            "eligible_managers": [
                                {"user_id": str(u.id), "name": name, "membership_id": str(m.id)}
                                for u, m, name in eligible
                            ],
                        },
                    )
                else:
                    # len(eligible) == 0
                    managers_count = session.execute(
                        select(func.count(AdminProfile.id)).where(AdminProfile.tenant_id == tenant.id)
                    ).scalar() or 0
                    if managers_count > 0:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail={
                                "error": "NO_ELIGIBLE_MANAGER",
                                "message": f"No active manager in this community covers {canonical_district}, {clean_state}.",
                            },
                        )
                    target_manager_user_id = None
                    target_manager_membership_id = None
            else:
                target_manager_user_id = None
                target_manager_membership_id = None

    # WhatsApp number verification (Phase 4)
    final_whatsapp_number = body.mobile_e164
    whatsapp_verified = True
    if not body.whatsapp_same_as_mobile:
        if not body.whatsapp_number or not body.whatsapp_number.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "WHATSAPP_NUMBER_REQUIRED", "message": "Alternative WhatsApp number is required."},
            )
        clean_wa = body.whatsapp_number.strip()
        final_whatsapp_number = clean_wa
        if clean_wa != body.mobile_e164:
            if not body.whatsapp_verification_token:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "WHATSAPP_TOKEN_REQUIRED", "message": "Alternative WhatsApp number must be verified with OTP."},
                )
            otp_service = OtpService(settings=settings)
            valid_wa = otp_service.validate_and_consume_registration_token(
                session=session,
                verification_token=body.whatsapp_verification_token,
                mobile_number=clean_wa,
                expected_purpose="whatsapp_verification",
            )
            if not valid_wa:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "INVALID_WHATSAPP_OTP_TOKEN", "message": "WhatsApp verification token is invalid or expired."},
                )

    # Email verification (Phase 5 - Mandatory for Agent)
    if not body.email or not body.email.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "EMAIL_REQUIRED", "message": "Email is mandatory for agent registration."},
        )
    from app.modules.authentication.email_otp_service import EmailOtpService, normalize_email
    try:
        clean_email = normalize_email(body.email)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_EMAIL", "message": str(exc)},
        )

    if not body.email_verification_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "EMAIL_TOKEN_REQUIRED", "message": "Email verification with OTP is mandatory for Agent registration."},
        )

    email_service = EmailOtpService(settings=settings)
    valid_email = email_service.validate_and_consume_token(
        session=session,
        verification_token=body.email_verification_token,
        email=clean_email,
        expected_purpose="email_verification",
    )
    if not valid_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_EMAIL_OTP_TOKEN", "message": "Email verification token is invalid, expired, or already used."},
        )

    if body.password is not None and len(body.password.strip()) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "PASSWORD_POLICY_VIOLATION", "message": "Password must be at least 8 characters long."},
        )

    # Regional validation and admin authorization
    district_region = get_or_create_region_for_district(session, tenant.id, clean_state, canonical_district)
    target_region_ids = list(body.region_ids or [])
    if district_region.id not in target_region_ids:
        target_region_ids.append(district_region.id)

    req_regions = session.execute(
        select(Region).where(
            Region.id.in_(target_region_ids),
            Region.tenant_id == tenant.id,
            Region.status == "active",
        )
    ).scalars().all()
    found_ids = {r.id for r in req_regions}
    missing_ids = set(target_region_ids) - found_ids
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_REGION", "message": "One or more selected regions do not exist in this community."},
        )

    for r in req_regions:
        if r.state and r.state != clean_state:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "DISTRICT_STATE_MISMATCH",
                    "message": f"District '{r.name}' does not belong to selected state '{clean_state}'.",
                },
            )

    if created_by_membership_id is not None:
        admin_regions = session.execute(
            select(UserRegionAssignment.region_id).where(
                UserRegionAssignment.user_id == created_by_user_id,
                UserRegionAssignment.tenant_id == tenant.id,
                UserRegionAssignment.status == "active",
            )
        ).scalars().all()
        admin_region_set = set(admin_regions)
        if admin_region_set:
            unauthorized = set(target_region_ids) - admin_region_set
            if unauthorized:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "REGION_ASSIGNMENT_UNAUTHORIZED",
                        "message": "Admin cannot assign regions outside their own authorized regions.",
                    },
                )
        else:
            admin_prof = session.execute(
                select(AdminProfile).where(
                    AdminProfile.user_id == created_by_user_id,
                    AdminProfile.tenant_id == tenant.id,
                )
            ).scalar_one_or_none()
            if admin_prof and admin_prof.state and admin_prof.state != clean_state:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "STATE_UNAUTHORIZED",
                        "message": f"Admin is registered for '{admin_prof.state}' and cannot register agents in '{clean_state}'.",
                    },
                )

    try:
        clean_aadhaar = validate_aadhaar_number(body.aadhaar_number)
        masked_aadhaar = mask_aadhaar(clean_aadhaar)
        ciphertext_aadhaar = encrypt_identity_number(clean_aadhaar, encryption_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_AADHAAR", "message": str(exc)},
        ) from exc

    try:
        clean_pan = validate_pan_number(body.pan_number)
        masked_pan = mask_pan(clean_pan)
        ciphertext_pan = encrypt_identity_number(clean_pan, encryption_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_PAN", "message": str(exc)},
        ) from exc

    if not body.profile_photo_key or not body.profile_photo_key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "PHOTO_REQUIRED", "message": "An Agent profile photograph is mandatory."},
        )

    if not body.aadhaar_document_key or not body.aadhaar_document_key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "AADHAAR_DOC_REQUIRED", "message": "An Aadhaar card document image is mandatory."},
        )

    if not body.pan_document_key or not body.pan_document_key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "PAN_DOC_REQUIRED", "message": "A PAN card document image is mandatory."},
        )

    verify_and_bind_stored_object(
        session=session,
        object_key=body.profile_photo_key.strip(),
        expected_category="agent-photo",
        tenant_id=tenant.id,
    )
    verify_and_bind_stored_object(
        session=session,
        object_key=body.aadhaar_document_key.strip(),
        expected_category="aadhaar-document",
        tenant_id=tenant.id,
    )
    verify_and_bind_stored_object(
        session=session,
        object_key=body.pan_document_key.strip(),
        expected_category="pan-document",
        tenant_id=tenant.id,
    )

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

    # 1. User (inactive until approved through appointment workflow)
    user = User(
        id=user_id,
        status="inactive",
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.flush()

    # 2. TenantMembership (inactive until approved)
    membership = TenantMembership(
        id=membership_id,
        tenant_id=tenant.id,
        user_id=user_id,
        mobile_e164=body.mobile_e164,
        status="inactive",
        created_at=now,
        updated_at=now,
    )
    session.add(membership)

    # 3. Role: 'agent' with seeded permissions
    from app.modules.authorization.seeding import seed_tenant_role_permissions
    _, agent_role = seed_tenant_role_permissions(session, tenant.id)

    # 4. UserRoleAssignment (inactive until approved)
    assignment = UserRoleAssignment(
        id=uuid4(),
        tenant_id=tenant.id,
        membership_id=membership_id,
        role_id=agent_role.id,
        assigned_by_membership_id=created_by_membership_id or membership_id,
        status="inactive",
        valid_from=now,
        valid_until=None,
        created_at=now,
        updated_at=now,
    )
    session.add(assignment)

    # 5. AccountCredential (inactive until approved)
    password_val = body.password or secrets.token_urlsafe(16)
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

    # 6. AgentProfile
    agent_profile = AgentProfile(
        id=agent_profile_id,
        tenant_id=tenant.id,
        membership_id=membership_id,
        user_id=user_id,
        first_name=body.first_name.strip(),
        last_name=body.last_name.strip(),
        email=clean_email,
        whatsapp_number=final_whatsapp_number,
        whatsapp_verified=whatsapp_verified,
        email_verified=True,
        email_verified_at=now,
        address=body.address.strip() if body.address else None,
        state=clean_state,
        district=canonical_district,
        mandal=clean_mandal,
        state_id=st_id,
        district_id=dist_id,
        mandal_id=m_id,
        manager_user_id=target_manager_user_id,
        manager_membership_id=target_manager_membership_id,
        parent_agent_membership_id=parent_agent_membership_id,
        ref_id=ref_id,
        commission_rate_override=body.commission_rate_override,
        profile_photo_key=body.profile_photo_key.strip(),
        aadhaar_number_ciphertext=ciphertext_aadhaar,
        aadhaar_number_masked=masked_aadhaar,
        aadhaar_document_key=body.aadhaar_document_key.strip(),
        pan_number_ciphertext=ciphertext_pan,
        pan_number_masked=masked_pan,
        pan_document_key=body.pan_document_key.strip(),
        created_by_membership_id=created_by_membership_id,
        created_at=now,
        updated_at=now,
    )
    session.add(agent_profile)
    session.flush()

    # Member relationships for hierarchy tracking
    if target_manager_membership_id and target_manager_membership_id != membership_id:
        session.add(
            MemberRelationship(
                id=uuid4(),
                tenant_id=tenant.id,
                parent_member_id=target_manager_membership_id,
                child_member_id=membership_id,
                relationship_type=RelationshipType.MANAGES.value,
                status=RelationshipStatus.ACTIVE.value,
                created_by_user_id=created_by_user_id,
            )
        )
    if parent_agent_membership_id and parent_agent_membership_id != membership_id:
        session.add(
            MemberRelationship(
                id=uuid4(),
                tenant_id=tenant.id,
                parent_member_id=parent_agent_membership_id,
                child_member_id=membership_id,
                relationship_type=RelationshipType.REPORTS_TO.value,
                status=RelationshipStatus.ACTIVE.value,
                created_by_user_id=created_by_user_id,
            )
        )

    # 7. Region assignments
    assigned_regions: list[UUID] = []
    for r_id in target_region_ids:
        reg = session.execute(
            select(Region).where(Region.id == r_id, Region.tenant_id == tenant.id)
        ).scalar_one_or_none()
        if reg is not None:
            session.add(
                UserRegionAssignment(
                    id=uuid4(),
                    user_id=user_id,
                    tenant_id=tenant.id,
                    region_id=r_id,
                    status="inactive",
                    assigned_by_user_id=created_by_user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                RegionAuditLog(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    user_id=user_id,
                    region_id=r_id,
                    action="proposed",
                    actor_id=created_by_user_id,
                    reason="agent_appointment_submission",
                    created_at=now,
                )
            )
            assigned_regions.append(r_id)

    # 8. Commission audit log if override provided
    if body.commission_rate_override is not None:
        session.add(
            CommissionAuditLog(
                id=uuid4(),
                tenant_id=tenant.id,
                agent_profile_id=agent_profile_id,
                changed_by_user_id=created_by_user_id,
                changed_by_username=created_by_username,
                scope="agent",
                previous_rate=None,
                new_rate=body.commission_rate_override,
                created_at=now,
            )
        )

    # 8b. Create formal AgentAppointment record (status PENDING - awaiting approval)
    appointment = AgentAppointment(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=user_id,
        membership_id=membership_id,
        agent_profile_id=agent_profile_id,
        requested_by_user_id=created_by_user_id,
        role_id=agent_role.id,
        status="PENDING",
        proposed_commission_rate=body.commission_rate_override,
        notes=None,
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
        requested_by_user_id=created_by_user_id,
    )

    # 9. Audit event
    audit_service.record_mandatory(
        session,
        AuditEventDraft(
            event_type="tenant.agent.registered",
            actor_category="platform_operator" if created_by_membership_id is None else "tenant_member",
            actor_ref=str(created_by_user_id),
            action="agent.registered",
            outcome="success",
            tenant_id=tenant.id,
            target_entity_type="agent_profile",
            target_entity_ref=str(agent_profile_id),
        ),
    )

    session.commit()

    return build_agent_summary(
        agent_profile,
        clean_username,
        body.mobile_e164,
        "pending",
        tenant.slug,
        tenant.default_commission_rate,
        assigned_region_ids=assigned_regions,
        pending_appointment_id=appointment.id,
        state=body.state,
    )


# -----------------------------------------------------------------------------
# 3. Super Admin: List Agents in Community or Globally
# -----------------------------------------------------------------------------
@router.get(
    "/platform/communities/{community_id}/agents",
    response_model=AgentListResponse,
    summary="Super Admin: List agents in a community",
)
def superadmin_list_community_agents(
    community_id: UUID,
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentListResponse:
    tenant = session.execute(select(Tenant).where(Tenant.id == community_id)).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    return _query_agents(session=session, tenant_id=community_id, tenant_slug=tenant.slug, default_comm=tenant.default_commission_rate)


@router.get(
    "/platform/agents",
    response_model=AgentListResponse,
    summary="Super Admin: List all agents across all communities",
)
def superadmin_list_all_agents(
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentListResponse:
    return _query_agents(session=session, tenant_id=None)


# -----------------------------------------------------------------------------
# 4. Tenant Admin: List Agents in their Community
# -----------------------------------------------------------------------------
@router.get(
    "/agents",
    response_model=AgentListResponse,
    summary="Tenant Admin: List agents in their community",
)
def admin_list_agents(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_READ))],
    session: Annotated[Session, Depends(get_db)],
) -> AgentListResponse:
    tenant_id = caller.tenant_id
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_TENANT", "message": "No community context."},
        )
    tenant = session.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
    slug = tenant.slug if tenant else ""
    comm = tenant.default_commission_rate if tenant else Decimal("10.00")

    allowed_region_ids: set[UUID] | None = None
    if caller.role == "ADMIN":
        admin_regions = session.execute(
            select(UserRegionAssignment.region_id).where(
                UserRegionAssignment.user_id == caller.user_id,
                UserRegionAssignment.tenant_id == tenant_id,
                UserRegionAssignment.status == "active",
            )
        ).scalars().all()
        allowed_region_ids = set(admin_regions)
        if not allowed_region_ids:
            admin_prof = session.execute(
                select(AdminProfile).where(
                    AdminProfile.user_id == caller.user_id,
                    AdminProfile.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if admin_prof and admin_prof.state:
                state_regions = session.execute(
                    select(Region.id).where(
                        Region.tenant_id == tenant_id,
                        Region.state == admin_prof.state,
                        Region.status == "active",
                    )
                ).scalars().all()
                if state_regions:
                    allowed_region_ids = set(state_regions)

    return _query_agents(
        session=session,
        tenant_id=tenant_id,
        tenant_slug=slug,
        default_comm=comm,
        allowed_region_ids=allowed_region_ids,
    )


def _query_agents(
    *,
    session: Session,
    tenant_id: UUID | None = None,
    tenant_slug: str | None = None,
    default_comm: Decimal | None = None,
    allowed_region_ids: set[UUID] | None = None,
) -> AgentListResponse:
    query = (
        select(
            AgentProfile,
            AccountCredential.username,
            TenantMembership.mobile_e164,
            TenantMembership.status.label("membership_status"),
            Tenant.slug.label("tenant_slug"),
            Tenant.default_commission_rate.label("tenant_default_commission"),
        )
        .join(TenantMembership, TenantMembership.id == AgentProfile.membership_id)
        .join(AccountCredential, AccountCredential.user_id == AgentProfile.user_id)
        .join(Tenant, Tenant.id == AgentProfile.tenant_id)
    )

    if tenant_id:
        query = query.where(AgentProfile.tenant_id == tenant_id)

    if allowed_region_ids is not None:
        if not allowed_region_ids:
            return AgentListResponse(items=[], total=0)
        agent_user_ids = (
            select(UserRegionAssignment.user_id)
            .where(
                UserRegionAssignment.tenant_id == tenant_id,
                UserRegionAssignment.region_id.in_(allowed_region_ids),
            )
            .scalar_subquery()
        )
        query = query.where(AgentProfile.user_id.in_(agent_user_ids))

    rows = session.execute(query.order_by(AgentProfile.created_at.desc())).all()
    items = [
        build_agent_summary(
            row.AgentProfile,
            row.username,
            row.mobile_e164,
            row.membership_status,
            row.tenant_slug,
            row.tenant_default_commission or Decimal("10.00"),
            session=session,
        )
        for row in rows
    ]
    return AgentListResponse(items=items, total=len(items))


# -----------------------------------------------------------------------------
# 5. Get and Update Agent Details
# -----------------------------------------------------------------------------
@router.get(
    "/agents/me",
    summary="Get profile of the currently authenticated agent",
)
def get_current_agent_profile(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_READ))],
    session: Annotated[Session, Depends(get_db)],
):
    row = session.execute(
        select(
            AgentProfile,
            TenantMembership.mobile_e164,
            Tenant.slug.label("tenant_slug"),
            Tenant.default_commission_rate.label("tenant_default_commission"),
        )
        .join(TenantMembership, TenantMembership.id == AgentProfile.membership_id)
        .join(Tenant, Tenant.id == AgentProfile.tenant_id)
        .where(AgentProfile.user_id == caller.user_id)
    ).first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "AGENT_PROFILE_NOT_FOUND", "message": "Agent profile not found."},
        )

    profile = row.AgentProfile
    slug = row.tenant_slug or ""
    default_comm = row.tenant_default_commission or Decimal("10.00")
    summary = build_agent_summary(
        profile,
        caller.username,
        row.mobile_e164 or "",
        caller.status,
        slug,
        default_comm,
        session=session,
    )
    return {
        "agent": summary,
        "employee": {
            "id": str(profile.id),
            "refId": profile.ref_id,
            "firstName": profile.first_name,
            "lastName": profile.last_name,
            "username": caller.username,
            "status": caller.status,
            "commissionRate": float(summary.effective_commission_rate),
        },
    }



@router.get(
    "/agents/{agent_id}",
    response_model=AgentSummary,
    summary="Get Agent details (with masked identity documents)",
)

def get_agent_detail(
    agent_id: UUID,
    caller: Annotated[ResolvedSession, Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentSummary:
    resolved, _ = caller

    row = session.execute(
        select(
            AgentProfile,
            AccountCredential.username,
            TenantMembership.mobile_e164,
            TenantMembership.status.label("membership_status"),
            Tenant.slug.label("tenant_slug"),
            Tenant.default_commission_rate.label("tenant_default_commission"),
        )
        .join(TenantMembership, TenantMembership.id == AgentProfile.membership_id)
        .join(AccountCredential, AccountCredential.user_id == AgentProfile.user_id)
        .join(Tenant, Tenant.id == AgentProfile.tenant_id)
        .where(AgentProfile.id == agent_id)
    ).first()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "AGENT_NOT_FOUND", "message": "Agent profile not found."},
        )

    # Isolation check
    if resolved.role != "SUPER_ADMIN":
        if row.AgentProfile.tenant_id != resolved.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "FORBIDDEN", "message": "Access to agent outside your community is denied."},
            )
        if resolved.role == "ADMIN":
            admin_regions = set(
                session.execute(
                    select(UserRegionAssignment.region_id).where(
                        UserRegionAssignment.user_id == resolved.user_id,
                        UserRegionAssignment.tenant_id == resolved.tenant_id,
                        UserRegionAssignment.status == "active",
                    )
                ).scalars().all()
            )
            if not admin_regions:
                admin_prof = session.execute(
                    select(AdminProfile).where(
                        AdminProfile.user_id == resolved.user_id,
                        AdminProfile.tenant_id == resolved.tenant_id,
                    )
                ).scalar_one_or_none()
                if admin_prof and admin_prof.state:
                    admin_regions = set(
                        session.execute(
                            select(Region.id).where(
                                Region.tenant_id == resolved.tenant_id,
                                Region.state == admin_prof.state,
                                Region.status == "active",
                            )
                        ).scalars().all()
                    )

            if admin_regions:
                agent_regions = set(
                    session.execute(
                        select(UserRegionAssignment.region_id).where(
                            UserRegionAssignment.user_id == row.AgentProfile.user_id,
                            UserRegionAssignment.tenant_id == resolved.tenant_id,
                        )
                    ).scalars().all()
                )
                if not (agent_regions & admin_regions):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={"error": "REGION_FORBIDDEN", "message": "Access to agent outside your assigned region is denied."},
                    )

    return build_agent_summary(
        row.AgentProfile,
        row.username,
        row.mobile_e164,
        row.membership_status,
        row.tenant_slug,
        row.tenant_default_commission or Decimal("10.00"),
        session=session,
    )


@router.patch(
    "/agents/{agent_id}",
    response_model=AgentSummary,
    summary="Update Agent status or details",
)
def update_agent(
    agent_id: UUID,
    body: AgentUpdateRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_UPDATE))],
    session: Annotated[Session, Depends(get_db)],
) -> AgentSummary:
    row = session.execute(
        select(
            AgentProfile,
            AccountCredential,
            TenantMembership,
            Tenant,
        )
        .join(TenantMembership, TenantMembership.id == AgentProfile.membership_id)
        .join(AccountCredential, AccountCredential.user_id == AgentProfile.user_id)
        .join(Tenant, Tenant.id == AgentProfile.tenant_id)
        .where(AgentProfile.id == agent_id)
    ).first()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "AGENT_NOT_FOUND", "message": "Agent profile not found."},
        )

    agent_prof, cred, memb, tenant = row

    if caller.role != "SUPER_ADMIN" and agent_prof.tenant_id != caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Cannot update agent in another community."},
        )

    if caller.role == "ADMIN":
        admin_regions = set(
            session.execute(
                select(UserRegionAssignment.region_id).where(
                    UserRegionAssignment.user_id == caller.user_id,
                    UserRegionAssignment.tenant_id == caller.tenant_id,
                    UserRegionAssignment.status == "active",
                )
            ).scalars().all()
        )
        if not admin_regions:
            admin_prof = session.execute(
                select(AdminProfile).where(
                    AdminProfile.user_id == caller.user_id,
                    AdminProfile.tenant_id == caller.tenant_id,
                )
            ).scalar_one_or_none()
            if admin_prof and admin_prof.state:
                admin_regions = set(
                    session.execute(
                        select(Region.id).where(
                            Region.tenant_id == caller.tenant_id,
                            Region.state == admin_prof.state,
                            Region.status == "active",
                        )
                    ).scalars().all()
                )

        if admin_regions:
            agent_regions = set(
                session.execute(
                    select(UserRegionAssignment.region_id).where(
                        UserRegionAssignment.user_id == agent_prof.user_id,
                        UserRegionAssignment.tenant_id == caller.tenant_id,
                    )
                ).scalars().all()
            )
            if not (agent_regions & admin_regions):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": "REGION_FORBIDDEN", "message": "Cannot update agent outside your assigned region."},
                )

    if body.status is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "LIFECYCLE_WORKFLOW_REQUIRED",
                "message": "Community Admins cannot directly change agent status. Submit an agent lifecycle request.",
            },
        )

    now = datetime.now(UTC)

    if body.commission_rate_override is not None:
        if caller.role != "SUPER_ADMIN":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "SUPER_ADMIN_REQUIRED", "message": "Only Super Admin can update commission overrides."},
            )
        prev_rate = agent_prof.commission_rate_override
        agent_prof.commission_rate_override = body.commission_rate_override
        session.add(
            CommissionAuditLog(
                id=uuid4(),
                tenant_id=tenant.id,
                agent_profile_id=agent_prof.id,
                changed_by_user_id=caller.user_id,
                changed_by_username=caller.username or "superadmin",
                scope="agent",
                previous_rate=prev_rate,
                new_rate=body.commission_rate_override,
                created_at=now,
            )
        )

    if body.first_name:
        agent_prof.first_name = body.first_name.strip()
    if body.last_name:
        agent_prof.last_name = body.last_name.strip()
    if body.email is not None:
        agent_prof.email = body.email.strip() if body.email else None
    if body.address is not None:
        agent_prof.address = body.address.strip() if body.address else None

    if body.region_ids is not None:
        if caller.role != "SUPER_ADMIN":
            admin_regions = session.execute(
                select(UserRegionAssignment.region_id).where(
                    UserRegionAssignment.user_id == caller.user_id,
                    UserRegionAssignment.tenant_id == tenant.id,
                    UserRegionAssignment.status == "active",
                )
            ).scalars().all()
            admin_region_set = set(admin_regions)
            if admin_region_set:
                unauthorized = set(body.region_ids) - admin_region_set
                if unauthorized:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "error": "REGION_ASSIGNMENT_UNAUTHORIZED",
                            "message": "Admin cannot assign regions outside their own authorized regions.",
                        },
                    )
        current_assignments = session.execute(
            select(UserRegionAssignment).where(
                UserRegionAssignment.user_id == agent_prof.user_id,
                UserRegionAssignment.tenant_id == tenant.id,
            )
        ).scalars().all()
        current_map = {a.region_id: a for a in current_assignments}
        target_ids = set(body.region_ids)

        for rid, a in current_map.items():
            if rid not in target_ids and a.status == "active":
                a.status = "inactive"
                session.add(
                    RegionAuditLog(
                        id=uuid4(),
                        tenant_id=tenant.id,
                        user_id=agent_prof.user_id,
                        region_id=rid,
                        action="revoked",
                        actor_id=caller.user_id,
                        reason="agent_update",
                        created_at=now,
                    )
                )
        for rid in target_ids:
            if rid not in current_map:
                reg = session.execute(
                    select(Region).where(Region.id == rid, Region.tenant_id == tenant.id)
                ).scalar_one_or_none()
                if reg is not None:
                    session.add(
                        UserRegionAssignment(
                            id=uuid4(),
                            user_id=agent_prof.user_id,
                            tenant_id=tenant.id,
                            region_id=rid,
                            status="active",
                            assigned_by_user_id=caller.user_id,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    session.add(
                        RegionAuditLog(
                            id=uuid4(),
                            tenant_id=tenant.id,
                            user_id=agent_prof.user_id,
                            region_id=rid,
                            action="assigned",
                            actor_id=caller.user_id,
                            reason="agent_update",
                            created_at=now,
                        )
                    )
            elif current_map[rid].status != "active":
                current_map[rid].status = "active"
                session.add(
                    RegionAuditLog(
                        id=uuid4(),
                        tenant_id=tenant.id,
                        user_id=agent_prof.user_id,
                        region_id=rid,
                        action="assigned",
                        actor_id=caller.user_id,
                        reason="agent_reactivate_region",
                        created_at=now,
                    )
                )

    agent_prof.updated_at = now
    session.commit()

    return build_agent_summary(
        agent_prof,
        cred.username,
        memb.mobile_e164,
        memb.status,
        tenant.slug,
        tenant.default_commission_rate,
        session=session,
    )


# -----------------------------------------------------------------------------
# 6. Super Admin: Configurable Community Commission & Photo Policy
# -----------------------------------------------------------------------------
@router.patch(
    "/platform/communities/{community_id}/commission",
    response_model=CommunityPolicyResponse,
    summary="Super Admin: Update community default commission rate",
)
def update_community_commission(
    community_id: UUID,
    body: CommissionUpdateRequest,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> CommunityPolicyResponse:
    tenant = session.execute(select(Tenant).where(Tenant.id == community_id)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "COMMUNITY_NOT_FOUND"})

    prev_rate = tenant.default_commission_rate
    tenant.default_commission_rate = body.default_commission_rate
    now = datetime.now(UTC)
    tenant.updated_at = now

    session.add(
        CommissionAuditLog(
            id=uuid4(),
            tenant_id=tenant.id,
            agent_profile_id=None,
            changed_by_user_id=caller.user_id,
            changed_by_username=caller.username or "superadmin",
            scope="community",
            previous_rate=prev_rate,
            new_rate=body.default_commission_rate,
            created_at=now,
        )
    )
    session.commit()

    return CommunityPolicyResponse(
        photo_requirement=tenant.photo_requirement,
        default_commission_rate=tenant.default_commission_rate,
        policy_version=tenant.policy_version,
    )


@router.get(
    "/platform/communities/{community_id}/commission/audit-logs",
    response_model=CommissionAuditListResponse,
    summary="Super Admin: View commission change audit logs",
)
def get_commission_audit_logs(
    community_id: UUID,
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> CommissionAuditListResponse:
    logs = session.execute(
        select(CommissionAuditLog)
        .where(CommissionAuditLog.tenant_id == community_id)
        .order_by(CommissionAuditLog.created_at.desc())
    ).scalars().all()

    items = [
        CommissionAuditSummary(
            id=l.id,
            tenant_id=l.tenant_id,
            agent_profile_id=l.agent_profile_id,
            changed_by_username=l.changed_by_username,
            scope=l.scope,
            previous_rate=l.previous_rate,
            new_rate=l.new_rate,
            created_at=l.created_at,
        )
        for l in logs
    ]
    return CommissionAuditListResponse(items=items, total=len(items))


@router.patch(
    "/platform/communities/{community_id}/photo-policy",
    response_model=CommunityPolicyResponse,
    summary="Super Admin: Configure community user photo requirement (OPTIONAL or REQUIRED)",
)
def update_community_photo_policy(
    community_id: UUID,
    body: PhotoPolicyUpdateRequest,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> CommunityPolicyResponse:
    tenant = session.execute(select(Tenant).where(Tenant.id == community_id)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "COMMUNITY_NOT_FOUND"})

    prev = tenant.photo_requirement
    tenant.photo_requirement = body.photo_requirement
    now = datetime.now(UTC)
    tenant.updated_at = now

    audit_service.record_mandatory(
        session,
        AuditEventDraft(
            event_type="platform.community.policy_updated",
            actor_category="platform_operator",
            actor_ref=str(caller.user_id),
            action="community.photo_policy_updated",
            outcome="success",
            tenant_id=tenant.id,
            target_entity_type="tenant",
            target_entity_ref=str(tenant.id),
        ),
    )
    session.commit()

    return CommunityPolicyResponse(
        photo_requirement=tenant.photo_requirement,
        default_commission_rate=tenant.default_commission_rate,
        policy_version=tenant.policy_version,
    )


# -----------------------------------------------------------------------------
# 7. Agent: User Registration & Photo Policy Enforcement
# -----------------------------------------------------------------------------
@router.get(
    "/agent/policy",
    response_model=CommunityPolicyResponse,
    summary="Agent: Retrieve current community registration policy and photo requirement",
)
def get_agent_community_policy(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.AGENT_READ))],
    session: Annotated[Session, Depends(get_db)],
) -> CommunityPolicyResponse:
    tenant = session.execute(select(Tenant).where(Tenant.id == caller.tenant_id)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "COMMUNITY_NOT_FOUND"})

    return CommunityPolicyResponse(
        photo_requirement=tenant.photo_requirement,
        default_commission_rate=tenant.default_commission_rate,
        policy_version=tenant.policy_version,
    )


@router.post(
    "/agent/users",
    response_model=UserSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Agent: Register a new subscriber / user in their community",
)
async def register_user(
    body: UserRegistrationRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_CREATE))],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> UserSummary:
    tenant = session.execute(select(Tenant).where(Tenant.id == caller.tenant_id)).scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "COMMUNITY_NOT_FOUND"})

    # Verify registration token if supplied
    if body.verification_token:
        settings = getattr(request.app.state, "settings", None) or Settings()
        otp_service = OtpService(settings=settings)
        valid = otp_service.validate_and_consume_registration_token(
            session=session,
            verification_token=body.verification_token,
            mobile_number=body.mobile_e164,
            expected_purpose="user_registration",
        )
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_OTP_TOKEN", "message": "Mobile number must be verified with a valid registration OTP."},
            )

    # WhatsApp number verification (Phase 4)
    final_whatsapp_number = body.mobile_e164
    whatsapp_verified = True
    if not body.whatsapp_same_as_mobile:
        if not body.whatsapp_number or not body.whatsapp_number.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "WHATSAPP_NUMBER_REQUIRED", "message": "Alternative WhatsApp number is required."},
            )
        clean_wa = body.whatsapp_number.strip()
        final_whatsapp_number = clean_wa
        if clean_wa != body.mobile_e164:
            if not body.whatsapp_verification_token:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "WHATSAPP_TOKEN_REQUIRED", "message": "Alternative WhatsApp number must be verified with OTP."},
                )
            settings = getattr(request.app.state, "settings", None) or Settings()
            otp_service = OtpService(settings=settings)
            valid_wa = otp_service.validate_and_consume_registration_token(
                session=session,
                verification_token=body.whatsapp_verification_token,
                mobile_number=clean_wa,
                expected_purpose="whatsapp_verification",
            )
            if not valid_wa:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "INVALID_WHATSAPP_OTP_TOKEN", "message": "WhatsApp verification token is invalid or expired."},
                )

    # Email verification (Phase 5 - Optional, but mandatory OTP if email is provided)
    clean_email = None
    email_verified = False
    email_verified_at = None
    if body.email and body.email.strip():
        from app.modules.authentication.email_otp_service import EmailOtpService, normalize_email
        try:
            clean_email = normalize_email(body.email)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_EMAIL", "message": str(exc)},
            )

        if not body.email_verification_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "EMAIL_TOKEN_REQUIRED", "message": "Email verification with OTP is required when email is provided."},
            )

        settings = getattr(request.app.state, "settings", None) or Settings()
        email_service = EmailOtpService(settings=settings)
        valid_email = email_service.validate_and_consume_token(
            session=session,
            verification_token=body.email_verification_token,
            email=clean_email,
            expected_purpose="email_verification",
        )
        if not valid_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_EMAIL_OTP_TOKEN", "message": "Email verification token is invalid, expired, or already used."},
            )
        email_verified = True
        email_verified_at = datetime.now(UTC)

    # Enforce community photo requirement policy
    if tenant.photo_requirement == "REQUIRED":
        if not body.photo_key or not body.photo_key.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "PHOTO_REQUIRED", "message": "User photograph is required by community policy."},
            )

    if body.photo_key and body.photo_key.strip():
        verify_and_bind_stored_object(
            session=session,
            object_key=body.photo_key.strip(),
            expected_category="user-photo",
            tenant_id=tenant.id,
        )

    existing_mobile = session.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == tenant.id,
            TenantMembership.mobile_e164 == body.mobile_e164,
        )
    ).scalar_one_or_none()
    if existing_mobile is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "MOBILE_CONFLICT", "message": f"Mobile '{body.mobile_e164}' is already registered in this community."},
        )

    now = datetime.now(UTC)
    user_id = uuid4()
    membership_id = uuid4()
    subscriber_profile_id = uuid4()

    # 1. User
    user = User(
        id=user_id,
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.flush()

    # 2. TenantMembership
    membership = TenantMembership(
        id=membership_id,
        tenant_id=tenant.id,
        user_id=user_id,
        mobile_e164=body.mobile_e164,
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(membership)
    session.flush()

    # 3. SubscriberProfile
    subscriber_prof = SubscriberProfile(
        id=subscriber_profile_id,
        tenant_id=tenant.id,
        membership_id=membership_id,
        user_id=user_id,
        registered_by_agent_membership_id=caller.membership_id,
        full_name=body.full_name.strip(),
        email=clean_email,
        whatsapp_number=final_whatsapp_number,
        whatsapp_verified=whatsapp_verified,
        whatsapp_consent=body.whatsapp_consent,
        whatsapp_consent_at=now if body.whatsapp_consent else None,
        email_verified=email_verified,
        email_verified_at=email_verified_at,
        address=body.address.strip() if body.address else None,
        photo_key=body.photo_key.strip() if body.photo_key else None,
        package_plan=body.package_plan,
        billing_cycle=body.billing_cycle,
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(subscriber_prof)

    # 4. Member relationships: REFERRED_BY and REPORTS_TO
    if caller.membership_id:
        session.add(
            MemberRelationship(
                id=uuid4(),
                tenant_id=tenant.id,
                parent_member_id=caller.membership_id,
                child_member_id=membership_id,
                relationship_type=RelationshipType.REFERRED_BY.value,
                status=RelationshipStatus.ACTIVE.value,
                created_by_user_id=caller.user_id,
                created_at=now,
            )
        )
        session.add(
            MemberRelationship(
                id=uuid4(),
                tenant_id=tenant.id,
                parent_member_id=caller.membership_id,
                child_member_id=membership_id,
                relationship_type=RelationshipType.REPORTS_TO.value,
                status=RelationshipStatus.ACTIVE.value,
                created_by_user_id=caller.user_id,
                created_at=now,
            )
        )

    # 5. Resolve Subscription Plan or Custom Amount
    custom_amount_flag = False
    resolved_amount = Decimal("199.00")
    plan_name = body.package_plan or "Standard"
    chosen_plan_id = body.plan_id
    selected_plan: SubscriptionPlan | None = None

    if body.plan_id is not None:
        selected_plan = session.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == body.plan_id)
        ).scalar_one_or_none()
        if not selected_plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PLAN_NOT_FOUND", "message": "Subscription plan not found."},
            )
        if selected_plan.community_id != tenant.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_COMMUNITY_PLAN_FORBIDDEN", "message": "Plan belongs to a different community."},
            )
        if selected_plan.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "PLAN_INACTIVE", "message": "Selected plan is inactive."},
            )
        plan_name = selected_plan.name
        if selected_plan.nominal_price:
            resolved_amount = selected_plan.nominal_price

    if body.custom_amount is not None and body.custom_amount > Decimal("0.00"):
        resolved_amount = body.custom_amount
        custom_amount_flag = True
        plan_name = f"Custom (Rs. {resolved_amount})"

    if chosen_plan_id is None:
        first_plan = session.execute(
            select(SubscriptionPlan).where(
                SubscriptionPlan.community_id == tenant.id,
                SubscriptionPlan.status == "active",
            ).limit(1)
        ).scalar_one_or_none()
        if not first_plan:
            first_plan = session.execute(
                select(SubscriptionPlan).where(SubscriptionPlan.community_id == tenant.id).limit(1)
            ).scalar_one_or_none()
        if not first_plan:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "NO_COMMUNITY_PLAN",
                    "message": "No subscription plan is configured for this community.",
                },
            )
        chosen_plan_id = first_plan.id
        selected_plan = first_plan
        if not custom_amount_flag:
            plan_name = first_plan.name
            if first_plan.nominal_price:
                resolved_amount = first_plan.nominal_price

    subscriber_prof.package_plan = plan_name

    # 6. MemberSubscription
    sub_id = uuid4()
    sub = MemberSubscription(
        id=sub_id,
        tenant_id=tenant.id,
        subscriber_membership_id=membership_id,
        plan_id=chosen_plan_id,
        agreed_amount=resolved_amount,
        currency="INR",
        starts_at=now,
        expires_at=now + timedelta(days=30),
        status=SubscriptionStatus.PENDING_PAYMENT.value,
        payment_status=SubscriptionPaymentStatus.UNPAID.value,
        snapshot_plan_name=plan_name,
        snapshot_duration_days=30,
        notes={"custom_amount": custom_amount_flag},
        created_by_user_id=caller.user_id,
    )
    session.add(sub)
    session.flush()

    # 7. Payment Order & Microservice Initiation
    payment_order_id = uuid4()
    amount_paise = int(resolved_amount * 100)
    payment_order = PaymentOrder(
        id=payment_order_id,
        tenant_id=tenant.id,
        subscription_id=sub_id,
        subscriber_membership_id=membership_id,
        gateway="payment_microservice",
        gateway_order_id=f"order_{uuid4().hex[:14]}",
        amount_paise=amount_paise,
        amount=resolved_amount,
        currency="INR",
        status=PaymentOrderStatus.CREATED.value,
        idempotency_key=str(uuid4()),
        custom_amount=custom_amount_flag,
        created_by_membership_id=caller.membership_id,
    )
    session.add(payment_order)
    session.flush()

    settings = getattr(request.app.state, "settings", None) or Settings()
    pay_client = getattr(request.app.state, "payment_client", None) or PaymentMicroserviceClient(settings=settings)
    callback_url = body.callback_url or f"/agent/payment/callback?order_id={payment_order.id}"
    init_res = await pay_client.initiate_payment(
        tenant_slug=tenant.slug,
        amount_paise=amount_paise,
        description=f"Subscription - {plan_name}",
        callback_url=callback_url,
        customer_name=body.full_name,
        customer_phone=body.mobile_e164,
        customer_email=clean_email,
        idempotency_key=str(payment_order.idempotency_key),
    )

    if not init_res.success:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "PAYMENT_INITIATION_FAILED",
                "message": f"Payment initiation failed: {init_res.error or 'Gateway rejected request'}",
            },
        )

    payment_order.external_payment_id = init_res.payment_id
    payment_order.payment_page_url = init_res.payment_page_url
    if init_res.order_id:
        payment_order.gateway_order_id = init_res.order_id

    audit_service.record_mandatory(
        session,
        AuditEventDraft(
            event_type="tenant.subscriber.registered",
            actor_category="tenant_member",
            actor_ref=str(caller.user_id),
            action="subscriber.registered",
            outcome="success",
            tenant_id=tenant.id,
            target_entity_type="subscriber_profile",
            target_entity_ref=str(subscriber_profile_id),
        ),
    )
    session.commit()

    clean_m = body.mobile_e164
    masked_m = f"{clean_m[:5]}*** **{clean_m[-3:]}" if len(clean_m) >= 13 else clean_m
    pkg_type = "Custom" if custom_amount_flag else "Plan"

    return UserSummary(
        id=subscriber_prof.id,
        tenant_id=subscriber_prof.tenant_id,
        community_slug=tenant.slug,
        membership_id=subscriber_prof.membership_id,
        user_id=subscriber_prof.user_id,
        full_name=subscriber_prof.full_name,
        mobile_e164=body.mobile_e164,
        masked_mobile=masked_m,
        whatsapp_number=subscriber_prof.whatsapp_number,
        whatsapp_verified=subscriber_prof.whatsapp_verified,
        whatsapp_consent=subscriber_prof.whatsapp_consent,
        email=subscriber_prof.email,
        email_verified=subscriber_prof.email_verified,
        address=subscriber_prof.address,
        photo_key=subscriber_prof.photo_key,
        package_plan=subscriber_prof.package_plan,
        package_type=pkg_type,
        billing_cycle=subscriber_prof.billing_cycle,
        status=subscriber_prof.status,
        payment_status=payment_order.status,
        subscription_status=sub.status,
        payment_amount=resolved_amount,
        payment_order_id=payment_order.id,
        payment_page_url=payment_order.payment_page_url,
        external_payment_id=payment_order.external_payment_id,
        registered_by_agent_membership_id=subscriber_prof.registered_by_agent_membership_id,
        created_at=subscriber_prof.created_at,
    )


@router.post(
    "/agent/payments/confirm",
    response_model=PaymentConfirmResponse,
    summary="Agent: Confirm payment and activate subscriber with notifications",
)
async def confirm_subscriber_payment(
    body: PaymentConfirmRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_CREATE))],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> PaymentConfirmResponse:
    query = select(PaymentOrder).where(PaymentOrder.tenant_id == caller.tenant_id)
    if body.payment_order_id:
        query = query.where(PaymentOrder.id == body.payment_order_id)
    elif body.external_payment_id:
        query = query.where(PaymentOrder.external_payment_id == body.external_payment_id)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "ORDER_ID_REQUIRED", "message": "Either payment_order_id or external_payment_id is required."},
        )

    order = session.execute(query).scalar_one_or_none()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "PAYMENT_ORDER_NOT_FOUND", "message": "Payment order could not be found."},
        )

    # If already paid, return idempotent success
    if order.status == PaymentOrderStatus.PAID.value:
        return PaymentConfirmResponse(
            success=True,
            status="paid",
            subscription_status="ACTIVE",
            message="Payment was already confirmed and subscription is active.",
            payment_order_id=order.id,
        )

    if not order.external_payment_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "PAYMENT_NOT_INITIALIZED", "message": "Payment order has no external payment reference."},
        )

    # Check payment status via client
    settings = getattr(request.app.state, "settings", None) or Settings()
    pay_client = getattr(request.app.state, "payment_client", None) or PaymentMicroserviceClient(settings=settings)
    status_res = await pay_client.get_payment_status(order.external_payment_id)

    # 1. Authoritative status verification check (Fail closed if unavailable)
    if not status_res or not status_res.success:
        err_msg = status_res.error if status_res else "No response"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "PAYMENT_STATUS_UNAVAILABLE",
                "message": f"Payment status could not be verified with payment gateway: {err_msg}",
            },
        )

    # 2. Reject untrusted checkout details: settlement cannot be established from non-authoritative public checkout
    if not status_res.is_authoritative:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "UNTRUSTED_PAYMENT_STATUS",
                "message": "Payment confirmation rejected: settlement status cannot be established from untrusted public checkout details. Authoritative verification required.",
            },
        )

    # 3. Gateway status check: must be completed
    if status_res.status != "COMPLETED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "PAYMENT_NOT_CONFIRMED",
                "message": f"Payment has not yet completed at gateway (current status: {status_res.status}).",
            },
        )

    # 4. Strict Field Verification against stored PaymentOrder
    # A. External Payment ID verification
    if status_res.payment_id and status_res.payment_id != order.external_payment_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "PAYMENT_ID_MISMATCH", "message": "Gateway payment ID does not match order record."},
        )

    # B. Gateway Order ID verification
    if status_res.order_id and order.gateway_order_id:
        if status_res.order_id != order.gateway_order_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "ORDER_ID_MISMATCH", "message": "Gateway order ID does not match order record."},
            )

    # C. Cross-tenant verification
    if status_res.tenant_id:
        tenant_match = False
        if str(order.tenant_id) == str(status_res.tenant_id):
            tenant_match = True
        else:
            t_slug = session.execute(select(Tenant.slug).where(Tenant.id == order.tenant_id)).scalar()
            if t_slug and t_slug == str(status_res.tenant_id):
                tenant_match = True
        if not tenant_match:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_TENANT_PAYMENT_REJECTED", "message": "Cross-tenant payment rejected: Payment belongs to a different tenant."},
            )

    # D. Currency verification
    if status_res.currency and order.currency:
        if status_res.currency.upper() != order.currency.upper():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "CURRENCY_MISMATCH", "message": f"Currency mismatch: expected {order.currency}, got {status_res.currency}."},
            )

    # E. Amount verification (tolerance of 10 paise)
    if status_res.amount is not None:
        amt_num = float(status_res.amount)
        if abs(amt_num - order.amount_paise) <= 10:
            pass
        elif abs((amt_num * 100) - order.amount_paise) <= 10:
            pass
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "AMOUNT_MISMATCH", "message": f"Payment amount ({status_res.amount}) does not match order record ({order.amount_paise} paise)."},
            )

    now = datetime.now(UTC)
    order.status = PaymentOrderStatus.PAID.value
    order.updated_at = now

    sub = session.execute(
        select(MemberSubscription).where(MemberSubscription.id == order.subscription_id)
    ).scalar_one_or_none()
    if sub:
        if sub.tenant_id != caller.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_COMMUNITY_PAYMENT_FORBIDDEN", "message": "Subscription belongs to a different community."},
            )
        plan = session.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id)).scalar_one_or_none()
        if plan and plan.community_id != caller.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_COMMUNITY_PLAN_FORBIDDEN", "message": "Plan belongs to a different community."},
            )
        sub.status = SubscriptionStatus.ACTIVE.value
        sub.payment_status = SubscriptionPaymentStatus.SETTLED.value
        sub.starts_at = now
        sub.expires_at = now + timedelta(days=sub.snapshot_duration_days or 30)
        sub.updated_at = now

    # Fetch subscriber details for notification
    sub_prof = session.execute(
        select(
            SubscriberProfile,
            TenantMembership.mobile_e164,
            Tenant.slug.label("tenant_slug"),
        )
        .join(TenantMembership, TenantMembership.id == SubscriberProfile.membership_id)
        .join(Tenant, Tenant.id == SubscriberProfile.tenant_id)
        .where(SubscriberProfile.membership_id == order.subscriber_membership_id)
    ).first()

    # Prevent duplicate welcome messages on retries or repeated callbacks
    order_notes = dict(order.notes or {})
    already_notified = bool(order_notes.get("welcome_notified"))

    if sub_prof and not already_notified:
        subscriber, mobile, t_slug = sub_prof
        notif_client = NotificationServiceClient(settings=settings)
        await notif_client.send_subscriber_welcome(
            subscriber_name=subscriber.full_name,
            mobile_e164=mobile,
            whatsapp_number=subscriber.whatsapp_number,
            whatsapp_consent=subscriber.whatsapp_consent,
            email=subscriber.email,
            plan_name=sub.snapshot_plan_name if sub else "Standard",
            amount_paid=order.amount,
            community_name=t_slug or "Community OTT",
        )
        order_notes["welcome_notified"] = True
        order_notes["welcome_notified_at"] = now.isoformat()
        order.notes = order_notes

    session.commit()

    return PaymentConfirmResponse(
        success=True,
        status="paid",
        subscription_status="ACTIVE",
        message="Payment verified successfully and subscription activated.",
        payment_order_id=order.id,
    )


@router.get(
    "/agent/collections/summary",
    response_model=AgentCollectionSummary,
    summary="Agent: Get collection performance and subscription statistics",
)
def get_agent_collection_summary(
    caller: Annotated[ResolvedSession, Depends(require_agent)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentCollectionSummary:
    tenant_id = caller.tenant_id
    if not tenant_id or not caller.membership_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Agent membership context required."},
        )

    total_subscribers = session.execute(
        select(func.count(SubscriberProfile.id)).where(
            SubscriberProfile.tenant_id == tenant_id,
            SubscriberProfile.registered_by_agent_membership_id == caller.membership_id,
        )
    ).scalar() or 0

    active_subs = session.execute(
        select(func.count(MemberSubscription.id))
        .join(SubscriberProfile, SubscriberProfile.membership_id == MemberSubscription.subscriber_membership_id)
        .where(
            SubscriberProfile.tenant_id == tenant_id,
            SubscriberProfile.registered_by_agent_membership_id == caller.membership_id,
            MemberSubscription.status == SubscriptionStatus.ACTIVE.value,
        )
    ).scalar() or 0

    total_collected = session.execute(
        select(func.coalesce(func.sum(PaymentOrder.amount), Decimal("0.00"))).where(
            PaymentOrder.tenant_id == tenant_id,
            PaymentOrder.created_by_membership_id == caller.membership_id,
            PaymentOrder.status == PaymentOrderStatus.PAID.value,
        )
    ).scalar() or Decimal("0.00")

    target_amount = Decimal("50000.00")
    pct = float((total_collected / target_amount) * 100) if target_amount > 0 else 0.0

    return AgentCollectionSummary(
        total_subscribers=total_subscribers,
        active_subscriptions=active_subs,
        total_collected_amount=total_collected,
        currency="INR",
        target_amount=target_amount,
        target_achievement_percentage=round(min(pct, 100.0), 2),
    )


@router.get(
    "/agent/users",
    response_model=UserListResponse,
    summary="Agent: List subscribers registered by this Agent",
)
def list_agent_users(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_READ))],
    session: Annotated[Session, Depends(get_db)],
) -> UserListResponse:
    tenant = session.execute(select(Tenant).where(Tenant.id == caller.tenant_id)).scalar_one_or_none()
    slug = tenant.slug if tenant else ""

    rows = session.execute(
        select(
            SubscriberProfile,
            TenantMembership.mobile_e164,
            MemberSubscription.status.label("sub_status"),
            MemberSubscription.snapshot_plan_name.label("sub_plan_name"),
            PaymentOrder.status.label("pay_status"),
            PaymentOrder.amount.label("pay_amount"),
            PaymentOrder.custom_amount.label("is_custom"),
            PaymentOrder.id.label("order_id"),
        )
        .join(TenantMembership, TenantMembership.id == SubscriberProfile.membership_id)
        .outerjoin(MemberSubscription, MemberSubscription.subscriber_membership_id == SubscriberProfile.membership_id)
        .outerjoin(PaymentOrder, PaymentOrder.subscription_id == MemberSubscription.id)
        .where(
            SubscriberProfile.tenant_id == caller.tenant_id,
            SubscriberProfile.registered_by_agent_membership_id == caller.membership_id,
        )
        .order_by(SubscriberProfile.created_at.desc())
    ).all()

    items = []
    seen_ids = set()
    for row in rows:
        sub_prof = row.SubscriberProfile
        if sub_prof.id in seen_ids:
            continue
        seen_ids.add(sub_prof.id)

        clean_m = row.mobile_e164 or ""
        masked_m = f"{clean_m[:5]}*** **{clean_m[-3:]}" if len(clean_m) >= 13 else clean_m
        pkg_type = "Custom" if row.is_custom else "Plan"

        items.append(
            UserSummary(
                id=sub_prof.id,
                tenant_id=sub_prof.tenant_id,
                community_slug=slug,
                membership_id=sub_prof.membership_id,
                user_id=sub_prof.user_id,
                full_name=sub_prof.full_name,
                mobile_e164=clean_m,
                masked_mobile=masked_m,
                whatsapp_number=sub_prof.whatsapp_number,
                whatsapp_verified=sub_prof.whatsapp_verified,
                whatsapp_consent=sub_prof.whatsapp_consent,
                email=sub_prof.email,
                email_verified=sub_prof.email_verified,
                address=sub_prof.address,
                photo_key=sub_prof.photo_key,
                package_plan=row.sub_plan_name or sub_prof.package_plan,
                package_type=pkg_type,
                billing_cycle=sub_prof.billing_cycle,
                status=sub_prof.status,
                payment_status=row.pay_status or "pending",
                subscription_status=row.sub_status or "PENDING",
                payment_amount=row.pay_amount,
                payment_order_id=row.order_id,
                registered_by_agent_membership_id=sub_prof.registered_by_agent_membership_id,
                created_at=sub_prof.created_at,
            )
        )

    return UserListResponse(items=items, total=len(items))


@router.get("/agent/subscribers", response_model=UserListResponse, include_in_schema=False)
def list_agent_subscribers_alias(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.MEMBER_READ))],
    session: Annotated[Session, Depends(get_db)],
) -> UserListResponse:
    return list_agent_users(caller=caller, session=session)
