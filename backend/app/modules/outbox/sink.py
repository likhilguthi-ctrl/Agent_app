from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


class AuditSinkError(Exception):
    """Base error returned by an external audit-sink adapter."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class RetryableAuditSinkError(AuditSinkError):
    """The sink did not accept the event; a later retry may succeed."""


class TerminalAuditSinkError(AuditSinkError):
    """The event or request was permanently rejected by the sink contract."""


class AuditSinkNotConfigured(RetryableAuditSinkError):
    """No supported durable external sink adapter is configured."""


@dataclass(frozen=True, slots=True)
class AuditAcknowledgement:
    event_id: UUID
    payload_sha256: str
    reference: str
    acknowledged_at: datetime
    verified: bool


class AuditSink(Protocol):
    """Adapter contract; `verified` acknowledgements must be cryptographically verifiable.

    `check_health` must only return true after validating the configured remote
    acknowledgement contract. Queueing or receiving HTTP 2xx alone is not enough.
    """

    configured: bool
    supported: bool

    async def deliver(
        self, payload: dict[str, object], *, idempotency_key: str
    ) -> AuditAcknowledgement: ...

    async def check_health(self) -> bool: ...

    async def aclose(self) -> None: ...


class UnavailableAuditSink:
    """Fail-closed placeholder used while no supported sink adapter is configured."""

    def __init__(self, *, configured: bool = False) -> None:
        self.configured = configured
        self.supported = False

    async def deliver(
        self, payload: dict[str, object], *, idempotency_key: str
    ) -> AuditAcknowledgement:
        raise AuditSinkNotConfigured("sink_adapter_unavailable")

    async def check_health(self) -> bool:
        return False

    async def aclose(self) -> None:
        return None
