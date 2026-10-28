from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.relationships.models import RelationshipStatus, RelationshipType


class RelationshipCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_member_id: UUID
    child_member_id: UUID
    relationship_type: RelationshipType

    @model_validator(mode="after")
    def validate_no_self_link(self) -> RelationshipCreateRequest:
        if self.parent_member_id == self.child_member_id:
            raise ValueError("parent_member_id and child_member_id cannot be the same membership (no self-links).")
        return self


class RelationshipRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    end_reason: str = Field(..., min_length=1, max_length=255, description="Reason for revoking this relationship.")


class RelationshipSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    parent_member_id: UUID
    child_member_id: UUID
    relationship_type: str
    status: str
    created_by_user_id: UUID
    created_at: datetime
    ended_at: datetime | None = None
    ended_by_user_id: UUID | None = None
    end_reason: str | None = None


class RelationshipListResponse(BaseModel):
    relationships: list[RelationshipSummary]
    total: int


class DescendantMemberSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    member_id: UUID
    user_id: UUID
    depth: int
    parent_member_id: UUID
    relationship_type: str
    status: str
    created_at: datetime
    mobile_masked: str | None = None
    full_name: str | None = None
    profile_type: str = "member"


class DescendantRosterResponse(BaseModel):
    descendants: list[DescendantMemberSummary]
    total: int
    max_depth_reached: int
    limit: int
    offset: int


class DescendantStatsResponse(BaseModel):
    total_descendants: int
    direct_children_count: int
    counts_by_depth: dict[int, int]
    counts_by_status: dict[str, int]
    counts_by_type: dict[str, int]

