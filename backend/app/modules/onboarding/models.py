from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Uuid,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


ONBOARDING_STATES = (
    "PENDING_DOMAIN_PROOF",
    "PENDING_ACTIVATION_REVIEW",
    "REJECTED",
    "EXPIRED",
    "CANCELLED",
    "ACTIVE",
)
CHALLENGE_STATES = ("active", "checking", "consumed", "expired", "superseded", "locked")
REVIEW_DECISIONS = ("approve", "reject")
ACTIVATION_ATTEMPT_STATES = ("PENDING_AUDIT_ACK", "ACTIVATED", "CANCELLED")


class TenantOnboardingRequest(Base):
    """Append-only request identity with a service-controlled state column."""

    __tablename__ = "tenant_onboarding_requests"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING_DOMAIN_PROOF', 'PENDING_ACTIVATION_REVIEW', 'REJECTED', 'EXPIRED', 'CANCELLED', 'ACTIVE')",
            name="ck_tenant_onboarding_requests_state",
        ),
        CheckConstraint("version >= 1", name="ck_tenant_onboarding_requests_version"),
        CheckConstraint(
            "length(normalized_domain) BETWEEN 1 AND 253",
            name="ck_tenant_onboarding_requests_domain_length",
        ),
        CheckConstraint(
            "state != 'PENDING_ACTIVATION_REVIEW' OR tenant_domain_id IS NOT NULL",
            name="ck_tenant_onboarding_requests_review_requires_domain",
        ),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["created_by_operator_id"], ["platform_operators.id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["tenant_domain_id"], ["tenant_domains.id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("tenant_id", name="uq_tenant_onboarding_requests_tenant"),
        UniqueConstraint(
            "id", "normalized_domain", name="uq_tenant_onboarding_requests_id_domain"
        ),
        UniqueConstraint("tenant_domain_id", name="uq_tenant_onboarding_requests_domain_link"),
        UniqueConstraint(
            "id", "created_by_operator_id", name="uq_tenant_onboarding_requests_id_creator"
        ),
        UniqueConstraint(
            "created_by_operator_id",
            "idempotency_key",
            name="uq_tenant_onboarding_requests_operator_idempotency",
        ),
        Index(
            "uq_tenant_onboarding_requests_pending_domain",
            "normalized_domain",
            unique=True,
            postgresql_where=text(
                "state IN ('PENDING_DOMAIN_PROOF', 'PENDING_ACTIVATION_REVIEW')"
            ),
            sqlite_where=text(
                "state IN ('PENDING_DOMAIN_PROOF', 'PENDING_ACTIVATION_REVIEW')"
            ),
        ),
        Index("ix_tenant_onboarding_requests_state_expiry", "state", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tenant_domain_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by_operator_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    normalized_domain: Mapped[str] = mapped_column(String(253), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DomainVerificationChallenge(Base):
    """One-time DNS proof challenge; the reusable challenge value is never stored."""

    __tablename__ = "domain_verification_challenges"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'checking', 'consumed', 'expired', 'superseded', 'locked')",
            name="ck_domain_verification_challenges_status",
        ),
        CheckConstraint(
            "purpose = 'tenant_domain_ownership_v1'",
            name="ck_domain_verification_challenges_purpose",
        ),
        CheckConstraint(
            "length(challenge_hash) = 64",
            name="ck_domain_verification_challenges_hash_length",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_domain_verification_challenges_attempt_count"),
        CheckConstraint(
            "(status = 'checking' AND verification_lease_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'checking' AND verification_lease_id IS NULL AND lease_expires_at IS NULL)",
            name="ck_domain_verification_challenges_lease_state",
        ),
        CheckConstraint(
            "(status = 'consumed' AND consumed_at IS NOT NULL) OR "
            "(status != 'consumed' AND consumed_at IS NULL)",
            name="ck_domain_verification_challenges_consumption_state",
        ),
        ForeignKeyConstraint(
            ["request_id", "normalized_domain"],
            ["tenant_onboarding_requests.id", "tenant_onboarding_requests.normalized_domain"],
            ondelete="RESTRICT",
            name="fk_domain_verification_challenges_request_domain",
        ),
        ForeignKeyConstraint(
            ["issued_by_operator_id"], ["platform_operators.id"], ondelete="RESTRICT"
        ),
        UniqueConstraint(
            "request_id", "idempotency_key", name="uq_domain_verification_challenges_idempotency"
        ),
        Index("ix_domain_verification_challenges_request_issued", "request_id", "issued_at"),
        Index(
            "uq_domain_verification_challenges_current",
            "request_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'checking')"),
            sqlite_where=text("status IN ('active', 'checking')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    issued_by_operator_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(40), nullable=False, default="tenant_domain_ownership_v1"
    )
    normalized_domain: Mapped[str] = mapped_column(String(253), nullable=False)
    challenge_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verification_lease_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantOnboardingReview(Base):
    """The immutable independent decision for a single onboarding request."""

    __tablename__ = "tenant_onboarding_reviews"
    __table_args__ = (
        CheckConstraint("decision IN ('approve', 'reject')", name="ck_onboarding_reviews_decision"),
        CheckConstraint(
            "requester_operator_id != reviewer_operator_id",
            name="ck_onboarding_reviews_independent_reviewer",
        ),
        CheckConstraint(
            "length(reason_code) BETWEEN 2 AND 80", name="ck_onboarding_reviews_reason_length"
        ),
        CheckConstraint(
            "length(policy_version) BETWEEN 1 AND 64", name="ck_onboarding_reviews_policy_version_length"
        ),
        ForeignKeyConstraint(
            ["request_id", "requester_operator_id"],
            ["tenant_onboarding_requests.id", "tenant_onboarding_requests.created_by_operator_id"],
            ondelete="RESTRICT",
            name="fk_onboarding_reviews_requester",
        ),
        ForeignKeyConstraint(
            ["reviewer_operator_id"], ["platform_operators.id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(["audit_event_id"], ["audit_events.id"], ondelete="RESTRICT"),
        UniqueConstraint("request_id", name="uq_tenant_onboarding_reviews_request"),
        UniqueConstraint("audit_event_id", name="uq_tenant_onboarding_reviews_audit_event"),
        UniqueConstraint(
            "request_id", "idempotency_key", name="uq_tenant_onboarding_reviews_idempotency"
        ),
        Index("ix_tenant_onboarding_reviews_reviewer_decision", "reviewer_operator_id", "decision_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    requester_operator_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reviewer_operator_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    audit_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(12), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantActivationAttempt(Base):
    """Durable activation intent; an acknowledged intent is required before activation."""

    __tablename__ = "tenant_activation_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_AUDIT_ACK', 'ACTIVATED', 'CANCELLED')",
            name="ck_tenant_activation_attempts_status",
        ),
        CheckConstraint(
            "(status = 'ACTIVATED' AND completed_at IS NOT NULL) OR "
            "(status != 'ACTIVATED' AND completed_at IS NULL)",
            name="ck_tenant_activation_attempts_completion",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 128",
            name="ck_tenant_activation_attempts_idempotency_length",
        ),
        ForeignKeyConstraint(["request_id"], ["tenant_onboarding_requests.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["initiated_by_operator_id"], ["platform_operators.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["intent_audit_event_id"], ["audit_events.id"], ondelete="RESTRICT"),
        UniqueConstraint(
            "request_id", "idempotency_key", name="uq_tenant_activation_attempts_idempotency"
        ),
        UniqueConstraint(
            "intent_audit_event_id", name="uq_tenant_activation_attempts_intent_event"
        ),
        ForeignKeyConstraint(
            ["completion_audit_event_id"],
            ["audit_events.id"],
            ondelete="RESTRICT",
            name="fk_tenant_activation_attempts_completion_audit_event",
        ),
        UniqueConstraint(
            "completion_audit_event_id", name="uq_tenant_activation_attempts_completion_event"
        ),
        Index(
            "uq_tenant_activation_attempts_pending",
            "request_id",
            unique=True,
            postgresql_where=text("status = 'PENDING_AUDIT_ACK'"),
            sqlite_where=text("status = 'PENDING_AUDIT_ACK'"),
        ),
        Index("ix_tenant_activation_attempts_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    initiated_by_operator_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    intent_audit_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    completion_audit_event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING_AUDIT_ACK")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

