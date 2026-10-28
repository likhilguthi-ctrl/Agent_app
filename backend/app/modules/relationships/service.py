from __future__ import annotations

from datetime import UTC, datetime
import re
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import and_, cast, func, literal, not_, select, String
from sqlalchemy.orm import Session, aliased

from app.modules.agents.models import AgentProfile, SubscriberProfile
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.identity.models import TenantMembership, User
from app.modules.relationships.models import (
    MemberRelationship,
    RelationshipStatus,
    RelationshipType,
)
from app.modules.relationships.schemas import (
    DescendantMemberSummary,
    DescendantRosterResponse,
    DescendantStatsResponse,
    RelationshipSummary,
)
from app.modules.tenants.models import UserRegionAssignment


def _mask_mobile(mobile: str | None) -> str | None:
    if not mobile:
        return None
    # Keep country prefix and last 4 digits: e.g. +919876543210 -> +91******3210
    if len(mobile) >= 8:
        return mobile[:3] + "*" * (len(mobile) - 7) + mobile[-4:]
    return "****" + mobile[-2:] if len(mobile) >= 2 else "****"


class RelationshipService:
    def __init__(self, audit_service: AuditService | None = None) -> None:
        self._audit = audit_service or AuditService()

    def check_cycle(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        parent_member_id: UUID,
        child_member_id: UUID,
        relationship_type: str,
        max_depth: int = 15,
    ) -> bool:
        """Return True if adding parent_member_id -> child_member_id would create a cycle.
        
        A cycle would occur if parent_member_id is already an active descendant of child_member_id.
        """
        # Search starting from child_member_id downwards. If parent_member_id is encountered, it's a cycle.
        anchor = (
            select(
                MemberRelationship.child_member_id.label("member_id"),
                literal(1).label("depth"),
                cast(MemberRelationship.parent_member_id, String).label("path"),
            )
            .where(
                MemberRelationship.tenant_id == tenant_id,
                MemberRelationship.parent_member_id == child_member_id,
                MemberRelationship.relationship_type == relationship_type,
                MemberRelationship.status == RelationshipStatus.ACTIVE.value,
            )
            .cte(name="cycle_check_tree", recursive=True)
        )

        child_mr = aliased(MemberRelationship)
        recursive = (
            select(
                child_mr.child_member_id.label("member_id"),
                (anchor.c.depth + 1).label("depth"),
                (anchor.c.path + "," + cast(child_mr.parent_member_id, String)).label("path"),
            )
            .where(
                child_mr.tenant_id == tenant_id,
                child_mr.parent_member_id == anchor.c.member_id,
                child_mr.relationship_type == relationship_type,
                child_mr.status == RelationshipStatus.ACTIVE.value,
                anchor.c.depth < max_depth,
                not_(anchor.c.path.contains(cast(child_mr.child_member_id, String))),
            )
        )

        tree = anchor.union_all(recursive)
        found = session.execute(
            select(tree.c.member_id).where(tree.c.member_id == parent_member_id).limit(1)
        ).scalar_one_or_none()
        return found is not None

    def create_relationship(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        parent_member_id: UUID,
        child_member_id: UUID,
        relationship_type: str,
        creator_user_id: UUID,
    ) -> RelationshipSummary:
        # 1. No self-link
        if parent_member_id == child_member_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "SELF_LINK_FORBIDDEN", "message": "A member cannot have a relationship with themselves."},
            )

        # 2. Validate parent membership exists in same tenant and is active
        parent = session.execute(
            select(TenantMembership).where(
                TenantMembership.id == parent_member_id,
                TenantMembership.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if parent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PARENT_MEMBER_NOT_FOUND", "message": "Parent member not found in this community."},
            )
        if parent.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "PARENT_MEMBER_INACTIVE", "message": "Parent member is not active."},
            )

        # 3. Validate child membership exists in same tenant and is active
        child = session.execute(
            select(TenantMembership).where(
                TenantMembership.id == child_member_id,
                TenantMembership.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if child is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CHILD_MEMBER_NOT_FOUND", "message": "Child member not found in this community."},
            )
        if child.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "CHILD_MEMBER_INACTIVE", "message": "Child member is not active."},
            )

        # 4. Check duplicate active relationship between exact same parent and child
        exact_existing = session.execute(
            select(MemberRelationship).where(
                MemberRelationship.tenant_id == tenant_id,
                MemberRelationship.parent_member_id == parent_member_id,
                MemberRelationship.child_member_id == child_member_id,
                MemberRelationship.relationship_type == relationship_type,
                MemberRelationship.status == RelationshipStatus.ACTIVE.value,
            )
        ).scalar_one_or_none()
        if exact_existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "RELATIONSHIP_ALREADY_EXISTS", "message": "Active relationship between these members already exists."},
            )

        # 5. Single-parent policy for REPORTS_TO and REFERRED_BY
        if relationship_type in (RelationshipType.REPORTS_TO.value, RelationshipType.REFERRED_BY.value):
            existing_parent = session.execute(
                select(MemberRelationship).where(
                    MemberRelationship.tenant_id == tenant_id,
                    MemberRelationship.child_member_id == child_member_id,
                    MemberRelationship.relationship_type == relationship_type,
                    MemberRelationship.status == RelationshipStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()
            if existing_parent is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "SINGLE_PARENT_CONSTRAINT_VIOLATION",
                        "message": f"Member already has an active parent for relationship type '{relationship_type}'.",
                    },
                )

        # 6. Cycle check for hierarchy relationships
        if relationship_type in (RelationshipType.REPORTS_TO.value, RelationshipType.MANAGES.value):
            if self.check_cycle(
                session,
                tenant_id=tenant_id,
                parent_member_id=parent_member_id,
                child_member_id=child_member_id,
                relationship_type=relationship_type,
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "HIERARCHY_CYCLE_DETECTED", "message": "Adding this relationship would create a cycle."},
                )

        now = datetime.now(UTC)
        rel_id = uuid4()
        rel = MemberRelationship(
            id=rel_id,
            tenant_id=tenant_id,
            parent_member_id=parent_member_id,
            child_member_id=child_member_id,
            relationship_type=relationship_type,
            status=RelationshipStatus.ACTIVE.value,
            created_by_user_id=creator_user_id,
            created_at=now,
        )
        session.add(rel)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="member.relationship.created",
                actor_category="tenant_member",
                actor_ref=str(creator_user_id),
                action="relationship.create",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="member_relationship",
                target_entity_ref=str(rel_id),
            ),
        )

        return RelationshipSummary.model_validate(rel)

    def revoke_relationship(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        relationship_id: UUID,
        revoker_user_id: UUID,
        end_reason: str,
    ) -> RelationshipSummary:
        rel = session.execute(
            select(MemberRelationship)
            .where(
                MemberRelationship.id == relationship_id,
                MemberRelationship.tenant_id == tenant_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if rel is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "RELATIONSHIP_NOT_FOUND", "message": "Member relationship not found."},
            )

        if rel.status == RelationshipStatus.REVOKED.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "ALREADY_REVOKED", "message": "This relationship is already revoked."},
            )

        now = datetime.now(UTC)
        rel.status = RelationshipStatus.REVOKED.value
        rel.ended_at = now
        rel.ended_by_user_id = revoker_user_id
        rel.end_reason = end_reason
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="member.relationship.revoked",
                actor_category="tenant_member",
                actor_ref=str(revoker_user_id),
                action="relationship.revoke",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="member_relationship",
                target_entity_ref=str(relationship_id),
            ),
        )

        return RelationshipSummary.model_validate(rel)

    def list_relationships_for_member(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        member_id: UUID,
        include_revoked: bool = False,
    ) -> list[RelationshipSummary]:
        stmt = select(MemberRelationship).where(
            MemberRelationship.tenant_id == tenant_id,
            (MemberRelationship.parent_member_id == member_id)
            | (MemberRelationship.child_member_id == member_id),
        )
        if not include_revoked:
            stmt = stmt.where(MemberRelationship.status == RelationshipStatus.ACTIVE.value)

        stmt = stmt.order_by(MemberRelationship.created_at.desc())
        results = session.execute(stmt).scalars().all()
        return [RelationshipSummary.model_validate(r) for r in results]

    def get_descendant_roster(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        root_member_id: UUID,
        relationship_type: str = "REPORTS_TO",
        max_depth: int = 3,
        limit: int = 50,
        offset: int = 0,
        region_id: UUID | None = None,
    ) -> DescendantRosterResponse:
        # Bounded depth clamp
        clamped_depth = max(1, min(max_depth, 10))

        # Anchor query: direct children
        anchor = (
            select(
                MemberRelationship.child_member_id.label("member_id"),
                MemberRelationship.parent_member_id.label("parent_member_id"),
                MemberRelationship.relationship_type.label("relationship_type"),
                literal(1).label("depth"),
                cast(MemberRelationship.parent_member_id, String).label("path"),
            )
            .join(
                TenantMembership,
                and_(
                    TenantMembership.id == MemberRelationship.child_member_id,
                    TenantMembership.tenant_id == MemberRelationship.tenant_id,
                ),
            )
            .where(
                MemberRelationship.tenant_id == tenant_id,
                MemberRelationship.parent_member_id == root_member_id,
                MemberRelationship.relationship_type == relationship_type,
                MemberRelationship.status == RelationshipStatus.ACTIVE.value,
                TenantMembership.status == "active",  # Stop at inactive nodes
            )
            .cte(name="descendant_tree", recursive=True)
        )

        # Recursive query: next level children
        child_mr = aliased(MemberRelationship)
        child_tm = aliased(TenantMembership)
        recursive = (
            select(
                child_mr.child_member_id.label("member_id"),
                child_mr.parent_member_id.label("parent_member_id"),
                child_mr.relationship_type.label("relationship_type"),
                (anchor.c.depth + 1).label("depth"),
                (anchor.c.path + "," + cast(child_mr.parent_member_id, String)).label("path"),
            )
            .join(
                child_tm,
                and_(
                    child_tm.id == child_mr.child_member_id,
                    child_tm.tenant_id == child_mr.tenant_id,
                ),
            )
            .where(
                child_mr.tenant_id == tenant_id,
                child_mr.parent_member_id == anchor.c.member_id,
                child_mr.relationship_type == relationship_type,
                child_mr.status == RelationshipStatus.ACTIVE.value,
                child_tm.status == "active",  # Traversal must not pass through inactive node
                anchor.c.depth < clamped_depth,
                not_(anchor.c.path.contains(cast(child_mr.child_member_id, String))),  # Cycle guard
            )
        )

        tree = anchor.union_all(recursive)

        # Query total count
        count_stmt = select(func.count()).select_from(tree)
        total = session.execute(count_stmt).scalar() or 0

        # Query paginated rows with user and profile info
        query = (
            select(
                tree.c.member_id,
                tree.c.parent_member_id,
                tree.c.depth,
                tree.c.relationship_type,
                TenantMembership.user_id,
                TenantMembership.status.label("membership_status"),
                TenantMembership.created_at,
                TenantMembership.mobile_e164,
                AgentProfile.first_name.label("agent_first"),
                AgentProfile.last_name.label("agent_last"),
                SubscriberProfile.full_name.label("sub_full_name"),
            )
            .join(TenantMembership, TenantMembership.id == tree.c.member_id)
            .outerjoin(AgentProfile, AgentProfile.membership_id == tree.c.member_id)
            .outerjoin(SubscriberProfile, SubscriberProfile.membership_id == tree.c.member_id)
        )

        # Optional region filter
        if region_id is not None:
            query = query.join(
                UserRegionAssignment,
                (UserRegionAssignment.user_id == TenantMembership.user_id)
                & (UserRegionAssignment.tenant_id == tenant_id)
                & (UserRegionAssignment.status == "active"),
            ).where(UserRegionAssignment.region_id == region_id)

        query = query.order_by(tree.c.depth.asc(), TenantMembership.created_at.desc()).limit(limit).offset(offset)
        rows = session.execute(query).all()

        max_depth_seen = 0
        descendants: list[DescendantMemberSummary] = []
        for r in rows:
            if r.depth > max_depth_seen:
                max_depth_seen = r.depth

            # Determine display name & profile type
            profile_type = "member"
            full_name = None
            if r.agent_first:
                profile_type = "agent"
                full_name = f"{r.agent_first} {r.agent_last}".strip()
            elif r.sub_full_name:
                profile_type = "subscriber"
                full_name = r.sub_full_name

            descendants.append(
                DescendantMemberSummary(
                    member_id=r.member_id,
                    user_id=r.user_id,
                    depth=r.depth,
                    parent_member_id=r.parent_member_id,
                    relationship_type=r.relationship_type,
                    status=r.membership_status,
                    created_at=r.created_at,
                    mobile_masked=_mask_mobile(r.mobile_e164),
                    full_name=full_name,
                    profile_type=profile_type,
                )
            )

        return DescendantRosterResponse(
            descendants=descendants,
            total=total,
            max_depth_reached=max_depth_seen,
            limit=limit,
            offset=offset,
        )

    def get_descendant_stats(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        root_member_id: UUID,
        relationship_type: str = "REPORTS_TO",
        max_depth: int = 3,
    ) -> DescendantStatsResponse:
        clamped_depth = max(1, min(max_depth, 10))

        anchor = (
            select(
                MemberRelationship.child_member_id.label("member_id"),
                literal(1).label("depth"),
                cast(MemberRelationship.parent_member_id, String).label("path"),
            )
            .join(
                TenantMembership,
                and_(
                    TenantMembership.id == MemberRelationship.child_member_id,
                    TenantMembership.tenant_id == MemberRelationship.tenant_id,
                ),
            )
            .where(
                MemberRelationship.tenant_id == tenant_id,
                MemberRelationship.parent_member_id == root_member_id,
                MemberRelationship.relationship_type == relationship_type,
                MemberRelationship.status == RelationshipStatus.ACTIVE.value,
                TenantMembership.status == "active",
            )
            .cte(name="stats_tree", recursive=True)
        )

        child_mr = aliased(MemberRelationship)
        child_tm = aliased(TenantMembership)
        recursive = (
            select(
                child_mr.child_member_id.label("member_id"),
                (anchor.c.depth + 1).label("depth"),
                (anchor.c.path + "," + cast(child_mr.parent_member_id, String)).label("path"),
            )
            .join(
                child_tm,
                and_(
                    child_tm.id == child_mr.child_member_id,
                    child_tm.tenant_id == child_mr.tenant_id,
                ),
            )
            .where(
                child_mr.tenant_id == tenant_id,
                child_mr.parent_member_id == anchor.c.member_id,
                child_mr.relationship_type == relationship_type,
                child_mr.status == RelationshipStatus.ACTIVE.value,
                child_tm.status == "active",
                anchor.c.depth < clamped_depth,
                not_(anchor.c.path.contains(cast(child_mr.child_member_id, String))),
            )
        )

        tree = anchor.union_all(recursive)

        # Aggregate query
        query = (
            select(
                tree.c.depth,
                TenantMembership.status,
                AgentProfile.id.label("agent_id"),
                SubscriberProfile.id.label("subscriber_id"),
            )
            .join(TenantMembership, TenantMembership.id == tree.c.member_id)
            .outerjoin(AgentProfile, AgentProfile.membership_id == tree.c.member_id)
            .outerjoin(SubscriberProfile, SubscriberProfile.membership_id == tree.c.member_id)
        )
        rows = session.execute(query).all()

        total_descendants = len(rows)
        direct_children = 0
        counts_by_depth: dict[int, int] = {}
        counts_by_status: dict[str, int] = {}
        counts_by_type: dict[str, int] = {"agent": 0, "subscriber": 0, "member": 0}

        for r in rows:
            if r.depth == 1:
                direct_children += 1
            counts_by_depth[r.depth] = counts_by_depth.get(r.depth, 0) + 1
            counts_by_status[r.status] = counts_by_status.get(r.status, 0) + 1
            if r.agent_id:
                counts_by_type["agent"] += 1
            elif r.subscriber_id:
                counts_by_type["subscriber"] += 1
            else:
                counts_by_type["member"] += 1

        return DescendantStatsResponse(
            total_descendants=total_descendants,
            direct_children_count=direct_children,
            counts_by_depth=counts_by_depth,
            counts_by_status=counts_by_status,
            counts_by_type=counts_by_type,
        )


_relationship_service: RelationshipService | None = None


def get_relationship_service() -> RelationshipService:
    global _relationship_service
    if _relationship_service is None:
        _relationship_service = RelationshipService()
    return _relationship_service
