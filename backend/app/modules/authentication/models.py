from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


from sqlalchemy import JSON


class OtpChallenge(Base):
    """Mobile-bound OTP challenge with server-side hashing, expiry, rate limits, and registration verification."""

    __tablename__ = "otp_challenges"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        CheckConstraint(
            "length(mobile_e164) BETWEEN 9 AND 16 AND substr(mobile_e164, 1, 1) = '+'",
            name="ck_otp_challenges_mobile_e164_shape",
        ),
        UniqueConstraint("mobile_e164", "purpose", name="uq_otp_challenges_mobile_purpose"),
        Index("ix_otp_challenges_verification_token", "verification_token"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    mobile_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="login")
    otp_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    otp_salt: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resend_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatch_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class EmailOtpChallenge(Base):
    """Email-bound OTP challenge with salt/hash, expiry, failed attempts, and registration verification token."""

    __tablename__ = "email_otp_challenges"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        Index("ix_email_otp_challenges_email_purpose", "email", "purpose"),
        Index("ix_email_otp_challenges_verification_token", "verification_token"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="registration")
    otp_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    otp_salt: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resend_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatch_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class OtpRateBucket(Base):
    """Shared atomic rate-limit state; counters live in PostgreSQL, not process memory."""

    __tablename__ = "otp_rate_buckets"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        UniqueConstraint(
            "tenant_id", "mobile_e164", "action", name="uq_otp_rate_buckets_scope_action"
        ),
        CheckConstraint(
            "action IN ('send', 'verify')", name="ck_otp_rate_buckets_action"
        ),
        CheckConstraint("attempt_count >= 1", name="ck_otp_rate_buckets_attempt_count"),
        CheckConstraint(
            "length(mobile_e164) BETWEEN 9 AND 16 AND substr(mobile_e164, 1, 1) = '+'",
            name="ck_otp_rate_buckets_mobile_e164_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mobile_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthSession(Base):
    """Opaque server-revocable session; only a digest of the bearer cookie is stored."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            ondelete="RESTRICT",
            name="fk_auth_sessions_tenant_membership",
        ),
        UniqueConstraint("token_digest", name="uq_auth_sessions_token_digest"),
        CheckConstraint("length(token_digest) = 64", name="ck_auth_sessions_token_digest_length"),
        Index("ix_auth_sessions_membership_expiry", "tenant_id", "membership_id", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # CSRF tokens are not authentication credentials; the session cookie remains HttpOnly.
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountCredential(Base):
    """Local username/password credential mapped to either a platform operator or a tenant user."""

    __tablename__ = "account_credentials"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('platform_operator', 'tenant_user')",
            name="ck_account_credentials_account_type",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_account_credentials_status",
        ),
        CheckConstraint(
            "(account_type = 'platform_operator' AND operator_id IS NOT NULL AND user_id IS NULL) OR "
            "(account_type = 'tenant_user' AND user_id IS NOT NULL AND operator_id IS NULL)",
            name="ck_account_credentials_target_presence",
        ),
        CheckConstraint(
            "username = lower(username)",
            name="ck_account_credentials_username_lowercase",
        ),
        CheckConstraint(
            "length(username) BETWEEN 3 AND 100",
            name="ck_account_credentials_username_length",
        ),
        ForeignKeyConstraint(
            ["operator_id"], ["platform_operators.id"], ondelete="CASCADE",
            name="fk_account_credentials_operator",
        ),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_account_credentials_user",
        ),
        UniqueConstraint("username", name="uq_account_credentials_username"),
        Index("ix_account_credentials_username", "username"),
        Index("ix_account_credentials_operator_id", "operator_id"),
        Index("ix_account_credentials_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)
    operator_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

