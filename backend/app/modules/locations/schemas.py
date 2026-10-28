from __future__ import annotations

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field


class StateSummary(BaseModel):
    id: UUID
    name: str
    code: str
    is_active: bool = True

    model_config = {"from_attributes": True}


class DistrictSummary(BaseModel):
    id: UUID
    state_id: UUID
    name: str
    district_code: str
    is_active: bool = True

    model_config = {"from_attributes": True}


class MandalSummary(BaseModel):
    id: UUID
    district_id: UUID
    name: str
    mandal_code: str
    is_active: bool = True

    model_config = {"from_attributes": True}


class MandalScopeConfig(BaseModel):
    district_id: UUID
    all_mandals: bool = True
    mandal_ids: list[UUID] = Field(default_factory=list)


class StateScopeConfig(BaseModel):
    state_id: UUID
    all_districts: bool = False
    district_ids: list[UUID] = Field(default_factory=list)
    mandal_configs: list[MandalScopeConfig] = Field(default_factory=list)


class ManagerLocationScopeRequest(BaseModel):
    states: list[StateScopeConfig] = Field(min_length=1)


class ManagerLocationScopeResponse(BaseModel):
    user_id: UUID
    tenant_id: UUID
    states: list[StateSummary]
    districts: list[DistrictSummary]
    # Maps state_id str -> bool (all_districts)
    all_districts_by_state: dict[str, bool]
    # Maps district_id str -> bool (all_mandals)
    all_mandals_by_district: dict[str, bool]
    # Maps district_id str -> list[UUID]
    mandals_by_district: dict[str, list[MandalSummary]]
