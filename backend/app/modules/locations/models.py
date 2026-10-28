from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class State(Base):
    """Authoritative state master entity."""

    __tablename__ = "states"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    districts: Mapped[list[District]] = relationship("District", back_populates="state", cascade="all, delete-orphan")


class District(Base):
    """Authoritative district master entity bound to a state."""

    __tablename__ = "districts"
    __table_args__ = (
        UniqueConstraint("state_id", "name", name="uq_districts_state_name"),
        Index("ix_districts_state_active", "state_id", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    state_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("states.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    district_code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    state: Mapped[State] = relationship("State", back_populates="districts")
    mandals: Mapped[list[Mandal]] = relationship("Mandal", back_populates="district", cascade="all, delete-orphan")


class Mandal(Base):
    """Authoritative mandal (sub-district) entity bound to a district."""

    __tablename__ = "mandals"
    __table_args__ = (
        UniqueConstraint("district_id", "name", name="uq_mandals_district_name"),
        Index("ix_mandals_district_active", "district_id", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    district_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("districts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    mandal_code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    district: Mapped[District] = relationship("District", back_populates="mandals")


class ManagerLocationAssignment(Base):
    """Relational assignment specifying a Manager's geographic state, district, and mandal scope."""

    __tablename__ = "manager_location_assignments"
    __table_args__ = (
        Index("ix_manager_loc_tenant_user", "tenant_id", "user_id", "status"),
        Index("ix_manager_loc_lookup", "tenant_id", "state_id", "district_id", "mandal_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("states.id", ondelete="CASCADE"), nullable=False
    )
    all_districts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    district_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("districts.id", ondelete="CASCADE"), nullable=True
    )
    all_mandals: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    mandal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mandals.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    assigned_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentCreationRequest(Base):
    """Formal two-stage approval request workflow for an Agent seeking permission to create a child Agent."""

    __tablename__ = "agent_creation_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_MANAGER', 'PENDING_SUPER_ADMIN', 'APPROVED', 'REJECTED', 'FULFILLED')",
            name="ck_agent_creation_requests_status",
        ),
        Index("ix_agent_creation_req_tenant_status", "tenant_id", "status"),
        Index("ix_agent_creation_req_requesting_agent", "tenant_id", "requesting_agent_user_id"),
        Index("ix_agent_creation_req_manager", "tenant_id", "manager_user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requesting_agent_membership_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requesting_agent_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    manager_membership_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="SET NULL"), nullable=True
    )
    manager_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    state_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("states.id", ondelete="RESTRICT"), nullable=False
    )
    district_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("districts.id", ondelete="RESTRICT"), nullable=False
    )
    mandal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mandals.id", ondelete="RESTRICT"), nullable=False
    )
    state_name: Mapped[str] = mapped_column(String(100), nullable=False)
    district_name: Mapped[str] = mapped_column(String(100), nullable=False)
    mandal_name: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING_MANAGER", server_default="PENDING_MANAGER", index=True
    )

    manager_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manager_decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manager_decision_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    manager_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    superadmin_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    superadmin_decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superadmin_decision_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    superadmin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    fulfilled_agent_profile_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL"), nullable=True
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
