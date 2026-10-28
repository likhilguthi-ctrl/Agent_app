from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, Field


class AgentCreateRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=100)
    password: str | None = Field(default=None, max_length=128)
    mobile_e164: str = Field(min_length=13, max_length=13, pattern=r"^\+91[6-9]\d{9}$")
    email: str = Field(min_length=5, max_length=255)
    email_verification_token: str | None = None
    whatsapp_same_as_mobile: bool = Field(default=True)
    whatsapp_number: str | None = Field(default=None, max_length=16, pattern=r"^\+91[6-9]\d{9}$")
    whatsapp_verification_token: str | None = None
    address: str | None = Field(default=None, max_length=500)
    state: str = Field(min_length=1, max_length=100)
    district: str = Field(min_length=1, max_length=100)
    mandal: str | None = Field(default=None, max_length=100)
    state_id: UUID | None = None
    district_id: UUID | None = None
    mandal_id: UUID | None = None
    manager_user_id: UUID | None = None
    creation_request_id: UUID | None = None
    aadhaar_number: str = Field(min_length=12, max_length=14)
    aadhaar_document_key: str = Field(min_length=1, max_length=64)
    pan_number: str = Field(min_length=10, max_length=10)
    pan_document_key: str = Field(min_length=1, max_length=64)
    profile_photo_key: str = Field(min_length=1, max_length=64)
    commission_rate_override: Decimal | None = Field(default=None, ge=0, le=100)
    status: str = Field(default="active", pattern="^(active|inactive)$")
    region_ids: list[UUID] = Field(default_factory=list)
    verification_token: str | None = None


class AgentSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    community_slug: str
    membership_id: UUID
    user_id: UUID
    username: str
    first_name: str
    last_name: str
    mobile_e164: str
    whatsapp_number: str | None = None
    whatsapp_verified: bool = False
    email: str | None = None
    email_verified: bool = False
    address: str | None = None
    state: str | None = None
    district: str | None = None
    mandal: str | None = None
    state_id: UUID | None = None
    district_id: UUID | None = None
    mandal_id: UUID | None = None
    manager_user_id: UUID | None = None
    manager_name: str | None = None
    parent_agent_membership_id: UUID | None = None
    ref_id: str
    status: str
    effective_commission_rate: Decimal
    commission_rate_override: Decimal | None = None
    profile_photo_key: str
    aadhaar_number_masked: str
    aadhaar_document_key: str
    pan_number_masked: str
    pan_document_key: str
    assigned_region_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime
    pending_appointment_id: UUID | None = None


class AgentListResponse(BaseModel):
    items: list[AgentSummary]
    total: int


class AgentUpdateRequest(BaseModel):
    status: str | None = Field(default=None, description="Deprecated; direct status updates are rejected. Use agent lifecycle requests.")
    commission_rate_override: Decimal | None = Field(default=None, ge=0, le=100)
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    region_ids: list[UUID] | None = None


class UserRegistrationRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    mobile_e164: str = Field(min_length=13, max_length=13, pattern=r"^\+91[6-9]\d{9}$")
    verification_token: str | None = None
    whatsapp_same_as_mobile: bool = Field(default=True)
    whatsapp_number: str | None = Field(default=None, max_length=16, pattern=r"^\+91[6-9]\d{9}$")
    whatsapp_verification_token: str | None = None
    whatsapp_consent: bool = Field(default=False)
    email: str | None = Field(default=None, max_length=255)
    email_verification_token: str | None = None
    address: str | None = Field(default=None, max_length=500)
    photo_key: str | None = Field(default=None, max_length=64)
    package_plan: str | None = Field(default="Standard", max_length=50)
    plan_id: UUID | None = None
    custom_amount: Decimal | None = None
    billing_cycle: str | None = Field(default="monthly", max_length=20)
    initiate_payment: bool = Field(default=True)
    callback_url: str | None = None


class UserSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    community_slug: str
    membership_id: UUID
    user_id: UUID
    full_name: str
    mobile_e164: str
    masked_mobile: str | None = None
    whatsapp_number: str | None = None
    whatsapp_verified: bool = False
    whatsapp_consent: bool = False
    email: str | None = None
    email_verified: bool = False
    address: str | None = None
    photo_key: str | None = None
    package_plan: str | None = None
    package_type: str | None = None
    billing_cycle: str | None = None
    status: str
    payment_status: str | None = None
    subscription_status: str | None = None
    payment_amount: Decimal | None = None
    payment_order_id: UUID | None = None
    payment_page_url: str | None = None
    external_payment_id: str | None = None
    registered_by_agent_membership_id: UUID
    created_at: datetime


class SubscriberPaymentInitiationResponse(BaseModel):
    subscriber: UserSummary
    subscription_id: UUID
    payment_order_id: UUID
    external_payment_id: str
    payment_page_url: str
    amount: Decimal
    currency: str = "INR"
    status: str


class PaymentConfirmRequest(BaseModel):
    payment_order_id: UUID | None = None
    external_payment_id: str | None = None


class PaymentConfirmResponse(BaseModel):
    success: bool
    status: str
    message: str
    subscription_status: str
    payment_order_id: UUID


class AgentCollectionSummary(BaseModel):
    total_subscribers: int
    active_subscriptions: int
    total_collected_amount: Decimal
    currency: str = "INR"
    target_amount: Decimal | None = None
    target_achievement_percentage: float | None = None


class UserListResponse(BaseModel):
    items: list[UserSummary]
    total: int


class CommunityPolicyResponse(BaseModel):
    photo_requirement: str
    default_commission_rate: Decimal
    policy_version: str


class PhotoPolicyUpdateRequest(BaseModel):
    photo_requirement: str = Field(pattern="^(OPTIONAL|REQUIRED)$")


class CommissionUpdateRequest(BaseModel):
    default_commission_rate: Decimal = Field(ge=0, le=100)


class CommissionAuditSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    agent_profile_id: UUID | None = None
    changed_by_username: str
    scope: str
    previous_rate: Decimal | None = None
    new_rate: Decimal
    created_at: datetime


class CommissionAuditListResponse(BaseModel):
    items: list[CommissionAuditSummary]
    total: int

