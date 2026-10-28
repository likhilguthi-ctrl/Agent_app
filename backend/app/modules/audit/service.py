from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.modules.audit.models import AuditEvent
from app.modules.outbox.models import AuditOutboxEvent


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,119}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
_SAFE_SYSTEM_ACTOR = re.compile(r"^system:[a-z][a-z0-9_.-]{1,79}$")
_SAFE_CLAIM_VALUE = re.compile(r"^[A-Za-z0-9._:/+-]{1,128}$")
_SAFE_METADATA_KEYS = {"reason_code", "policy_version", "auth_strength", "attempt_number"}


class AuditInputRejected(ValueError):
    """Audit data did not meet the safe, non-sensitive event contract."""


@dataclass(frozen=True, slots=True)
class AuditEventDraft:
    event_type: str
    actor_category: str
    actor_ref: str | None
    action: str
    outcome: str
    tenant_id: UUID | None = None
    target_entity_type: str | None = None
    target_entity_ref: str | None = None
    request_id: str | None = None
    decision_metadata: dict[str, object] = field(default_factory=dict)
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecordedAuditEvent:
    event_id: UUID
    outbox_event_id: UUID
    payload_sha256: str


class AuditService:
    """Record a mandatory audit event and its delivery request in the caller's transaction.

    This service never commits or suppresses database failures. The caller owns
    the transaction and must roll it back if any part of the business operation
    or event recording fails.
    """

    def record_mandatory(self, session: Session, draft: AuditEventDraft) -> RecordedAuditEvent:
        self._validate(draft)
        event_id = uuid4()
        occurred_at = draft.occurred_at or datetime.now(UTC)
        metadata = dict(draft.decision_metadata)
        payload: dict[str, object] = {
            "event_id": str(event_id),
            "event_type": draft.event_type,
            "schema_version": 1,
            "occurred_at": occurred_at.isoformat(),
            "actor": {"category": draft.actor_category, "ref": draft.actor_ref},
            "tenant_id": str(draft.tenant_id) if draft.tenant_id else None,
            "target": {
                "entity_type": draft.target_entity_type,
                "entity_ref": draft.target_entity_ref,
            },
            "action": draft.action,
            "outcome": draft.outcome,
            "request_id": draft.request_id,
            "decision_metadata": metadata,
        }
        canonical_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        payload_digest = hashlib.sha256(canonical_payload).hexdigest()

        session.add(
            AuditEvent(
                id=event_id,
                event_type=draft.event_type,
                schema_version=1,
                occurred_at=occurred_at,
                actor_category=draft.actor_category,
                actor_ref=draft.actor_ref,
                tenant_id=draft.tenant_id,
                target_entity_type=draft.target_entity_type,
                target_entity_ref=draft.target_entity_ref,
                action=draft.action,
                outcome=draft.outcome,
                request_id=draft.request_id,
                decision_metadata=metadata,
            )
        )
        session.add(
            AuditOutboxEvent(
                event_id=event_id,
                idempotency_key=str(event_id),
                payload=payload,
                payload_sha256=payload_digest,
                status="queued",
                attempt_count=0,
                next_attempt_at=occurred_at,
            )
        )
        # Surface FK/unique/check failures to the business transaction.
        session.flush()
        return RecordedAuditEvent(
            event_id=event_id,
            outbox_event_id=event_id,
            payload_sha256=payload_digest,
        )

    @staticmethod
    def _validate(draft: AuditEventDraft) -> None:
        if not _SAFE_CODE.fullmatch(draft.event_type) or not _SAFE_CODE.fullmatch(draft.action):
            raise AuditInputRejected("Audit event type and action must use registered safe codes.")
        if draft.actor_category not in {
            "platform_operator", "tenant_member", "system", "unauthenticated"
        }:
            raise AuditInputRejected("Audit actor category is not supported.")
        if (draft.actor_category == "unauthenticated") != (draft.actor_ref is None):
            raise AuditInputRejected("Audit actor reference does not match its category.")
        if draft.actor_ref is not None:
            if draft.actor_category == "system":
                if not _SAFE_SYSTEM_ACTOR.fullmatch(draft.actor_ref):
                    raise AuditInputRejected("System actor reference must use a registered service code.")
            else:
                try:
                    if str(UUID(draft.actor_ref)) != draft.actor_ref.lower():
                        raise ValueError
                except (ValueError, AttributeError) as exc:
                    raise AuditInputRejected(
                        "Human actor references must be stable UUIDs, not personal identifiers."
                    ) from exc
        if draft.outcome not in {"success", "denied", "failure", "pending"}:
            raise AuditInputRejected("Audit outcome is not supported.")
        if draft.target_entity_type is not None and not _SAFE_CODE.fullmatch(draft.target_entity_type):
            raise AuditInputRejected("Audit target entity type must use a safe code.")
        if draft.target_entity_ref is not None:
            try:
                if str(UUID(draft.target_entity_ref)) != draft.target_entity_ref.lower():
                    raise ValueError
            except (ValueError, AttributeError) as exc:
                raise AuditInputRejected(
                    "Audit target reference must be a stable UUID, not personal data."
                ) from exc
        if draft.request_id is not None and not _SAFE_REQUEST_ID.fullmatch(draft.request_id):
            raise AuditInputRejected("Audit request ID has an invalid shape.")
        if not isinstance(draft.decision_metadata, dict):
            raise AuditInputRejected("Audit decision metadata must be a flat object.")
        if draft.occurred_at is not None and (
            not isinstance(draft.occurred_at, datetime) or draft.occurred_at.tzinfo is None
        ):
            raise AuditInputRejected("Audit event timestamps must be timezone-aware.")
        for key, value in draft.decision_metadata.items():
            if key not in _SAFE_METADATA_KEYS:
                raise AuditInputRejected("Audit decision metadata contains an unapproved key.")
            if key == "reason_code":
                if not isinstance(value, str) or not _SAFE_REASON.fullmatch(value):
                    raise AuditInputRejected("Audit reason code must be a registered safe code.")
            elif key == "attempt_number":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise AuditInputRejected("Audit attempt number must be a non-negative integer.")
            elif key in {"auth_strength", "policy_version"} and (
                not isinstance(value, str) or not _SAFE_CLAIM_VALUE.fullmatch(value)
            ):
                raise AuditInputRejected("Audit decision metadata values must be short safe strings.")
            if key == "auth_strength" and value not in {"mfa_verified", "verified"}:
                raise AuditInputRejected("Audit authentication strength must use a safe classification.")

