from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RelationshipType(StrEnum):
    REPORTS_TO = "REPORTS_TO"      # Operational reporting line (strict tree)
    REFERRED_BY = "REFERRED_BY"    # Referral attribution / commission lineage
    MANAGES = "MANAGES"            # Team / territory managerial oversight


class RelationshipStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class MemberRelationship(Base):
    """Authoritative community member relationship linking tenant memberships."""

    __tablename__ = "member_relationships"
    __table_args__ = (
        CheckConstraint(
            "parent_member_id != child_member_id",
            name="ck_member_relationships_no_self_link",
        ),
        CheckConstraint(
            "relationship_type IN ('REPORTS_TO', 'REFERRED_BY', 'MANAGES')",
            name="ck_member_relationships_type",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_member_relationships_status",
        ),
        Index("ix_member_rel_parent_active", "tenant_id", "parent_member_id", "relationship_type", "status"),
        Index("ix_member_rel_child_active", "tenant_id", "child_member_id", "relationship_type", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    parent_member_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=False
    )
    child_member_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RelationshipStatus.ACTIVE.value, server_default="ACTIVE"
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    end_reason: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

