from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, Field


class ApprovalDecisionSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    appointment_id: UUID
    decision: str
    decided_by_user_id: UUID
    reason: str | None = None
    decided_at: datetime


class AppointmentSubmitRequest(BaseModel):
    model_config = {"extra": "forbid"}

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=100)
    password: str | None = Field(default=None, max_length=128)
    mobile_e164: str = Field(min_length=9, max_length=16, pattern=r"^\+[1-9]\d{1,14}$")
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    aadhaar_number: str = Field(min_length=12, max_length=14)
    aadhaar_document_key: str = Field(min_length=1, max_length=64)
    pan_number: str = Field(min_length=10, max_length=10)
    pan_document_key: str = Field(min_length=1, max_length=64)
    profile_photo_key: str = Field(min_length=1, max_length=64)
    proposed_commission_rate: Decimal | None = Field(default=None, ge=0, le=100)
    commission_rate_override: Decimal | None = Field(default=None, ge=0, le=100)
    region_ids: list[UUID] = Field(default_factory=list)
    role_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)
    verification_token: str | None = None


class AppointmentDecisionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str | None = Field(default=None, max_length=1000)


class AppointmentSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    user_id: UUID
    membership_id: UUID
    agent_profile_id: UUID
    requested_by_user_id: UUID
    role_id: UUID
    status: str
    proposed_commission_rate: Decimal | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    candidate_name: str | None = None
    candidate_username: str | None = None
    candidate_mobile: str | None = None
    candidate_ref_id: str | None = None
    decisions: list[ApprovalDecisionSummary] = Field(default_factory=list)


class AppointmentListResponse(BaseModel):
    items: list[AppointmentSummary]
    total: int

