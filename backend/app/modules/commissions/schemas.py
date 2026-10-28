from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class CommissionAccountSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    agent_membership_id: UUID | None = None
    agent_profile_id: UUID | None = None
    account_type: str
    account_number: str
    currency: str
    current_balance: Decimal
    status: str
    allow_negative: bool
    created_at: datetime
    updated_at: datetime


class JournalEntryLineSpec(BaseModel):
    account_id: UUID
    entry_type: str = Field(..., pattern="^(DEBIT|CREDIT)$")
    amount: Decimal = Field(..., gt=0)
    description: str | None = Field(default=None, max_length=255)


class JournalCreateRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1, max_length=255)
    reference_type: str = Field(..., min_length=1, max_length=50)
    reference_id: str | None = Field(default=None, max_length=100)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    entries: list[JournalEntryLineSpec] = Field(..., min_length=2)


class LedgerEntrySummary(BaseModel):
    id: UUID
    tenant_id: UUID
    journal_id: UUID
    account_id: UUID
    entry_type: str
    amount: Decimal
    currency: str
    balance_after: Decimal
    description: str | None = None
    created_at: datetime


class JournalSummary(BaseModel):
    id: UUID
    tenant_id: UUID
    journal_number: str
    description: str
    reference_type: str
    reference_id: str | None = None
    idempotency_key: str
    currency: str
    total_amount: Decimal
    status: str
    posted_at: datetime
    created_by_user_id: UUID | None = None
    created_at: datetime


class JournalDetailResponse(BaseModel):
    journal: JournalSummary
    entries: list[LedgerEntrySummary]


class JournalReversalRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=5, max_length=255)


class LedgerEntriesListResponse(BaseModel):
    items: list[LedgerEntrySummary]
    total: int
    limit: int
    offset: int

