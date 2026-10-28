from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.onboarding.models import ONBOARDING_STATES
from app.modules.tenants.hostname import normalize_hostname


_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$|^[a-z0-9]$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_DOMAIN_PROOF_OWNER_PREFIX = "_community-ott-verification."


class OnboardingState(StrEnum):
    PENDING_DOMAIN_PROOF = "PENDING_DOMAIN_PROOF"
    PENDING_ACTIVATION_REVIEW = "PENDING_ACTIVATION_REVIEW"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    ACTIVE = "ACTIVE"


class RejectionReason(StrEnum):
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    REQUEST_INELIGIBLE = "request_ineligible"
    POLICY_REVIEW_FAILED = "policy_review_failed"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ReviewReason(StrEnum):
    ORGANIZATION_VERIFIED = "organization_verified"
    POLICY_REVIEW_PASSED = "policy_review_passed"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    REQUEST_INELIGIBLE = "request_ineligible"
    POLICY_REVIEW_FAILED = "policy_review_failed"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OnboardingRequestCreate(StrictModel):
    tenant_slug: Annotated[str, Field(min_length=1, max_length=63)]
    domain: Annotated[str, Field(min_length=1, max_length=253)]

    @field_validator("tenant_slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        normalized = value.lower()
        if not _SLUG.fullmatch(normalized):
            raise ValueError("tenant_slug must be a lowercase DNS-style slug.")
        return normalized

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        if ":" in value:
            raise ValueError("domain must be a hostname without a port.")
        normalized = normalize_hostname(value)
        if "." not in normalized:
            raise ValueError("domain must be a fully qualified DNS hostname.")
        # RFC 1035 wire names encode every label with a length byte plus a root
        # terminator. The normalized hostname is ASCII IDNA at this point.
        qname_labels = (_DOMAIN_PROOF_OWNER_PREFIX[:-1] + "." + normalized).split(".")
        if sum(len(label.encode("ascii")) + 1 for label in qname_labels) + 1 > 255:
            raise ValueError("domain is too long for the DNS proof record name.")
        return normalized


class OnboardingRequestSummary(StrictModel):
    request_id: UUID
    tenant_slug: str
    domain: str
    state: OnboardingState
    created_at: datetime
    expires_at: datetime
    domain_verified: bool


class OnboardingRequestCreated(StrictModel):
    request: OnboardingRequestSummary
    replayed: bool


class OnboardingHistoryEntry(StrictModel):
    event_type: str
    action: str
    outcome: str
    occurred_at: datetime
    actor_category: str
    reason_code: str | None = None


class OnboardingRequestDetail(OnboardingRequestSummary):
    history: list[OnboardingHistoryEntry]


class OnboardingRequestList(StrictModel):
    items: list[OnboardingRequestSummary]
    limit: int
    offset: int


class DomainChallengeIssued(StrictModel):
    request_id: UUID
    dns_txt_name: str
    dns_txt_value: str | None
    expires_at: datetime
    replayed: bool


class RejectOnboardingRequest(StrictModel):
    reason_code: RejectionReason


class OnboardingReviewSubmission(StrictModel):
    decision: ReviewDecision
    reason_code: ReviewReason


class OnboardingReviewResult(StrictModel):
    request_id: UUID
    decision: ReviewDecision
    request_state: OnboardingState
    decision_at: datetime
    replayed: bool


class ActivationEligibility(StrictModel):
    request_id: UUID
    eligible: bool
    blockers: list[str]


class ActivationIntentResult(StrictModel):
    request_id: UUID
    activation_attempt_id: UUID
    state: str
    created_at: datetime
    replayed: bool


class TenantActivationResult(StrictModel):
    request_id: UUID
    activation_attempt_id: UUID
    request_state: OnboardingState
    tenant_active: bool
    domain_active: bool
    tenant_resolution_enabled: bool
    activated_at: datetime
    replayed: bool


def validate_idempotency_key(value: str) -> str:
    if not _IDEMPOTENCY_KEY.fullmatch(value):
        raise ValueError("Idempotency-Key must contain 8 to 128 safe ASCII characters.")
    return value


def terminal_state(state: str) -> bool:
    return state in {
        OnboardingState.REJECTED.value,
        OnboardingState.EXPIRED.value,
        OnboardingState.CANCELLED.value,
    }

