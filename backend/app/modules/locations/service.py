from __future__ import annotations

import logging
from uuid import UUID, uuid4
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.locations.models import (
    District,
    Mandal,
    ManagerLocationAssignment,
    State,
)
from app.modules.locations.schemas import (
    DistrictSummary,
    MandalSummary,
    ManagerLocationScopeResponse,
    StateScopeConfig,
    StateSummary,
)

logger = logging.getLogger(__name__)


class LocationService:
    """Service for managing location master data, hierarchical validation, and Manager geographic assignments."""

    @staticmethod
    def validate_location_hierarchy(
        session: Session,
        state_id: UUID,
        district_id: UUID,
        mandal_id: UUID,
    ) -> tuple[State, District, Mandal]:
        state = session.execute(select(State).where(State.id == state_id, State.is_active == True)).scalar_one_or_none()
        if not state:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_STATE", "message": f"State '{state_id}' is invalid or inactive."},
            )

        district = session.execute(
            select(District).where(
                District.id == district_id,
                District.state_id == state_id,
                District.is_active == True,
            )
        ).scalar_one_or_none()
        if not district:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "INVALID_DISTRICT",
                    "message": f"District '{district_id}' is invalid, inactive, or does not belong to state '{state.name}'.",
                },
            )

        mandal = session.execute(
            select(Mandal).where(
                Mandal.id == mandal_id,
                Mandal.district_id == district_id,
                Mandal.is_active == True,
            )
        ).scalar_one_or_none()
        if not mandal:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "INVALID_MANDAL",
                    "message": f"Mandal '{mandal_id}' is invalid, inactive, or does not belong to district '{district.name}'.",
                },
            )

        return state, district, mandal

    @staticmethod
    def save_manager_location_assignments(
        session: Session,
        tenant_id: UUID,
        user_id: UUID,
        configs: list[StateScopeConfig],
        assigned_by_user_id: UUID | None = None,
    ) -> None:
        if not configs:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "STATE_REQUIRED", "message": "At least one state must be assigned to the Manager."},
            )

        # Deactivate existing assignments for this manager in this tenant
        existing = session.execute(
            select(ManagerLocationAssignment).where(
                ManagerLocationAssignment.tenant_id == tenant_id,
                ManagerLocationAssignment.user_id == user_id,
                ManagerLocationAssignment.status == "active",
            )
        ).scalars().all()
        for e in existing:
            e.status = "inactive"
        session.flush()

        seen_states: set[UUID] = set()
        for st_cfg in configs:
            if st_cfg.state_id in seen_states:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "DUPLICATE_STATE", "message": "State cannot be assigned multiple times."},
                )
            seen_states.add(st_cfg.state_id)

            state = session.execute(
                select(State).where(State.id == st_cfg.state_id, State.is_active == True)
            ).scalar_one_or_none()
            if not state:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "INVALID_STATE", "message": f"State '{st_cfg.state_id}' is invalid or inactive."},
                )

            if st_cfg.all_districts:
                # Grant access to all districts in this state
                session.add(
                    ManagerLocationAssignment(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        user_id=user_id,
                        state_id=state.id,
                        all_districts=True,
                        district_id=None,
                        all_mandals=True,
                        mandal_id=None,
                        status="active",
                        assigned_by_user_id=assigned_by_user_id,
                    )
                )
                continue

            if not st_cfg.district_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "DISTRICTS_REQUIRED",
                        "message": f"At least one district must be selected for state '{state.name}' when 'all_districts' is False.",
                    },
                )

            mandal_map = {m.district_id: m for m in st_cfg.mandal_configs}
            for dist_id in st_cfg.district_ids:
                dist = session.execute(
                    select(District).where(
                        District.id == dist_id,
                        District.state_id == state.id,
                        District.is_active == True,
                    )
                ).scalar_one_or_none()
                if not dist:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "error": "INVALID_DISTRICT",
                            "message": f"District '{dist_id}' does not belong to state '{state.name}'.",
                        },
                    )

                m_cfg = mandal_map.get(dist_id)
                if not m_cfg or m_cfg.all_mandals or not m_cfg.mandal_ids:
                    # Grant access to all mandals in this district
                    session.add(
                        ManagerLocationAssignment(
                            id=uuid4(),
                            tenant_id=tenant_id,
                            user_id=user_id,
                            state_id=state.id,
                            all_districts=False,
                            district_id=dist.id,
                            all_mandals=True,
                            mandal_id=None,
                            status="active",
                            assigned_by_user_id=assigned_by_user_id,
                        )
                    )
                else:
                    # Specific mandal restrictions configured
                    for m_id in m_cfg.mandal_ids:
                        mandal = session.execute(
                            select(Mandal).where(
                                Mandal.id == m_id,
                                Mandal.district_id == dist.id,
                                Mandal.is_active == True,
                            )
                        ).scalar_one_or_none()
                        if not mandal:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail={
                                    "error": "INVALID_MANDAL",
                                    "message": f"Mandal '{m_id}' does not belong to district '{dist.name}'.",
                                },
                            )
                        session.add(
                            ManagerLocationAssignment(
                                id=uuid4(),
                                tenant_id=tenant_id,
                                user_id=user_id,
                                state_id=state.id,
                                all_districts=False,
                                district_id=dist.id,
                                all_mandals=False,
                                mandal_id=mandal.id,
                                status="active",
                                assigned_by_user_id=assigned_by_user_id,
                            )
                        )
        session.flush()

    @staticmethod
    def is_manager_authorized_for_location(
        session: Session,
        tenant_id: UUID,
        manager_user_id: UUID,
        state_id: UUID,
        district_id: UUID,
        mandal_id: UUID | None = None,
    ) -> bool:
        assignments = session.execute(
            select(ManagerLocationAssignment).where(
                ManagerLocationAssignment.tenant_id == tenant_id,
                ManagerLocationAssignment.user_id == manager_user_id,
                ManagerLocationAssignment.status == "active",
            )
        ).scalars().all()

        for a in assignments:
            if a.state_id != state_id:
                continue
            if a.all_districts:
                return True
            if a.district_id == district_id:
                if a.all_mandals or a.mandal_id is None:
                    return True
                if mandal_id is not None and a.mandal_id == mandal_id:
                    return True
        return False

    @staticmethod
    def find_eligible_managers_for_location(
        session: Session,
        tenant_id: UUID,
        state_id: UUID,
        district_id: UUID,
        mandal_id: UUID | None = None,
    ) -> list[tuple[User, TenantMembership, str]]:
        """Finds all Managers in the community whose assigned location scope covers the specified coordinates.

        Returns list of tuples: (User, TenantMembership, ManagerFullName)
        """
        assignments = session.execute(
            select(ManagerLocationAssignment).where(
                ManagerLocationAssignment.tenant_id == tenant_id,
                ManagerLocationAssignment.state_id == state_id,
                ManagerLocationAssignment.status == "active",
            )
        ).scalars().all()

        eligible_user_ids: set[UUID] = set()
        for a in assignments:
            if a.all_districts:
                eligible_user_ids.add(a.user_id)
            elif a.district_id == district_id:
                if a.all_mandals or a.mandal_id is None:
                    eligible_user_ids.add(a.user_id)
                elif mandal_id is not None and a.mandal_id == mandal_id:
                    eligible_user_ids.add(a.user_id)

        if not eligible_user_ids:
            return []

        # Filter only active community members with Admin / Manager role
        from app.modules.tenants.models import AdminProfile
        results = []
        for u_id in eligible_user_ids:
            membership = session.execute(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == tenant_id,
                    TenantMembership.user_id == u_id,
                    TenantMembership.status == "active",
                )
            ).scalar_one_or_none()
            if not membership:
                continue

            user = session.execute(select(User).where(User.id == u_id)).scalar_one_or_none()
            admin_prof = session.execute(
                select(AdminProfile).where(
                    AdminProfile.tenant_id == tenant_id,
                    AdminProfile.user_id == u_id,
                )
            ).scalar_one_or_none()

            name = "Manager"
            if admin_prof:
                fn = (admin_prof.first_name or "").strip()
                ln = (admin_prof.last_name or "").strip()
                name = f"{fn} {ln}".strip() or "Manager"

            results.append((user, membership, name))
        return results

    @staticmethod
    def get_manager_scope(
        session: Session,
        tenant_id: UUID,
        manager_user_id: UUID,
    ) -> ManagerLocationScopeResponse:
        assignments = session.execute(
            select(ManagerLocationAssignment).where(
                ManagerLocationAssignment.tenant_id == tenant_id,
                ManagerLocationAssignment.user_id == manager_user_id,
                ManagerLocationAssignment.status == "active",
            )
        ).scalars().all()

        state_ids = {a.state_id for a in assignments}
        states = session.execute(select(State).where(State.id.in_(state_ids))).scalars().all() if state_ids else []

        all_districts_by_state: dict[str, bool] = {}
        for s in states:
            all_districts_by_state[str(s.id)] = any(a.state_id == s.id and a.all_districts for a in assignments)

        # Collect districts
        districts_dict: dict[UUID, District] = {}
        all_mandals_by_district: dict[str, bool] = {}
        specific_mandal_ids_by_dist: dict[UUID, set[UUID]] = {}

        for a in assignments:
            if a.all_districts:
                # All districts in state
                dists_in_state = session.execute(
                    select(District).where(District.state_id == a.state_id, District.is_active == True)
                ).scalars().all()
                for d in dists_in_state:
                    districts_dict[d.id] = d
                    all_mandals_by_district[str(d.id)] = True
            elif a.district_id:
                dist = session.execute(
                    select(District).where(District.id == a.district_id, District.is_active == True)
                ).scalar_one_or_none()
                if dist:
                    districts_dict[dist.id] = dist
                    if a.all_mandals or a.mandal_id is None:
                        all_mandals_by_district[str(dist.id)] = True
                    else:
                        all_mandals_by_district.setdefault(str(dist.id), False)
                        specific_mandal_ids_by_dist.setdefault(dist.id, set()).add(a.mandal_id)

        # Build mandals_by_district
        mandals_by_district: dict[str, list[MandalSummary]] = {}
        for d_id, d in districts_dict.items():
            if all_mandals_by_district.get(str(d_id), False):
                mandals = session.execute(
                    select(Mandal).where(Mandal.district_id == d_id, Mandal.is_active == True)
                ).scalars().all()
                mandals_by_district[str(d_id)] = [MandalSummary.model_validate(m) for m in mandals]
            else:
                m_ids = specific_mandal_ids_by_dist.get(d_id, set())
                mandals = (
                    session.execute(select(Mandal).where(Mandal.id.in_(m_ids), Mandal.is_active == True)).scalars().all()
                    if m_ids
                    else []
                )
                mandals_by_district[str(d_id)] = [MandalSummary.model_validate(m) for m in mandals]

        return ManagerLocationScopeResponse(
            user_id=manager_user_id,
            tenant_id=tenant_id,
            states=[StateSummary.model_validate(s) for s in states],
            districts=[DistrictSummary.model_validate(d) for d in districts_dict.values()],
            all_districts_by_state=all_districts_by_state,
            all_mandals_by_district=all_mandals_by_district,
            mandals_by_district=mandals_by_district,
        )
