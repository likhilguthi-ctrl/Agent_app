from __future__ import annotations

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field


class InAppNotificationSummary(BaseModel):
    id: UUID
    recipient_operator_id: UUID
    tenant_id: UUID | None = None
    event_type: str
    title: str
    message: str
    appointment_id: UUID | None = None
    agent_profile_id: UUID | None = None
    is_read: bool
    read_at: datetime | None = None
    resolution_status: str
    resolved_at: datetime | None = None
    metadata_json: dict | None = None
    created_at: datetime
    updated_at: datetime


class InAppNotificationListResponse(BaseModel):
    items: list[InAppNotificationSummary]
    total: int
    unread_count: int


class UnreadCountResponse(BaseModel):
    unread_count: int


class MarkAllReadResponse(BaseModel):
    marked_read: int

