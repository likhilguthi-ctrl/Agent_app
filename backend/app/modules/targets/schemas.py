from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TargetTypeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    description: str
    unit: str
    calculation_type: str
    applicable_role: str
    is_active: bool


class CreateAdminTargetRequest(BaseModel):
    admin_user_id: UUID
    target_type_code: str
    target_quantity: int = Field(gt=0, description="Target quota must be greater than zero.")
    period: Literal["WEEKLY", "MONTHLY", "QUARTERLY", "ANNUAL", "CUSTOM"] = "MONTHLY"
    start_date: date
    deadline: date
    notes: str | None = None


class AllocateAgentTargetRequest(BaseModel):
    parent_target_id: UUID
    agent_user_id: UUID
    target_quantity: int = Field(gt=0, description="Allocated quantity must be greater than zero.")
    start_date: date | None = None
    deadline: date | None = None
    period: Literal["WEEKLY", "MONTHLY", "QUARTERLY", "ANNUAL", "CUSTOM"] | None = None
    notes: str | None = None


class TargetAdjustmentRequest(BaseModel):
    adjustment_quantity: int = Field(description="Adjustment delta (positive or negative).")
    reason: str = Field(min_length=3, description="Auditable justification for the manual adjustment.")


class TargetAdjustmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_id: UUID
    adjusted_by_user_id: UUID
    adjustment_quantity: int
    reason: str
    created_at: datetime


class TargetProgress(BaseModel):
    target_quantity: int
    achieved_quantity: int
    remaining_quantity: int
    progress_percentage: float
    status: str
    is_overdue: bool
    is_completed: bool


class TargetResponse(BaseModel):
    id: UUID
    tenant_id: UUID | None
    parent_target_id: UUID | None
    target_type_id: UUID
    target_type_code: str
    target_type_name: str
    target_type_unit: str
    calculation_type: str
    assigned_to_user_id: UUID
    assigned_to_name: str
    assigned_to_role: str
    assigned_by_user_id: UUID
    target_quantity: int
    achieved_quantity: int
    remaining_quantity: int
    progress_percentage: float
    start_date: date
    deadline: date
    period: str
    status: str
    notes: str | None
    created_at: datetime
    updated_at: datetime
    allocated_to_children: int = 0
    remaining_to_allocate: int = 0
    adjustments_count: int = 0


class TargetSummaryResponse(BaseModel):
    total_targets: int
    active_targets: int
    completed_targets: int
    overdue_targets: int
    total_target_quantity: int
    total_achieved_quantity: int
    overall_progress_percentage: float

