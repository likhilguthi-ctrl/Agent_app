from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class PayoutCreateRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, description="Requested payout amount in community currency.")
    idempotency_key: str = Field(..., min_length=1, max_length=128, description="Unique client idempotency key.")
    request_note: str | None = Field(default=None, max_length=255, description="Optional note from requester.")
    bank_account_details: dict[str, Any] | None = Field(
        default=None, description="Snapshot of target bank or payout details (account number, IFSC, UPI, etc.)."
    )


class PayoutReviewRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"] = Field(..., description="Community Admin review decision.")
    reason: str = Field(..., min_length=3, max_length=255, description="Mandatory decision justification.")
    notes: str | None = Field(default=None, max_length=255, description="Optional review notes.")


class PayoutDisburseRequest(BaseModel):
    disbursement_reference: str = Field(
        ..., min_length=3, max_length=128, description="Verifiable banking reference, UTR, or gateway transfer ID."
    )
    disbursement_method: str = Field(
        default="MANUAL_BANK_TRANSFER", max_length=32, description="Settlement channel (e.g. NEFT, RTGS, IMPS, UPI)."
    )
    notes: str | None = Field(default=None, max_length=255, description="Optional disbursement notes.")


class PayoutFailRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255, description="Mandatory failure reason.")


class PayoutCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=255, description="Optional cancellation note.")


class PayoutSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    agent_membership_id: UUID
    agent_profile_id: UUID
    account_id: UUID
    amount: Decimal
    currency: str
    status: str
    idempotency_key: str
    request_note: str | None = None
    rejection_reason: str | None = None
    failure_reason: str | None = None
    reviewed_by_membership_id: UUID | None = None
    reviewed_at: datetime | None = None
    review_notes: str | None = None
    disbursed_by_user_id: UUID | None = None
    disbursed_at: datetime | None = None
    disbursement_reference: str | None = None
    disbursement_method: str | None = None
    bank_account_snapshot: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class PayoutListResponse(BaseModel):
    items: list[PayoutSummary]
    total: int
    limit: int
    offset: int


class AgentEligibleBalanceResponse(BaseModel):
    agent_membership_id: UUID
    currency: str
    current_balance: Decimal
    pending_payout_amount: Decimal
    available_to_withdraw: Decimal
    minimum_withdrawal_amount: Decimal

