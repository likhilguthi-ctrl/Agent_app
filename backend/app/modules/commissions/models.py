from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AccountType(StrEnum):
    AGENT_COMMISSION = "AGENT_COMMISSION"
    COMMUNITY_EXPENSE = "COMMUNITY_EXPENSE"
    COMMUNITY_CLEARING = "COMMUNITY_CLEARING"
    PAYOUT_CLEARING = "PAYOUT_CLEARING"
    ADJUSTMENT_CLEARING = "ADJUSTMENT_CLEARING"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


class JournalStatus(StrEnum):
    POSTED = "POSTED"
    REVERSED = "REVERSED"


class JournalReferenceType(StrEnum):
    REFERRAL_COMMISSION = "REFERRAL_COMMISSION"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"
    PAYOUT_HOLD = "PAYOUT_HOLD"
    PAYOUT_DISBURSEMENT = "PAYOUT_DISBURSEMENT"
    REVERSAL = "REVERSAL"


class EntryType(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class CommissionAccount(Base):
    """Authoritative ledger account for agents and community financial buckets."""

    __tablename__ = "commission_accounts"
    __table_args__ = (
        CheckConstraint(
            "allow_negative = TRUE OR current_balance >= 0.00",
            name="ck_comm_acct_balance",
        ),
        CheckConstraint(
            "account_type IN ('AGENT_COMMISSION', 'COMMUNITY_EXPENSE', 'COMMUNITY_CLEARING', 'PAYOUT_CLEARING', 'ADJUSTMENT_CLEARING')",
            name="ck_comm_acct_type",
        ),
        CheckConstraint(
            "status IN ('active', 'frozen', 'closed')",
            name="ck_comm_acct_status",
        ),
        CheckConstraint(
            "length(currency) = 3",
            name="ck_comm_acct_currency",
        ),
        UniqueConstraint("tenant_id", "account_number", name="uq_comm_accts_number"),
        Index("ix_comm_accts_tenant_id", "tenant_id"),
        Index(
            "uq_comm_accts_agent",
            "tenant_id",
            "agent_membership_id",
            "currency",
            unique=True,
            postgresql_where=text("agent_membership_id IS NOT NULL"),
            sqlite_where=text("agent_membership_id IS NOT NULL"),
        ),
        Index(
            "uq_comm_accts_system",
            "tenant_id",
            "account_type",
            "currency",
            unique=True,
            postgresql_where=text("agent_membership_id IS NULL"),
            sqlite_where=text("agent_membership_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    agent_membership_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant_memberships.id", ondelete="RESTRICT"), nullable=True
    )
    agent_profile_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="RESTRICT"), nullable=True
    )
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)
    account_number: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), server_default="INR", default="INR", nullable=False)
    current_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), server_default="0.00", default=Decimal("0.00"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), server_default="active", default="active", nullable=False)
    allow_negative: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    ledger_entries: Mapped[list[CommissionLedgerEntry]] = relationship(
        "CommissionLedgerEntry", back_populates="account", order_by="CommissionLedgerEntry.created_at.desc()"
    )


class CommissionJournal(Base):
    """Header record representing an atomic balanced financial transaction."""

    __tablename__ = "commission_journals"
    __table_args__ = (
        CheckConstraint("total_amount > 0", name="ck_comm_journal_amount"),
        CheckConstraint("status IN ('POSTED', 'REVERSED')", name="ck_comm_journal_status"),
        CheckConstraint("length(currency) = 3", name="ck_comm_journal_currency"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_comm_journals_idempotency"),
        UniqueConstraint("tenant_id", "journal_number", name="uq_comm_journals_number"),
        Index("ix_comm_journals_tenant_posted", "tenant_id", "posted_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    journal_number: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    reference_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reference_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), server_default="INR", default="INR", nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="POSTED", default="POSTED", nullable=False)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    entries: Mapped[list[CommissionLedgerEntry]] = relationship(
        "CommissionLedgerEntry", back_populates="journal", cascade="all, delete-orphan", order_by="CommissionLedgerEntry.created_at.asc()"
    )


class CommissionLedgerEntry(Base):
    """Immutable debit or credit line item in the double-entry financial ledger."""

    __tablename__ = "commission_ledger_entries"
    __table_args__ = (
        CheckConstraint("entry_type IN ('DEBIT', 'CREDIT')", name="ck_comm_ledger_type"),
        CheckConstraint("amount > 0.00", name="ck_comm_ledger_amount"),
        CheckConstraint("length(currency) = 3", name="ck_comm_ledger_currency"),
        Index("ix_comm_ledger_acct_created", "tenant_id", "account_id", "created_at"),
        Index("ix_comm_ledger_journal_id", "journal_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    journal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("commission_journals.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("commission_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    entry_type: Mapped[str] = mapped_column(String(10), nullable=False)  # "DEBIT" or "CREDIT"
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), server_default="INR", default="INR", nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    journal: Mapped[CommissionJournal] = relationship("CommissionJournal", back_populates="entries")
    account: Mapped[CommissionAccount] = relationship("CommissionAccount", back_populates="ledger_entries")

