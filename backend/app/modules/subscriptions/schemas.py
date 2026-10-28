from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.subscriptions.models import PlanStatus, SubscriptionPaymentStatus, SubscriptionStatus


class PlanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    community_id: UUID | None = None
    code: str = Field(min_length=2, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    benefits: list[str] | dict[str, Any] | None = None
    duration_days: int = Field(ge=1, le=3650)
    nominal_price: Decimal | None = None
    currency: str = Field(default="INR", min_length=3, max_length=3)
    status: PlanStatus = PlanStatus.ACTIVE

    @field_validator("nominal_price")
    @classmethod
    def validate_nominal_price(cls, v: Decimal | None) -> Decimal | None:
        if v is not None:
            if v < Decimal("0.00"):
                raise ValueError("nominal_price cannot be negative")
            if v.as_tuple().exponent < -2:
                raise ValueError("nominal_price cannot have more than 2 decimal places")
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        return v.upper()


class PlanUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    benefits: list[str] | dict[str, Any] | None = None
    duration_days: int | None = Field(default=None, ge=1, le=3650)
    nominal_price: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    status: PlanStatus | None = None

    @field_validator("nominal_price")
    @classmethod
    def validate_nominal_price(cls, v: Decimal | None) -> Decimal | None:
        if v is not None:
            if v < Decimal("0.00"):
                raise ValueError("nominal_price cannot be negative")
            if v.as_tuple().exponent < -2:
                raise ValueError("nominal_price cannot have more than 2 decimal places")
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str | None) -> str | None:
        return v.upper() if v else None


class PlanSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    community_id: UUID
    community_name: str | None = None
    code: str
    name: str
    description: str | None
    benefits: Any | None
    duration_days: int
    status: str
    nominal_price: Decimal | None
    currency: str
    created_at: datetime
    updated_at: datetime


class PlanListResponse(BaseModel):
    items: list[PlanSummary]
    total: int


class SubscriptionCreateRequest(BaseModel):
    """Input payload for creating a subscription.

    The agreed_amount is a manual business input and is never automatically
    derived from a plan price.
    """

    model_config = ConfigDict(extra="forbid")

    subscriber_membership_id: UUID
    plan_id: UUID
    agreed_amount: Decimal = Field(
        description="Manually agreed subscription amount. Exact monetary representation."
    )
    currency: str = Field(default="INR", min_length=3, max_length=3)
    starts_at: datetime | None = None
    notes: Any | None = Field(default=None)

    @field_validator("agreed_amount")
    @classmethod
    def validate_agreed_amount(cls, v: Decimal) -> Decimal:
        if v < Decimal("0.00"):
            raise ValueError("agreed_amount cannot be negative")
        if v.as_tuple().exponent < -2:
            raise ValueError("agreed_amount cannot have more than 2 decimal places")
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        return v.upper()


class SubscriptionCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)


class SubscriptionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    subscriber_membership_id: UUID
    plan_id: UUID
    agreed_amount: Decimal
    currency: str
    starts_at: datetime
    expires_at: datetime
    status: str
    payment_status: str
    snapshot_plan_name: str
    snapshot_duration_days: int
    notes: Any | None = None
    created_by_user_id: UUID
    created_at: datetime
    updated_at: datetime


class SubscriptionListResponse(BaseModel):
    items: list[SubscriptionSummary]
    total: int
    limit: int
    offset: int

