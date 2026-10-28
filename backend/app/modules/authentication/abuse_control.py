from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, case, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.db.session import get_session_factory
from app.modules.authentication.models import OtpRateBucket


class OtpRateLimited(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("OTP operation rate limit reached.")


class AbuseControlUnavailable(Exception):
    """Shared OTP abuse-control storage is unavailable; OTP operations fail closed."""


class SQLAlchemyOtpAbuseGuard:
    """Atomic database-backed cooldown and rolling-window guard.

    PostgreSQL is the production store. SQLite is supported only so isolated tests can
    exercise the same conflict-update semantics without production infrastructure.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._settings = settings
        try:
            self._session_factory = session_factory or get_session_factory()
        except RuntimeError as exc:
            raise AbuseControlUnavailable from exc

    async def authorize_send(self, tenant_id: UUID, mobile_e164: str) -> None:
        await run_in_threadpool(self._consume, tenant_id, mobile_e164, "send")

    async def authorize_verify_attempt(self, tenant_id: UUID, mobile_e164: str) -> None:
        await run_in_threadpool(self._consume, tenant_id, mobile_e164, "verify")

    def _consume(self, tenant_id: UUID, mobile_e164: str, action: str) -> None:
        now = datetime.now(UTC)
        if action == "send":
            window_seconds = self._settings.otp_send_window_seconds
            max_attempts = self._settings.otp_send_limit
            cooldown_seconds = self._settings.otp_send_cooldown_seconds
        else:
            window_seconds = self._settings.otp_verify_window_seconds
            max_attempts = self._settings.otp_verify_limit
            cooldown_seconds = 0

        expired_window = OtpRateBucket.window_started_at <= now - timedelta(seconds=window_seconds)
        within_cooldown = or_(
            OtpRateBucket.cooldown_until.is_(None), OtpRateBucket.cooldown_until <= now
        )
        under_limit = or_(expired_window, OtpRateBucket.attempt_count < max_attempts)
        table = OtpRateBucket.__table__
        try:
            insert_factory = self._insert_for_dialect()
            with self._session_factory.begin() as session:
                statement = insert_factory(table).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    mobile_e164=mobile_e164,
                    action=action,
                    window_started_at=now,
                    attempt_count=1,
                    cooldown_until=now + timedelta(seconds=cooldown_seconds)
                    if action == "send"
                    else None,
                )
                set_values = {
                    "window_started_at": case(
                        (expired_window, now), else_=OtpRateBucket.window_started_at
                    ),
                    "attempt_count": case(
                        (expired_window, 1), else_=OtpRateBucket.attempt_count + 1
                    ),
                }
                if action == "send":
                    set_values["cooldown_until"] = now + timedelta(seconds=cooldown_seconds)
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        OtpRateBucket.tenant_id,
                        OtpRateBucket.mobile_e164,
                        OtpRateBucket.action,
                    ],
                    set_=set_values,
                    where=and_(under_limit, within_cooldown),
                ).returning(OtpRateBucket.id)
                changed = session.execute(statement).scalar_one_or_none()
                if changed is not None:
                    return

                existing = session.execute(
                    select(
                        OtpRateBucket.window_started_at,
                        OtpRateBucket.cooldown_until,
                        OtpRateBucket.attempt_count,
                    ).where(
                        OtpRateBucket.tenant_id == tenant_id,
                        OtpRateBucket.mobile_e164 == mobile_e164,
                        OtpRateBucket.action == action,
                    )
                ).one_or_none()
                retry_after = window_seconds
                if existing is not None:
                    window_started = existing.window_started_at
                    if window_started.tzinfo is None:
                        window_started = window_started.replace(tzinfo=UTC)
                    window_release = window_started + timedelta(seconds=window_seconds)
                    blocked_until = existing.cooldown_until
                    if blocked_until is not None and blocked_until.tzinfo is None:
                        blocked_until = blocked_until.replace(tzinfo=UTC)
                    if action == "send" and existing.attempt_count < max_attempts:
                        release_at = blocked_until or window_release
                    elif blocked_until is not None:
                        release_at = max(blocked_until, window_release)
                    else:
                        release_at = window_release
                    retry_after = max(1, int((release_at - now).total_seconds()))
                raise OtpRateLimited(retry_after)
        except OtpRateLimited:
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise AbuseControlUnavailable from exc

    def _insert_for_dialect(self):
        bind = self._session_factory.kw.get("bind")
        if bind is None:
            # The default factory is lazily bound; obtaining it may itself fail closed.
            from app.db.session import get_engine

            bind = get_engine()
        dialect = bind.dialect.name
        if dialect == "postgresql":
            return pg_insert
        if dialect == "sqlite":
            return sqlite_insert
        raise AbuseControlUnavailable("Unsupported OTP rate-limit database dialect.")
