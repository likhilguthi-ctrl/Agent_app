from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class SupportTicketCreate(BaseModel):
    message: str = Field(min_length=1, max_length=5000)


class SupportTicketSummary(BaseModel):
    id: str
    employeeRefId: str
    employeeName: str
    message: str
    status: str
    createdAt: str
    resolvedAt: str | None = None


class SupportTicketListResponse(BaseModel):
    tickets: list[SupportTicketSummary]


class SupportTicketSingleResponse(BaseModel):
    ticket: SupportTicketSummary

