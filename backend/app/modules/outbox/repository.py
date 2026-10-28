from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.modules.outbox.models import (
    AuditOutboxAttempt,
    AuditOutboxEvent,
    AuditOutboxWorkerHeartbeat,
)
from app.modules.outbox.sink import AuditAcknowledgement


@dataclass(frozen=True, slots=True)
class ClaimedAuditEvent:
    event_id: UUID
    idempotency_key: str
    payload: dict[str, object]
    payload_sha256: str
    attempt_count: int


class OutboxDataUnavailable(Exception):
    """Outbox state could not be safely claimed or updated."""


class SQLAlchemyOutboxRepository:
    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    def claim_batch(
        self,
        *,
        now: datetime,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[ClaimedAuditEvent]:
        eligible = or_(
            and_(
                AuditOutboxEvent.status.in_(("queued", "retryable_failure")),
                AuditOutboxEvent.next_attempt_at <= now,
            ),
            and_(
                AuditOutboxEvent.status == "delivering",
                AuditOutboxEvent.lease_expires_at <= now,
            ),
        )
        try:
            with self._session_factory() as session, session.begin():
                rows = list(
                    session.scalars(
                        select(AuditOutboxEvent)
                        .where(eligible)
                        .order_by(AuditOutboxEvent.next_attempt_at, AuditOutboxEvent.created_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                claimed: list[ClaimedAuditEvent] = []
                for row in rows:
                    if row.status == "delivering":
                        previous_attempt = session.scalar(
                            select(AuditOutboxAttempt).where(
                                AuditOutboxAttempt.event_id == row.event_id,
                                AuditOutboxAttempt.attempt_number == row.attempt_count,
                                AuditOutboxAttempt.status == "in_progress",
                            )
                        )
                        if previous_attempt is not None:
                            previous_attempt.status = "retryable_failure"
                            previous_attempt.completed_at = now
                            previous_attempt.error_code = "worker_lease_expired"
                    row.status = "delivering"
                    row.attempt_count += 1
                    row.worker_id = worker_id
                    row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                    row.last_error_code = None
                    session.add(
                        AuditOutboxAttempt(
                            id=uuid4(),
                            event_id=row.event_id,
                            attempt_number=row.attempt_count,
                            status="in_progress",
                            worker_id=worker_id,
                            started_at=now,
                        )
                    )
                    claimed.append(
                        ClaimedAuditEvent(
                            event_id=row.event_id,
                            idempotency_key=row.idempotency_key,
                            payload=dict(row.payload),
                            payload_sha256=row.payload_sha256,
                            attempt_count=row.attempt_count,
                        )
                    )
                return claimed
        except SQLAlchemyError as exc:
            raise OutboxDataUnavailable from exc

    def record_worker_heartbeat(self, *, worker_id: str, adapter_name: str, now: datetime) -> None:
        try:
            with self._session_factory() as session, session.begin():
                row = session.get(AuditOutboxWorkerHeartbeat, worker_id)
                if row is None:
                    session.add(
                        AuditOutboxWorkerHeartbeat(
                            worker_id=worker_id,
                            adapter_name=adapter_name,
                            last_seen_at=now,
                            sink_verified_at=now,
                        )
                    )
                else:
                    row.adapter_name = adapter_name
                    row.last_seen_at = now
                    row.sink_verified_at = now
        except SQLAlchemyError as exc:
            raise OutboxDataUnavailable from exc

    def has_recent_verified_worker(
        self, *, now: datetime, ttl_seconds: int, adapter_name: str
    ) -> bool:
        cutoff = now - timedelta(seconds=ttl_seconds)
        try:
            with self._session_factory() as session:
                return session.scalar(
                    select(AuditOutboxWorkerHeartbeat.worker_id)
                    .where(
                        AuditOutboxWorkerHeartbeat.last_seen_at >= cutoff,
                        AuditOutboxWorkerHeartbeat.last_seen_at <= now,
                        AuditOutboxWorkerHeartbeat.sink_verified_at >= cutoff,
                        AuditOutboxWorkerHeartbeat.sink_verified_at <= now,
                        AuditOutboxWorkerHeartbeat.adapter_name == adapter_name,
                    )
                    .limit(1)
                ) is not None
        except SQLAlchemyError as exc:
            raise OutboxDataUnavailable from exc

    def mark_delivered(
        self,
        event: ClaimedAuditEvent,
        *,
        worker_id: str,
        acknowledgement: AuditAcknowledgement,
        now: datetime,
    ) -> bool:
        try:
            with self._session_factory() as session, session.begin():
                result = session.execute(
                    update(AuditOutboxEvent)
                    .where(
                        AuditOutboxEvent.event_id == event.event_id,
                        AuditOutboxEvent.status == "delivering",
                        AuditOutboxEvent.worker_id == worker_id,
                    )
                    .values(
                        status="delivered",
                        worker_id=None,
                        lease_expires_at=None,
                        acknowledgement_ref=acknowledgement.reference,
                        acknowledged_at=acknowledgement.acknowledged_at,
                        last_error_code=None,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    return False
                attempt_result = session.execute(
                    update(AuditOutboxAttempt)
                    .where(
                        AuditOutboxAttempt.event_id == event.event_id,
                        AuditOutboxAttempt.attempt_number == event.attempt_count,
                        AuditOutboxAttempt.worker_id == worker_id,
                        AuditOutboxAttempt.status == "in_progress",
                    )
                    .values(
                        status="delivered",
                        completed_at=now,
                        acknowledgement_ref=acknowledgement.reference,
                    )
                )
                if attempt_result.rowcount != 1:
                    raise OutboxDataUnavailable
                return True
        except SQLAlchemyError as exc:
            raise OutboxDataUnavailable from exc

    def mark_failure(
        self,
        event: ClaimedAuditEvent,
        *,
        worker_id: str,
        now: datetime,
        error_code: str,
        retryable: bool,
        max_attempts: int,
        retry_delay_seconds: int,
    ) -> bool:
        terminal = not retryable or event.attempt_count >= max_attempts
        status = "terminal_failure" if terminal else "retryable_failure"
        values: dict[str, object] = {
            "status": status,
            "worker_id": None,
            "lease_expires_at": None,
            "last_error_code": error_code[:80],
            "updated_at": now,
        }
        if not terminal:
            values["next_attempt_at"] = now + timedelta(seconds=retry_delay_seconds)
        try:
            with self._session_factory() as session, session.begin():
                result = session.execute(
                    update(AuditOutboxEvent)
                    .where(
                        AuditOutboxEvent.event_id == event.event_id,
                        AuditOutboxEvent.status == "delivering",
                        AuditOutboxEvent.worker_id == worker_id,
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    return False
                attempt_result = session.execute(
                    update(AuditOutboxAttempt)
                    .where(
                        AuditOutboxAttempt.event_id == event.event_id,
                        AuditOutboxAttempt.attempt_number == event.attempt_count,
                        AuditOutboxAttempt.worker_id == worker_id,
                        AuditOutboxAttempt.status == "in_progress",
                    )
                    .values(
                        status=status,
                        completed_at=now,
                        error_code=error_code[:80],
                    )
                )
                if attempt_result.rowcount != 1:
                    raise OutboxDataUnavailable
                return True
        except SQLAlchemyError as exc:
            raise OutboxDataUnavailable from exc
