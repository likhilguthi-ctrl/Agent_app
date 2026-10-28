from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.encryption import (
    decrypt_identity_number,
    encrypt_identity_number,
    mask_aadhaar,
    mask_pan,
    validate_aadhaar_number,
    validate_pan_number,
)
from app.db.session import get_db
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.models import AccountCredential, AuthSession
from app.modules.authentication.otp_service import OtpService
from app.modules.authentication.password import hash_password
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.tenants.hostname import normalize_hostname
from app.modules.tenants.locations import (
    CANONICAL_LOCATIONS,
    SUPPORTED_STATES,
    seed_canonical_regions_for_tenant,
)
from app.modules.tenants.models import (
    AdminProfile,
    Region,
    RegionAuditLog,
    Tenant,
    TenantDomain,
    UserRegionAssignment,
)
from app.modules.locations.models import District, Mandal, ManagerLocationAssignment, State
from app.modules.locations.schemas import StateScopeConfig
from app.modules.locations.service import LocationService
from app.modules.tenants.schemas import (
    AdministratorCreateRequest,
    AdministratorListResponse,
    AdministratorSummary,
    AdministratorUpdateRequest,
    CommunityCreateRequest,
    CommunityDetail,
    CommunityListResponse,
    CommunitySummary,
    CommunityUpdateRequest,
    DomainSummary,
    RegionCreateRequest,
    RegionListResponse,
    RegionSummary,
    UserRegionAssignRequest,
)

router = APIRouter(prefix="/platform/communities", tags=["platform communities"])

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]?$")
audit_service = AuditService()


def require_super_admin(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
) -> ResolvedSession:
    resolved, _ = session_data
    if resolved.role != "SUPER_ADMIN" or resolved.account_type != "platform_operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "SUPER_ADMIN_REQUIRED", "message": "Super Admin privileges required."},
        )
    return resolved


def require_super_admin_csrf(
    request: Request,
    response: Response,
    session: Annotated[ResolvedSession, Depends(require_super_admin)],
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token", max_length=64)] = None,
) -> ResolvedSession:
    response.headers["Cache-Control"] = "no-store"
    settings: Settings = request.app.state.settings
    origin = request.headers.get("origin")
    if (
        csrf_header is None
        or not secrets.compare_digest(csrf_header, session.csrf_token)
        or (origin and origin not in settings.frontend_origin_list)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "CSRF_VALIDATION_FAILED", "message": "Valid CSRF token required for state changes."},
        )
    return session


@router.get("", response_model=CommunityListResponse, summary="List communities with pagination and search")
def list_communities(
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    search: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status"),
) -> CommunityListResponse:
    query = select(Tenant)
    if search:
        clean_search = search.strip().lower()
        query = query.where(Tenant.slug.contains(clean_search))
    if status_filter:
        query = query.where(Tenant.status == status_filter.strip().lower())

    count_query = select(func.count()).select_from(query.subquery())
    total = session.execute(count_query).scalar_one()

    tenants = session.execute(
        query.order_by(Tenant.created_at.desc()).limit(limit).offset(offset)
    ).scalars().all()

    items: list[CommunitySummary] = []
    for tenant in tenants:
        domains_count = session.execute(
            select(func.count()).select_from(TenantDomain).where(TenantDomain.tenant_id == tenant.id)
        ).scalar_one()

        admins_count = session.execute(
            select(func.count(TenantMembership.id))
            .select_from(TenantMembership)
            .join(UserRoleAssignment, (UserRoleAssignment.membership_id == TenantMembership.id) & (UserRoleAssignment.tenant_id == tenant.id))
            .join(Role, (Role.id == UserRoleAssignment.role_id) & (Role.tenant_id == tenant.id))
            .where(
                TenantMembership.tenant_id == tenant.id,
                Role.code == "admin",
                UserRoleAssignment.status == "active",
            )
        ).scalar_one()

        items.append(
            CommunitySummary(
                id=tenant.id,
                slug=tenant.slug,
                status=tenant.status,
                created_at=tenant.created_at,
                updated_at=tenant.updated_at,
                domains_count=domains_count,
                administrators_count=admins_count,
            )
        )

    return CommunityListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=CommunityDetail, status_code=status.HTTP_201_CREATED, summary="Create a new community")
def create_community(
    body: CommunityCreateRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin_csrf)],
    session: Annotated[Session, Depends(get_db)],
) -> CommunityDetail:
    normalized_slug = body.slug.strip().lower()
    if not SLUG_PATTERN.match(normalized_slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_TENANT_SLUG", "message": "Slug must be lowercase alphanumeric with hyphens (1-63 chars)."},
        )

    existing = session.execute(
        select(Tenant).where(Tenant.slug == normalized_slug)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "TENANT_SLUG_CONFLICT", "message": f"Community with slug '{normalized_slug}' already exists."},
        )

    now = datetime.now(UTC)
    tenant_id = uuid4()
    tenant = Tenant(
        id=tenant_id,
        slug=normalized_slug,
        status="active" if body.status == "active" else "inactive",
        created_at=now,
        updated_at=now,
    )
    session.add(tenant)
    session.flush()

    domains: list[DomainSummary] = []
    if body.domain_hostname:
        try:
            normalized_domain = normalize_hostname(body.domain_hostname)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_HOSTNAME", "message": str(exc)},
            ) from exc

        existing_domain = session.execute(
            select(TenantDomain).where(TenantDomain.hostname == normalized_domain)
        ).scalar_one_or_none()
        if existing_domain is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "HOSTNAME_CONFLICT", "message": f"Hostname '{normalized_domain}' is already registered."},
            )

        domain_record = TenantDomain(
            id=uuid4(),
            tenant_id=tenant_id,
            hostname=normalized_domain,
            verification_status="verified",
            is_active=True,
            verified_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(domain_record)
        domains.append(
            DomainSummary(
                id=domain_record.id,
                hostname=domain_record.hostname,
                verification_status=domain_record.verification_status,
                is_active=domain_record.is_active,
                verified_at=domain_record.verified_at,
            )
        )

    # Automatically create the canonical tenant 'admin' and 'agent' roles with permissions
    from app.modules.authorization.seeding import seed_tenant_role_permissions
    admin_role, _ = seed_tenant_role_permissions(session, tenant_id)

    # Record mandatory audit event
    audit_service.record_mandatory(
        session,
        AuditEventDraft(
            event_type="platform.community.created",
            actor_category="platform_operator",
            actor_ref=str(operator.user_id),
            tenant_id=tenant_id,
            target_entity_type="tenant",
            target_entity_ref=str(tenant_id),
            action="community.create",
            outcome="success",
        ),
    )

    session.commit()

    photo_requirement = getattr(tenant, "photo_requirement", "OPTIONAL") or "OPTIONAL"
    default_commission_rate = getattr(tenant, "default_commission_rate", Decimal("10.00")) or Decimal("10.00")
    policy_version = getattr(tenant, "policy_version", "v1") or "v1"

    return CommunityDetail(
        id=tenant.id,
        slug=tenant.slug,
        status=tenant.status,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
        domains=domains,
        administrators_count=0,
        photo_requirement=photo_requirement,
        default_commission_rate=default_commission_rate,
        policy_version=policy_version,
        registration_policy={
            "mode": "standard",
            "photo_requirement": photo_requirement,
            "default_commission_rate": float(default_commission_rate),
            "policy_version": policy_version,
            "description": "Standard registration policy. Phase 5 configurable photo verification ready.",
        },
    )


@router.get("/{community_id}", response_model=CommunityDetail, summary="Get community details")
def get_community(
    community_id: UUID,
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> CommunityDetail:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    domain_records = session.execute(
        select(TenantDomain).where(TenantDomain.tenant_id == community_id)
    ).scalars().all()

    domains = [
        DomainSummary(
            id=d.id,
            hostname=d.hostname,
            verification_status=d.verification_status,
            is_active=d.is_active,
            verified_at=d.verified_at,
        )
        for d in domain_records
    ]

    admins_count = session.execute(
        select(func.count(TenantMembership.id))
        .select_from(TenantMembership)
        .join(UserRoleAssignment, (UserRoleAssignment.membership_id == TenantMembership.id) & (UserRoleAssignment.tenant_id == tenant.id))
        .join(Role, (Role.id == UserRoleAssignment.role_id) & (Role.tenant_id == tenant.id))
        .where(
            TenantMembership.tenant_id == tenant.id,
            Role.code == "admin",
            UserRoleAssignment.status == "active",
        )
    ).scalar_one()

    photo_requirement = getattr(tenant, "photo_requirement", "OPTIONAL") or "OPTIONAL"
    default_commission_rate = getattr(tenant, "default_commission_rate", Decimal("10.00")) or Decimal("10.00")
    policy_version = getattr(tenant, "policy_version", "v1") or "v1"

    return CommunityDetail(
        id=tenant.id,
        slug=tenant.slug,
        status=tenant.status,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
        domains=domains,
        administrators_count=admins_count,
        photo_requirement=photo_requirement,
        default_commission_rate=default_commission_rate,
        policy_version=policy_version,
        registration_policy={
            "mode": "standard",
            "photo_requirement": photo_requirement,
            "default_commission_rate": float(default_commission_rate),
            "policy_version": policy_version,
            "description": "Standard registration policy. Phase 5 configurable photo verification ready.",
        },
    )


@router.patch("/{community_id}", response_model=CommunityDetail, summary="Update community status")
def update_community(
    community_id: UUID,
    body: CommunityUpdateRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin_csrf)],
    session: Annotated[Session, Depends(get_db)],
) -> CommunityDetail:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    now = datetime.now(UTC)
    tenant.status = body.status
    tenant.updated_at = now

    if body.status == "inactive":
        # Revoke all active sessions for this community/tenant
        session.execute(
            update(AuthSession)
            .where(
                AuthSession.tenant_id == community_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    audit_service.record_mandatory(
        session,
        AuditEventDraft(
            event_type="platform.community.status_updated",
            actor_category="platform_operator",
            actor_ref=str(operator.user_id),
            tenant_id=community_id,
            target_entity_type="tenant",
            target_entity_ref=str(community_id),
            action="community.status.update",
            outcome="success",
        ),
    )

    session.commit()

    domain_records = session.execute(
        select(TenantDomain).where(TenantDomain.tenant_id == community_id)
    ).scalars().all()

    domains = [
        DomainSummary(
            id=d.id,
            hostname=d.hostname,
            verification_status=d.verification_status,
            is_active=d.is_active,
            verified_at=d.verified_at,
        )
        for d in domain_records
    ]

    admins_count = session.execute(
        select(func.count(TenantMembership.id))
        .select_from(TenantMembership)
        .join(UserRoleAssignment, (UserRoleAssignment.membership_id == TenantMembership.id) & (UserRoleAssignment.tenant_id == tenant.id))
        .join(Role, (Role.id == UserRoleAssignment.role_id) & (Role.tenant_id == tenant.id))
        .where(
            TenantMembership.tenant_id == tenant.id,
            Role.code == "admin",
            UserRoleAssignment.status == "active",
        )
    ).scalar_one()

    photo_requirement = getattr(tenant, "photo_requirement", "OPTIONAL") or "OPTIONAL"
    default_commission_rate = getattr(tenant, "default_commission_rate", Decimal("10.00")) or Decimal("10.00")
    policy_version = getattr(tenant, "policy_version", "v1") or "v1"

    return CommunityDetail(
        id=tenant.id,
        slug=tenant.slug,
        status=tenant.status,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
        domains=domains,
        administrators_count=admins_count,
        photo_requirement=photo_requirement,
        default_commission_rate=default_commission_rate,
        policy_version=policy_version,
        registration_policy={
            "mode": "standard",
            "photo_requirement": photo_requirement,
            "default_commission_rate": float(default_commission_rate),
            "policy_version": policy_version,
            "description": "Standard registration policy. Phase 5 configurable photo verification ready.",
        },
    )


platform_admin_router = APIRouter(prefix="/platform/administrators", tags=["platform administrators"])


def _build_administrator_summary(
    session: Session,
    *,
    user_id: UUID,
    username: str,
    membership_id: UUID,
    tenant_id: UUID,
    community_slug: str | None,
    mobile_e164: str,
    status_val: str,
    role_val: str,
    created_at_val: datetime,
    admin_prof: AdminProfile | None = None,
) -> AdministratorSummary:
    if admin_prof is None:
        admin_prof = session.execute(
            select(AdminProfile).where(
                AdminProfile.user_id == user_id,
                AdminProfile.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()

    region_rows = session.execute(
        select(Region.id, Region.name, Region.state)
        .join(
            UserRegionAssignment,
            (UserRegionAssignment.region_id == Region.id) & (UserRegionAssignment.tenant_id == tenant_id),
        )
        .where(
            UserRegionAssignment.user_id == user_id,
            UserRegionAssignment.status == "active",
        )
        .order_by(Region.name.asc())
    ).all()
    region_ids = [r[0] for r in region_rows]
    region_names = [r[1] for r in region_rows]
    admin_state = region_rows[0][2] if region_rows else None

    masked_aadhaar = None
    masked_pan = None
    if admin_prof:
        settings = get_settings()
        if admin_prof.aadhaar_number_encrypted:
            try:
                raw_aadhaar = (
                    admin_prof.aadhaar_number_encrypted.decode("utf-8")
                    if isinstance(admin_prof.aadhaar_number_encrypted, (bytes, memoryview))
                    else str(admin_prof.aadhaar_number_encrypted)
                )
                dec_aadhaar = decrypt_identity_number(raw_aadhaar, settings)
                masked_aadhaar = mask_aadhaar(dec_aadhaar)
            except Exception:
                masked_aadhaar = "XXXXXXXXXXXX"
        if admin_prof.pan_number_encrypted:
            try:
                raw_pan = (
                    admin_prof.pan_number_encrypted.decode("utf-8")
                    if isinstance(admin_prof.pan_number_encrypted, (bytes, memoryview))
                    else str(admin_prof.pan_number_encrypted)
                )
                dec_pan = decrypt_identity_number(raw_pan, settings)
                masked_pan = mask_pan(dec_pan)
            except Exception:
                masked_pan = "XXXXXXXXXX"

    mla_states = session.execute(
        select(State.name)
        .join(ManagerLocationAssignment, ManagerLocationAssignment.state_id == State.id)
        .where(
            ManagerLocationAssignment.tenant_id == tenant_id,
            ManagerLocationAssignment.user_id == user_id,
            ManagerLocationAssignment.status == "active",
        )
        .distinct()
    ).scalars().all()
    assigned_states_names = list(mla_states) if mla_states else ([admin_prof.state] if (admin_prof and admin_prof.state) else ([admin_state] if admin_state else []))

    return AdministratorSummary(
        user_id=user_id,
        username=username,
        membership_id=membership_id,
        tenant_id=tenant_id,
        community_slug=community_slug,
        first_name=admin_prof.first_name if admin_prof else None,
        last_name=admin_prof.last_name if admin_prof else None,
        mobile_e164=mobile_e164,
        whatsapp_number=admin_prof.whatsapp_number if admin_prof else None,
        whatsapp_verified=admin_prof.whatsapp_verified if admin_prof else False,
        email=admin_prof.email if admin_prof else None,
        email_verified=admin_prof.email_verified if admin_prof else False,
        address=admin_prof.address if admin_prof else None,
        state=admin_prof.state if (admin_prof and admin_prof.state) else admin_state,
        states=assigned_states_names,
        status=status_val,
        role=role_val.upper(),
        profile_photo_key=admin_prof.profile_photo_key if admin_prof else None,
        aadhaar_number_masked=masked_aadhaar,
        pan_number_masked=masked_pan,
        assigned_region_ids=region_ids,
        assigned_region_names=region_names,
        created_at=created_at_val,
    )


@platform_admin_router.get("", response_model=AdministratorListResponse, summary="List administrators across all communities")
def list_all_administrators(
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AdministratorListResponse:
    rows = session.execute(
        select(
            User.id.label("user_id"),
            AccountCredential.username.label("username"),
            TenantMembership.id.label("membership_id"),
            TenantMembership.tenant_id.label("tenant_id"),
            Tenant.slug.label("community_slug"),
            TenantMembership.mobile_e164.label("mobile_e164"),
            AccountCredential.status.label("status"),
            Role.code.label("role"),
            AccountCredential.created_at.label("created_at"),
        )
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .join(AccountCredential, (AccountCredential.user_id == User.id) & (AccountCredential.account_type == "tenant_user"))
        .join(UserRoleAssignment, (UserRoleAssignment.membership_id == TenantMembership.id) & (UserRoleAssignment.tenant_id == TenantMembership.tenant_id))
        .join(Role, (Role.id == UserRoleAssignment.role_id) & (Role.tenant_id == TenantMembership.tenant_id))
        .where(
            Role.code == "admin",
        )
        .order_by(AccountCredential.created_at.desc())
    ).all()

    items = [
        _build_administrator_summary(
            session=session,
            user_id=row.user_id,
            username=row.username,
            membership_id=row.membership_id,
            tenant_id=row.tenant_id,
            community_slug=row.community_slug,
            mobile_e164=row.mobile_e164,
            status_val=row.status,
            role_val=row.role,
            created_at_val=row.created_at,
        )
        for row in rows
    ]

    return AdministratorListResponse(items=items, total=len(items))


@platform_admin_router.get(
    "/{admin_user_id}",
    response_model=AdministratorSummary,
    summary="Get administrator profile across all communities",
)
def get_administrator_profile(
    admin_user_id: UUID,
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AdministratorSummary:
    row = session.execute(
        select(
            User.id.label("user_id"),
            AccountCredential.username.label("username"),
            TenantMembership.id.label("membership_id"),
            TenantMembership.tenant_id.label("tenant_id"),
            Tenant.slug.label("community_slug"),
            TenantMembership.mobile_e164.label("mobile_e164"),
            AccountCredential.status.label("status"),
            Role.code.label("role"),
            AccountCredential.created_at.label("created_at"),
        )
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .join(AccountCredential, (AccountCredential.user_id == User.id) & (AccountCredential.account_type == "tenant_user"))
        .join(UserRoleAssignment, (UserRoleAssignment.membership_id == TenantMembership.id) & (UserRoleAssignment.tenant_id == TenantMembership.tenant_id))
        .join(Role, (Role.id == UserRoleAssignment.role_id) & (Role.tenant_id == TenantMembership.tenant_id))
        .where(
            User.id == admin_user_id,
            Role.code == "admin",
        )
    ).first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "ADMINISTRATOR_NOT_FOUND", "message": "Administrator profile not found."},
        )

    return _build_administrator_summary(
        session=session,
        user_id=row.user_id,
        username=row.username,
        membership_id=row.membership_id,
        tenant_id=row.tenant_id,
        community_slug=row.community_slug,
        mobile_e164=row.mobile_e164,
        status_val=row.status,
        role_val=row.role,
        created_at_val=row.created_at,
    )


@router.get("/{community_id}/administrators", response_model=AdministratorListResponse, summary="List community administrators")
@router.get("/{community_id}/managers", response_model=AdministratorListResponse, summary="List community managers")
def list_administrators(
    community_id: UUID,
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AdministratorListResponse:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    rows = session.execute(
        select(
            User.id.label("user_id"),
            AccountCredential.username.label("username"),
            TenantMembership.id.label("membership_id"),
            TenantMembership.tenant_id.label("tenant_id"),
            TenantMembership.mobile_e164.label("mobile_e164"),
            AccountCredential.status.label("status"),
            Role.code.label("role"),
            AccountCredential.created_at.label("created_at"),
        )
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(AccountCredential, (AccountCredential.user_id == User.id) & (AccountCredential.account_type == "tenant_user"))
        .join(UserRoleAssignment, (UserRoleAssignment.membership_id == TenantMembership.id) & (UserRoleAssignment.tenant_id == community_id))
        .join(Role, (Role.id == UserRoleAssignment.role_id) & (Role.tenant_id == community_id))
        .where(
            TenantMembership.tenant_id == community_id,
            Role.code == "admin",
        )
        .order_by(AccountCredential.created_at.desc())
    ).all()

    items = [
        _build_administrator_summary(
            session=session,
            user_id=row.user_id,
            username=row.username,
            membership_id=row.membership_id,
            tenant_id=row.tenant_id,
            community_slug=tenant.slug,
            mobile_e164=row.mobile_e164,
            status_val=row.status,
            role_val=row.role,
            created_at_val=row.created_at,
        )
        for row in rows
    ]

    return AdministratorListResponse(items=items, total=len(items))


@router.get(
    "/{community_id}/administrators/{admin_user_id}",
    response_model=AdministratorSummary,
    summary="Get community administrator profile",
)
def get_community_administrator_profile(
    community_id: UUID,
    admin_user_id: UUID,
    _: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AdministratorSummary:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    row = session.execute(
        select(
            User.id.label("user_id"),
            AccountCredential.username.label("username"),
            TenantMembership.id.label("membership_id"),
            TenantMembership.tenant_id.label("tenant_id"),
            TenantMembership.mobile_e164.label("mobile_e164"),
            AccountCredential.status.label("status"),
            Role.code.label("role"),
            AccountCredential.created_at.label("created_at"),
        )
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(AccountCredential, (AccountCredential.user_id == User.id) & (AccountCredential.account_type == "tenant_user"))
        .join(UserRoleAssignment, (UserRoleAssignment.membership_id == TenantMembership.id) & (UserRoleAssignment.tenant_id == community_id))
        .join(Role, (Role.id == UserRoleAssignment.role_id) & (Role.tenant_id == community_id))
        .where(
            TenantMembership.tenant_id == community_id,
            User.id == admin_user_id,
            Role.code == "admin",
        )
    ).first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "ADMINISTRATOR_NOT_FOUND", "message": "Administrator profile not found in this community."},
        )

    return _build_administrator_summary(
        session=session,
        user_id=row.user_id,
        username=row.username,
        membership_id=row.membership_id,
        tenant_id=row.tenant_id,
        community_slug=tenant.slug,
        mobile_e164=row.mobile_e164,
        status_val=row.status,
        role_val=row.role,
        created_at_val=row.created_at,
    )


@router.post(
    "/{community_id}/administrators",
    response_model=AdministratorSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new community administrator",
)
@router.post(
    "/{community_id}/managers",
    response_model=AdministratorSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new community manager",
)
def create_administrator(
    community_id: UUID,
    body: AdministratorCreateRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin_csrf)],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> AdministratorSummary:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    settings: Settings = request.app.state.settings

    assigned_states: list[str] = []
    if body.location_scopes:
        for cfg in body.location_scopes:
            st = session.execute(select(State).where(State.id == cfg.state_id, State.is_active == True)).scalar_one_or_none()
            if not st:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "INVALID_STATE", "message": f"State '{cfg.state_id}' is invalid."})
            assigned_states.append(st.name)
        clean_state = assigned_states[0] if assigned_states else "Andhra Pradesh"
    elif body.states:
        for s in body.states:
            s_clean = s.strip()
            if s_clean not in SUPPORTED_STATES:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "INVALID_STATE", "message": f"State '{s}' is not supported. Supported states: {', '.join(SUPPORTED_STATES)}"})
            assigned_states.append(s_clean)
        clean_state = assigned_states[0] if assigned_states else "Andhra Pradesh"
    elif body.state:
        clean_state = body.state.strip()
        if clean_state not in SUPPORTED_STATES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_STATE", "message": f"State '{body.state}' is not supported. Supported states: {', '.join(SUPPORTED_STATES)}"},
            )
        assigned_states.append(clean_state)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "STATE_REQUIRED", "message": "State is mandatory for manager registration."})

    if body.region_ids:
        for r_id in body.region_ids:
            reg = session.execute(
                select(Region).where(Region.id == r_id, Region.tenant_id == community_id)
            ).scalar_one_or_none()
            if reg is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "REGION_NOT_FOUND", "message": f"Region '{r_id}' not found."},
                )
            if reg.state and reg.state not in assigned_states:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "DISTRICT_STATE_MISMATCH",
                        "message": f"District '{reg.name}' ({reg.state}) does not belong to selected state '{clean_state}'.",
                    },
                )

    if body.password is not None and len(body.password.strip()) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "PASSWORD_POLICY_VIOLATION", "message": "Password must be at least 8 characters long."},
        )

    # Validate and encrypt Aadhaar / PAN
    masked_aadhaar = None
    enc_aadhaar = None
    if body.aadhaar_number:
        try:
            clean_aadhaar = validate_aadhaar_number(body.aadhaar_number)
            masked_aadhaar = mask_aadhaar(clean_aadhaar)
            enc_aadhaar = encrypt_identity_number(clean_aadhaar, settings)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_AADHAAR", "message": str(exc)},
            )

    masked_pan = None
    enc_pan = None
    if body.pan_number:
        try:
            clean_pan = validate_pan_number(body.pan_number)
            masked_pan = mask_pan(clean_pan)
            enc_pan = encrypt_identity_number(clean_pan, settings)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "INVALID_PAN", "message": str(exc)},
            )

    clean_username = body.username.strip().lower()
    existing_username = session.execute(
        select(AccountCredential).where(AccountCredential.username == clean_username)
    ).scalar_one_or_none()
    if existing_username is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "USERNAME_CONFLICT", "message": f"Username '{clean_username}' is already in use."},
        )

    existing_mobile = session.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == community_id,
            TenantMembership.mobile_e164 == body.mobile_e164,
        )
    ).scalar_one_or_none()
    if existing_mobile is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "MOBILE_CONFLICT", "message": f"Mobile number '{body.mobile_e164}' is already registered in this community."},
        )

    # Mobile OTP verification (mandatory for Admin)
    if not body.verification_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "MOBILE_TOKEN_REQUIRED", "message": "Mobile number must be verified with an OTP before creating an administrator."},
        )
    otp_service = OtpService(settings=settings)
    valid = otp_service.validate_and_consume_registration_token(
        session=session,
        verification_token=body.verification_token,
        mobile_number=body.mobile_e164,
        expected_purpose="admin_registration",
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_OTP_TOKEN", "message": "Mobile number verification token is invalid, expired, or already used."},
        )

    # WhatsApp number verification
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

    # Email verification (Mandatory for Admin)
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
            detail={"error": "EMAIL_TOKEN_REQUIRED", "message": "Email verification with OTP is mandatory for Admin registration."},
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

    now = datetime.now(UTC)
    user_id = uuid4()
    membership_id = uuid4()

    # 1. User
    user = User(
        id=user_id,
        status=body.status,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.flush()

    # 2. TenantMembership
    membership = TenantMembership(
        id=membership_id,
        tenant_id=community_id,
        user_id=user_id,
        mobile_e164=body.mobile_e164,
        status=body.status,
        created_at=now,
        updated_at=now,
    )
    session.add(membership)

    # 3. Ensure community has an 'admin' role and standard role permissions
    from app.modules.authorization.seeding import seed_tenant_role_permissions
    admin_role, _ = seed_tenant_role_permissions(session, community_id)

    # 4. UserRoleAssignment
    assignment = UserRoleAssignment(
        id=uuid4(),
        tenant_id=community_id,
        membership_id=membership_id,
        role_id=admin_role.id,
        assigned_by_membership_id=membership_id,
        status=body.status,
        valid_from=now,
        valid_until=None,
        created_at=now,
        updated_at=now,
    )
    session.add(assignment)

    # 5. AccountCredential
    password_val = body.password or secrets.token_urlsafe(16)
    credential = AccountCredential(
        id=uuid4(),
        username=clean_username,
        password_hash=hash_password(password_val),
        account_type="tenant_user",
        user_id=user_id,
        operator_id=None,
        status=body.status,
        created_at=now,
        updated_at=now,
    )
    session.add(credential)

    # 6. AdminProfile
    admin_prof = AdminProfile(
        id=uuid4(),
        user_id=user_id,
        tenant_id=community_id,
        first_name=body.first_name.strip() if body.first_name else None,
        last_name=body.last_name.strip() if body.last_name else None,
        email=clean_email,
        email_verified=True,
        email_verified_at=now,
        whatsapp_number=final_whatsapp_number,
        whatsapp_verified=whatsapp_verified,
        state=clean_state,
        address=body.address.strip() if body.address else None,
        profile_photo_key=body.profile_photo_key.strip() if body.profile_photo_key else None,
        aadhaar_document_key=body.aadhaar_document_key.strip() if body.aadhaar_document_key else None,
        pan_document_key=body.pan_document_key.strip() if body.pan_document_key else None,
        aadhaar_number_encrypted=enc_aadhaar.encode("utf-8") if isinstance(enc_aadhaar, str) else enc_aadhaar,
        pan_number_encrypted=enc_pan.encode("utf-8") if isinstance(enc_pan, str) else enc_pan,
        status=body.status,
        created_at=now,
        updated_at=now,
    )
    session.add(admin_prof)

    # 7. Region assignments
    assigned_regions: list[UUID] = []
    for r_id in body.region_ids:
        reg = session.execute(
            select(Region).where(Region.id == r_id, Region.tenant_id == community_id)
        ).scalar_one_or_none()
        if reg is not None:
            session.add(
                UserRegionAssignment(
                    id=uuid4(),
                    user_id=user_id,
                    tenant_id=community_id,
                    region_id=r_id,
                    status="active",
                    assigned_by_user_id=operator.user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                RegionAuditLog(
                    id=uuid4(),
                    tenant_id=community_id,
                    user_id=user_id,
                    region_id=r_id,
                    action="assigned",
                    actor_id=operator.user_id,
                    reason="admin_creation",
                    created_at=now,
                )
            )
            assigned_regions.append(r_id)

    # 7b. Location scope assignments
    if body.location_scopes:
        LocationService.save_manager_location_assignments(
            session=session,
            tenant_id=community_id,
            user_id=user_id,
            configs=body.location_scopes,
            assigned_by_user_id=operator.user_id,
        )
    else:
        configs = []
        for s_name in assigned_states:
            st_rec = session.execute(select(State).where(func.lower(State.name) == s_name.lower())).scalar_one_or_none()
            if st_rec:
                configs.append(StateScopeConfig(state_id=st_rec.id, all_districts=True))
        if configs:
            LocationService.save_manager_location_assignments(
                session=session,
                tenant_id=community_id,
                user_id=user_id,
                configs=configs,
                assigned_by_user_id=operator.user_id,
            )

    # 8. Audit event
    audit_service.record_mandatory(
        session,
        AuditEventDraft(
            event_type="platform.community.admin_created",
            actor_category="platform_operator",
            actor_ref=str(operator.user_id),
            tenant_id=community_id,
            target_entity_type="user",
            target_entity_ref=str(user_id),
            action="community.admin.create",
            outcome="success",
        ),
    )

    session.commit()

    return AdministratorSummary(
        user_id=user.id,
        username=clean_username,
        membership_id=membership.id,
        tenant_id=community_id,
        community_slug=tenant.slug,
        first_name=body.first_name,
        last_name=body.last_name,
        mobile_e164=body.mobile_e164,
        whatsapp_number=final_whatsapp_number,
        whatsapp_verified=whatsapp_verified,
        email=body.email,
        email_verified=True,
        address=body.address,
        state=clean_state,
        states=assigned_states,
        location_scopes=body.location_scopes,
        status=body.status,
        role="ADMIN",
        profile_photo_key=body.profile_photo_key,
        aadhaar_number_masked=masked_aadhaar,
        pan_number_masked=masked_pan,
        assigned_region_ids=assigned_regions,
        created_at=now,
    )


@router.patch(
    "/{community_id}/administrators/{admin_user_id}",
    response_model=AdministratorSummary,
    summary="Update administrator status",
)
def update_administrator(
    community_id: UUID,
    admin_user_id: UUID,
    body: AdministratorUpdateRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin_csrf)],
    session: Annotated[Session, Depends(get_db)],
) -> AdministratorSummary:
    tenant = session.execute(
        select(Tenant).where(Tenant.id == community_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "COMMUNITY_NOT_FOUND", "message": "Community not found."},
        )

    membership = session.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == community_id,
            TenantMembership.user_id == admin_user_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "ADMINISTRATOR_NOT_FOUND", "message": "Administrator not found in this community."},
        )

    credential = session.execute(
        select(AccountCredential).where(
            AccountCredential.user_id == admin_user_id,
            AccountCredential.account_type == "tenant_user",
        )
    ).scalar_one_or_none()
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "CREDENTIAL_NOT_FOUND", "message": "Administrator credentials not found."},
        )

    now = datetime.now(UTC)
    credential.status = body.status
    credential.updated_at = now

    user = session.execute(
        select(User).where(User.id == admin_user_id)
    ).scalar_one_or_none()
    if user is not None:
        user.status = body.status
        user.updated_at = now

    membership.status = body.status
    membership.updated_at = now

    session.execute(
        update(UserRoleAssignment)
        .where(
            UserRoleAssignment.tenant_id == community_id,
            UserRoleAssignment.membership_id == membership.id,
        )
        .values(status=body.status, updated_at=now)
    )

    if body.status == "inactive":
        # Revoke active sessions for this membership
        session.execute(
            update(AuthSession)
            .where(
                AuthSession.membership_id == membership.id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    audit_service.record_mandatory(
        session,
        AuditEventDraft(
            event_type="platform.community.admin_updated",
            actor_category="platform_operator",
            actor_ref=str(operator.user_id),
            tenant_id=community_id,
            target_entity_type="user",
            target_entity_ref=str(admin_user_id),
            action="community.admin.update",
            outcome="success",
        ),
    )

    session.commit()

    return _build_administrator_summary(
        session=session,
        user_id=admin_user_id,
        username=credential.username,
        membership_id=membership.id,
        tenant_id=community_id,
        community_slug=tenant.slug,
        mobile_e164=membership.mobile_e164,
        status_val=body.status,
        role_val="ADMIN",
        created_at_val=credential.created_at,
    )


# -----------------------------------------------------------------------------
# Regions Management
# -----------------------------------------------------------------------------
@router.get(
    "/{community_id}/regions",
    response_model=RegionListResponse,
    summary="List regions in a community",
)
def list_community_regions(
    community_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
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
            tenant_id=r.tenant_id,
            code=r.code,
            name=r.name,
            state=r.state,
            description=r.description,
            status=r.status,
            created_at=r.created_at,
        )
        for r in regions
    ]
    return RegionListResponse(items=items, total=len(items))


@router.get(
    "/{community_id}/states",
    summary="Super Admin: List states in a community",
)
def list_community_states(
    community_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    return {"items": SUPPORTED_STATES, "total": len(SUPPORTED_STATES)}


@router.post(
    "/{community_id}/regions",
    response_model=RegionSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Super Admin: Create a region in a community",
)
def create_community_region(
    community_id: UUID,
    body: RegionCreateRequest,
    caller: Annotated[ResolvedSession, Depends(require_super_admin_csrf)],
    session: Annotated[Session, Depends(get_db)],
) -> RegionSummary:
    tenant = session.execute(select(Tenant).where(Tenant.id == community_id)).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "COMMUNITY_NOT_FOUND"})

    clean_code = body.code.strip().upper()
    existing = session.execute(
        select(Region).where(Region.tenant_id == community_id, Region.code == clean_code)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "REGION_EXISTS", "message": f"Region '{clean_code}' already exists in this community."},
        )

    now = datetime.now(UTC)
    region = Region(
        id=uuid4(),
        tenant_id=community_id,
        code=clean_code,
        name=body.name.strip(),
        description=body.description.strip() if body.description else None,
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(region)
    session.commit()
    return RegionSummary(
        id=region.id,
        tenant_id=region.tenant_id,
        code=region.code,
        name=region.name,
        description=region.description,
        status=region.status,
        created_at=region.created_at,
    )


@router.post(
    "/{community_id}/users/{user_id}/regions",
    summary="Super Admin: Assign or update regions for a user",
)
def assign_user_regions(
    community_id: UUID,
    user_id: UUID,
    body: UserRegionAssignRequest,
    caller: Annotated[ResolvedSession, Depends(require_super_admin_csrf)],
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    now = datetime.now(UTC)
    existing_assignments = session.execute(
        select(UserRegionAssignment)
        .where(UserRegionAssignment.tenant_id == community_id, UserRegionAssignment.user_id == user_id)
    ).scalars().all()
    for ea in existing_assignments:
        ea.status = "inactive"
        session.add(
            RegionAuditLog(
                id=uuid4(),
                tenant_id=community_id,
                user_id=user_id,
                region_id=ea.region_id,
                action="revoked",
                actor_id=caller.user_id,
                reason="reassignment",
                created_at=now,
            )
        )

    assigned: list[str] = []
    for r_id in body.region_ids:
        reg = session.execute(
            select(Region).where(Region.id == r_id, Region.tenant_id == community_id)
        ).scalar_one_or_none()
        if reg is not None:
            session.add(
                UserRegionAssignment(
                    id=uuid4(),
                    user_id=user_id,
                    tenant_id=community_id,
                    region_id=r_id,
                    status="active",
                    assigned_by_user_id=caller.user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                RegionAuditLog(
                    id=uuid4(),
                    tenant_id=community_id,
                    user_id=user_id,
                    region_id=r_id,
                    action="assigned",
                    actor_id=caller.user_id,
                    reason="manual_assignment",
                    created_at=now,
                )
            )
            assigned.append(str(r_id))

    session.commit()
    return {"status": "success", "assigned_region_ids": assigned}


community_regions_router = APIRouter(prefix="/community/regions", tags=["community regions"])


@community_regions_router.get("", response_model=RegionListResponse, summary="List regions for caller's community")
def get_my_community_regions(
    caller: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> RegionListResponse:
    resolved, _ = caller
    if not resolved.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "TENANT_REQUIRED"})
    regions = session.execute(
        select(Region).where(Region.tenant_id == resolved.tenant_id, Region.status == "active").order_by(Region.code.asc())
    ).scalars().all()
    items = [
        RegionSummary(
            id=r.id,
            tenant_id=r.tenant_id,
            code=r.code,
            name=r.name,
            description=r.description,
            status=r.status,
            created_at=r.created_at,
        )
        for r in regions
    ]
    return RegionListResponse(items=items, total=len(items))

