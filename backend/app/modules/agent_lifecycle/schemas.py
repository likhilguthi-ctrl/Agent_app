from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.agent_lifecycle.models import LifecycleAction


class AgentLifecycleSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: LifecycleAction = Field(..., description="Target lifecycle action: SUSPEND, DEACTIVATE, REACTIVATE, REVOKE")
    reason: str = Field(..., min_length=5, max_length=1000, description="Mandatory non-empty reason or justification for the requested transition.")


class AgentLifecycleDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(..., pattern="^(APPROVED|REJECTED)$", description="Review decision: APPROVED or REJECTED")
    decision_reason: str | None = Field(None, max_length=1000, description="Optional decision reason or note.")


class AgentLifecycleSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    agent_profile_id: UUID
    membership_id: UUID
    user_id: UUID
    requested_by_user_id: UUID
    action: str
    status: str
    reason: str
    created_at: datetime
    updated_at: datetime
    reviewed_by_user_id: UUID | None = None
    decision_reason: str | None = None
    decided_at: datetime | None = None
    agent_ref_id: str | None = None
    agent_name: str | None = None


class AgentLifecycleListResponse(BaseModel):
    requests: list[AgentLifecycleSummary]
    total: int

