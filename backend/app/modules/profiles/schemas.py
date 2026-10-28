"""Pydantic schemas for Role-Based Profile Editing and Management.

Defines unified view and update models across:
- Super Admin (PlatformOperator)
- Manager (AdminProfile)
- Agent & Child Agent (AgentProfile)
- Subscriber / User (SubscriberProfile)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LocationScopeItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state_id: UUID | None = None
    state_code: str | None = None
    state_name: str | None = None
    district_id: UUID | None = None
    district_code: str | None = None
    district_name: str | None = None
    mandal_id: UUID | None = None
    mandal_code: str | None = None
    mandal_name: str | None = None


class ProfileViewResponse(BaseModel):
    """Unified profile representation for any authenticated role."""

    model_config = ConfigDict(extra="ignore")

    profile_id: UUID
    user_id: UUID
    account_id: UUID | None = None
    membership_id: UUID | None = None
    tenant_id: UUID | None = None
    community_name: str | None = None
    username: str
    role: str  # SUPER_ADMIN, ADMIN, AGENT, CHILD_AGENT, MEMBER
    status: str

    # Personal details
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    mobile_e164: str
    email: str | None = None
    email_verified: bool = False
    whatsapp_number: str | None = None
    whatsapp_verified: bool = False
    address: str | None = None
    profile_photo_key: str | None = None
    profile_photo_url: str | None = None

    # Geographic assignments (Read-only on profile edit)
    state: str | None = None
    district: str | None = None
    mandal: str | None = None
    state_id: UUID | None = None
    district_id: UUID | None = None
    mandal_id: UUID | None = None
    assigned_locations: list[LocationScopeItem] = Field(default_factory=list)

    # Hierarchy details (Read-only on profile edit)
    manager_name: str | None = None
    manager_id: UUID | None = None
    parent_agent_name: str | None = None
    parent_agent_id: UUID | None = None
    registered_by_agent_name: str | None = None
    registered_by_agent_id: UUID | None = None

    # Subscriber specific fields (Read-only on profile edit)
    package_plan: str | None = None
    billing_cycle: str | None = None
    subscription_status: str | None = None
    payment_status: str | None = None

    # Metadata
    created_at: datetime
    updated_at: datetime
    editable_fields: list[str] = Field(default_factory=list)


class ProfileUpdateRequest(BaseModel):
    """Allowlist of editable fields. Sensitive fields are strictly forbidden."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    full_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=255)
    whatsapp_number: str | None = Field(default=None, max_length=16, pattern=r"^\+91[6-9]\d{9}$")
    address: str | None = Field(default=None, max_length=500)
    profile_photo_key: str | None = Field(default=None, max_length=64)

    # Mobile update requires verified OTP token
    mobile_e164: str | None = Field(default=None, max_length=13, pattern=r"^\+91[6-9]\d{9}$")
    mobile_verification_token: str | None = Field(default=None, max_length=128)


class MobileChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    new_mobile_number: str = Field(min_length=13, max_length=13, pattern=r"^\+91[6-9]\d{9}$")


class MobileChangeResponse(BaseModel):
    status: str = "accepted"
    challenge_id: UUID
    cooldown_seconds: int = 60
    message: str = "Verification code dispatched to your new mobile number."


class MobileChangeVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    challenge_id: UUID
    new_mobile_number: str = Field(min_length=13, max_length=13, pattern=r"^\+91[6-9]\d{9}$")
    otp: str = Field(min_length=4, max_length=10)


class MobileChangeVerifyResponse(BaseModel):
    status: str = "verified"
    verification_token: str
    message: str = "Mobile number verified successfully. You may now save your profile."

