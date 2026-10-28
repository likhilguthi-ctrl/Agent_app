from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.modules.outbox.repository import ClaimedAuditEvent, SQLAlchemyOutboxRepository
from app.modules.outbox.sink import (
    AuditAcknowledgement,
    AuditSink,
    AuditSinkNotConfigured,
    RetryableAuditSinkError,
    TerminalAuditSinkError,
    UnavailableAuditSink,
)

_SAFE_ACK_REF = re.compile(r"^[A-Za-z0-9._:@/-]{1,255}$")


@dataclass(frozen=True, slots=True)
class OutboxDispatchResult:
    claimed: int
    delivered: int
    retryable_failures: int
    terminal_failures: int


def create_audit_sink(settings: Settings) -> AuditSink:
    """Return no successful fallback; no concrete provider adapter is selected in this project."""
    return UnavailableAuditSink(configured=settings.audit_sink_configuration_complete)


class AuditOutboxWorker:
    def __init__(
        self,
        settings: Settings,
        sink: AuditSink,
        *,
        repository: SQLAlchemyOutboxRepository | None = None,
        worker_id: str | None = None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._sink = sink
        self._repository = repository or SQLAlchemyOutboxRepository()
        self._worker_id = worker_id or str(uuid4())
        self._clock = clock

    async def run_once(self, *, now: datetime | None = None) -> OutboxDispatchResult:
        if not self._settings.outbox_worker_enabled:
            raise AuditSinkNotConfigured("outbox_worker_disabled")
        if not self._sink.configured or not self._sink.supported:
            raise AuditSinkNotConfigured("sink_adapter_unavailable")
        try:
            sink_healthy = await self._sink.check_health()
        except Exception as exc:
            raise AuditSinkNotConfigured("sink_health_unverified") from exc
        if not sink_healthy:
            raise AuditSinkNotConfigured("sink_health_unverified")

        dispatch_time = now or self._clock()
        self._repository.record_worker_heartbeat(
            worker_id=self._worker_id,
            adapter_name=self._settings.audit_sink_adapter or "unconfigured",
            now=dispatch_time,
        )
        events = self._repository.claim_batch(
            now=dispatch_time,
            worker_id=self._worker_id,
            limit=self._settings.outbox_worker_batch_size,
            lease_seconds=self._settings.outbox_worker_lease_seconds,
        )
        delivered = retryable = terminal = 0
        for event in events:
            try:
                acknowledgement = await self._sink.deliver(
                    event.payload, idempotency_key=event.idempotency_key
                )
                self._validate_acknowledgement(event, acknowledgement, self._clock())
                if self._repository.mark_delivered(
                    event,
                    worker_id=self._worker_id,
                    acknowledgement=acknowledgement,
                    now=dispatch_time,
                ):
                    delivered += 1
            except RetryableAuditSinkError as exc:
                delay = self._retry_delay(event.attempt_count, event.event_id)
                updated = self._repository.mark_failure(
                    event,
                    worker_id=self._worker_id,
                    now=dispatch_time,
                    error_code=exc.error_code,
                    retryable=True,
                    max_attempts=self._settings.outbox_max_attempts,
                    retry_delay_seconds=delay,
                )
                retryable += int(updated and event.attempt_count < self._settings.outbox_max_attempts)
                terminal += int(updated and event.attempt_count >= self._settings.outbox_max_attempts)
            except TerminalAuditSinkError as exc:
                updated = self._repository.mark_failure(
                    event,
                    worker_id=self._worker_id,
                    now=dispatch_time,
                    error_code=exc.error_code,
                    retryable=False,
                    max_attempts=self._settings.outbox_max_attempts,
                    retry_delay_seconds=0,
                )
                terminal += int(updated)
            except (TimeoutError, httpx.TimeoutException):
                updated = self._repository.mark_failure(
                    event,
                    worker_id=self._worker_id,
                    now=dispatch_time,
                    error_code="sink_timeout",
                    retryable=True,
                    max_attempts=self._settings.outbox_max_attempts,
                    retry_delay_seconds=self._retry_delay(event.attempt_count, event.event_id),
                )
                retryable += int(updated and event.attempt_count < self._settings.outbox_max_attempts)
                terminal += int(updated and event.attempt_count >= self._settings.outbox_max_attempts)
            except Exception:
                updated = self._repository.mark_failure(
                    event,
                    worker_id=self._worker_id,
                    now=dispatch_time,
                    error_code="sink_delivery_error",
                    retryable=False,
                    max_attempts=self._settings.outbox_max_attempts,
                    retry_delay_seconds=0,
                )
                terminal += int(updated)
        return OutboxDispatchResult(
            claimed=len(events),
            delivered=delivered,
            retryable_failures=retryable,
            terminal_failures=terminal,
        )

    def _retry_delay(self, attempt_count: int, event_id) -> int:
        exponent = min(max(attempt_count - 1, 0), 20)
        base = min(
            self._settings.outbox_retry_base_seconds * (2**exponent),
            self._settings.outbox_retry_max_seconds,
        )
        # Stable per event/attempt jitter spreads retries while remaining reproducible.
        digest = hashlib.sha256(f"{event_id}:{attempt_count}".encode("ascii")).digest()
        multiplier = 0.8 + (int.from_bytes(digest[:2], "big") / 65535) * 0.4
        return max(1, min(int(base * multiplier), self._settings.outbox_retry_max_seconds))

    @staticmethod
    def _validate_acknowledgement(
        event: ClaimedAuditEvent,
        acknowledgement: AuditAcknowledgement,
        now: datetime,
    ) -> None:
        if (
            not acknowledgement.verified
            or acknowledgement.event_id != event.event_id
            or acknowledgement.payload_sha256 != event.payload_sha256
            or not _SAFE_ACK_REF.fullmatch(acknowledgement.reference)
            or acknowledgement.acknowledged_at.tzinfo is None
            or acknowledgement.acknowledged_at > now
        ):
            raise TerminalAuditSinkError("invalid_acknowledgement")

