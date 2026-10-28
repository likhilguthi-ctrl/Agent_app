from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PaymentOrderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: UUID = Field(..., description="ID of the pending member subscription.")
    idempotency_key: str | None = Field(
        None, max_length=128, description="Optional client idempotency key to prevent duplicate orders."
    )
    notes: dict[str, Any] | None = Field(None, description="Optional metadata key-values.")


class PaymentOrderSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    subscription_id: UUID
    subscriber_membership_id: UUID
    gateway: str
    gateway_order_id: str
    amount_paise: int
    amount: Decimal
    currency: str
    status: str
    key_id: str | None = Field(None, description="Publishable Razorpay key ID for client checkout.")
    created_at: datetime


class PaymentVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    razorpay_order_id: str = Field(..., min_length=5, max_length=128)
    razorpay_payment_id: str = Field(..., min_length=5, max_length=128)
    razorpay_signature: str = Field(..., min_length=10, max_length=256)


class PaymentVerifyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    success: bool
    payment_id: str
    order_id: str
    subscription_id: UUID
    subscription_status: str
    payment_status: str
    settled_at: datetime


class PaymentWebhookResponse(BaseModel):
    status: str
    event_id: str
    event_type: str

