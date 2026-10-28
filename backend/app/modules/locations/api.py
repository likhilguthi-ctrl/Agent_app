from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.authentication.api import get_current_resolved_session
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability, require_super_admin
from app.modules.locations.models import District, Mandal, State
from app.modules.locations.schemas import (
    DistrictSummary,
    MandalSummary,
    ManagerLocationScopeRequest,
    ManagerLocationScopeResponse,
    StateSummary,
)
from app.modules.locations.service import LocationService

router = APIRouter(prefix="/locations", tags=["Location Master & Scoping"])


@router.get("/states", response_model=list[StateSummary], summary="List all active states")
def list_states(session: Annotated[Session, Depends(get_db)]) -> list[StateSummary]:
    states = session.execute(
        select(State).where(State.is_active == True).order_by(State.name)
    ).scalars().all()
    return [StateSummary.model_validate(s) for s in states]


@router.get("/districts", response_model=list[DistrictSummary], summary="List districts in a state")
def list_districts(
    state_id: Annotated[UUID, Query(description="UUID of the state")],
    session: Annotated[Session, Depends(get_db)],
) -> list[DistrictSummary]:
    districts = session.execute(
        select(District).where(
            District.state_id == state_id,
            District.is_active == True,
        ).order_by(District.name)
    ).scalars().all()
    return [DistrictSummary.model_validate(d) for d in districts]


@router.get("/mandals", response_model=list[MandalSummary], summary="List mandals in a district")
def list_mandals(
    district_id: Annotated[UUID, Query(description="UUID of the district")],
    session: Annotated[Session, Depends(get_db)],
) -> list[MandalSummary]:
    mandals = session.execute(
        select(Mandal).where(
            Mandal.district_id == district_id,
            Mandal.is_active == True,
        ).order_by(Mandal.name)
    ).scalars().all()
    return [MandalSummary.model_validate(m) for m in mandals]


@router.get(
    "/manager-scope",
    response_model=ManagerLocationScopeResponse,
    summary="Retrieve the authenticated Manager's permitted geographic scope",
)
def get_current_manager_scope(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> ManagerLocationScopeResponse:
    caller, _ = session_data
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Community context required."},
        )

    return LocationService.get_manager_scope(
        session=session,
        tenant_id=caller.tenant_id,
        manager_user_id=caller.user_id,
    )


@router.post(
    "/communities/{community_id}/managers/{manager_user_id}/scope",
    status_code=status.HTTP_200_OK,
    summary="Super Admin: Update a Manager's geographic state/district/mandal scope",
)
def update_manager_scope(
    community_id: UUID,
    manager_user_id: UUID,
    body: ManagerLocationScopeRequest,
    operator: Annotated[ResolvedSession, Depends(require_super_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    LocationService.save_manager_location_assignments(
        session=session,
        tenant_id=community_id,
        user_id=manager_user_id,
        configs=body.states,
        assigned_by_user_id=operator.user_id,
    )
    session.commit()
    return {"message": "Manager geographic scope updated successfully."}
