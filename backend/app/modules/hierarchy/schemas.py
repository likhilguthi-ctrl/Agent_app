from __future__ import annotations

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Super Admin Hierarchy Drill-Down Schemas
# ---------------------------------------------------------------------------
class HierarchyManagerItem(BaseModel):
    id: UUID
    user_id: UUID
    membership_id: UUID
    name: str
    username: str
    mobile_e164: str
    email: str | None = None
    assigned_states: list[str] = Field(default_factory=list)
    assigned_districts_summary: str = "All districts"
    agent_count: int = 0
    subscriber_count: int = 0
    status: str = "active"
    created_at: datetime


class HierarchyManagerListResponse(BaseModel):
    items: list[HierarchyManagerItem]
    total: int
    page: int
    page_size: int


class HierarchyAgentItem(BaseModel):
    id: UUID
    user_id: UUID
    membership_id: UUID
    name: str
    username: str
    mobile_e164: str
    email: str | None = None
    state: str | None = None
    district: str | None = None
    mandal: str | None = None
    manager_name: str | None = None
    subscriber_count: int = 0
    status: str = "active"
    created_at: datetime


class HierarchyAgentListResponse(BaseModel):
    items: list[HierarchyAgentItem]
    total: int
    page: int
    page_size: int


class HierarchySubscriberItem(BaseModel):
    id: UUID
    full_name: str
    mobile_e164: str
    email: str | None = None
    address: str | None = None
    package_plan: str | None = None
    payment_status: str | None = None
    subscription_status: str | None = None
    created_at: datetime


class HierarchySubscriberListResponse(BaseModel):
    items: list[HierarchySubscriberItem]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Agent Creation Request (Approval Workflow) Schemas
# ---------------------------------------------------------------------------
class AgentCreationRequestSubmit(BaseModel):
    state_id: UUID
    district_id: UUID
    mandal_id: UUID
    reason: str = Field(min_length=5, max_length=1000)


class AgentCreationRequestReview(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")
    comment: str | None = Field(default=None, max_length=500)


class AgentCreationRequestSummary(BaseModel):
    id: UUID
    request_number: str
    tenant_id: UUID
    requesting_agent_user_id: UUID
    requesting_agent_membership_id: UUID
    requesting_agent_name: str
    manager_user_id: UUID | None = None
    manager_name: str | None = None
    state_id: UUID
    district_id: UUID
    mandal_id: UUID
    state_name: str
    district_name: str
    mandal_name: str
    reason: str
    status: str
    manager_decision: str | None = None
    manager_decision_at: datetime | None = None
    manager_comment: str | None = None
    superadmin_decision: str | None = None
    superadmin_decision_at: datetime | None = None
    superadmin_comment: str | None = None
    fulfilled_agent_profile_id: UUID | None = None
    fulfilled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentCreationRequestListResponse(BaseModel):
    items: list[AgentCreationRequestSummary]
    total: int
