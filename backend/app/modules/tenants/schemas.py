from __future__ import annotations

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field

from app.modules.locations.schemas import StateScopeConfig


from decimal import Decimal


class DomainSummary(BaseModel):
    id: UUID
    hostname: str
    verification_status: str
    is_active: bool
    verified_at: datetime | None = None


class CommunitySummary(BaseModel):
    id: UUID
    slug: str
    status: str
    created_at: datetime
    updated_at: datetime
    domains_count: int = 0
    administrators_count: int = 0


class CommunityDetail(BaseModel):
    id: UUID
    slug: str
    status: str
    created_at: datetime
    updated_at: datetime
    domains: list[DomainSummary] = Field(default_factory=list)
    administrators_count: int = 0
    photo_requirement: str = "OPTIONAL"
    default_commission_rate: Decimal = Decimal("10.00")
    policy_version: str | int = "v1"
    registration_policy: dict[str, object] = Field(
        default_factory=lambda: {
            "mode": "standard",
            "photo_requirement": "OPTIONAL",
            "description": "Standard registration policy. Phase 5 configurable photo verification ready.",
        }
    )


class CommunityListResponse(BaseModel):
    items: list[CommunitySummary]
    total: int
    limit: int
    offset: int


class CommunityCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=63)
    domain_hostname: str | None = Field(default=None, max_length=253)
    status: str = Field(default="active")


class CommunityUpdateRequest(BaseModel):
    status: str = Field(pattern="^(active|inactive)$")


class AdministratorSummary(BaseModel):
    user_id: UUID
    username: str
    membership_id: UUID
    tenant_id: UUID
    community_slug: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    mobile_e164: str
    whatsapp_number: str | None = None
    whatsapp_verified: bool = False
    email: str | None = None
    email_verified: bool = False
    address: str | None = None
    state: str | None = None
    status: str
    role: str
    profile_photo_key: str | None = None
    aadhaar_number_masked: str | None = None
    pan_number_masked: str | None = None
    assigned_region_ids: list[UUID] = Field(default_factory=list)
    assigned_region_names: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    location_scopes: list[StateScopeConfig] = Field(default_factory=list)
    created_at: datetime


class AdministratorListResponse(BaseModel):
    items: list[AdministratorSummary]
    total: int


class AdministratorCreateRequest(BaseModel):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    username: str = Field(min_length=3, max_length=100)
    password: str | None = Field(default=None, max_length=128)
    mobile_e164: str = Field(min_length=13, max_length=13, pattern=r"^\+91[6-9]\d{9}$")
    email: str = Field(min_length=5, max_length=255)
    email_verification_token: str | None = None
    whatsapp_same_as_mobile: bool = Field(default=True)
    whatsapp_number: str | None = Field(default=None, max_length=16, pattern=r"^\+91[6-9]\d{9}$")
    whatsapp_verification_token: str | None = None
    address: str | None = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=100)
    states: list[str] = Field(default_factory=list)
    location_scopes: list[StateScopeConfig] = Field(default_factory=list)
    aadhaar_number: str | None = Field(default=None, max_length=14)
    aadhaar_document_key: str | None = Field(default=None, max_length=64)
    pan_number: str | None = Field(default=None, max_length=10)
    pan_document_key: str | None = Field(default=None, max_length=64)
    profile_photo_key: str | None = Field(default=None, max_length=64)
    status: str = Field(default="active", pattern="^(active|inactive)$")
    region_ids: list[UUID] = Field(default_factory=list)
    verification_token: str | None = None


ManagerCreateRequest = AdministratorCreateRequest
ManagerSummary = AdministratorSummary
ManagerListResponse = AdministratorListResponse


class AdministratorUpdateRequest(BaseModel):
    status: str = Field(pattern="^(active|inactive)$")


class RegionSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    code: str
    name: str
    state: str = "Andhra Pradesh"
    description: str | None = None
    status: str
    created_at: datetime


class RegionListResponse(BaseModel):
    items: list[RegionSummary]
    total: int


class RegionCreateRequest(BaseModel):
    code: str = Field(min_length=2, max_length=50)
    name: str = Field(min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=255)


class UserRegionAssignRequest(BaseModel):
    region_ids: list[UUID]


