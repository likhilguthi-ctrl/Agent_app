from __future__ import annotations


class OnboardingError(Exception):
    status_code = 400
    code = "ONBOARDING_REQUEST_FAILED"
    message = "The onboarding request could not be completed."
    retry_after_seconds: int | None = None

    def __init__(self, *, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(self.code)


class OnboardingNotFound(OnboardingError):
    status_code = 404
    code = "ONBOARDING_REQUEST_NOT_FOUND"
    message = "The onboarding request was not found."


class OnboardingInputInvalid(OnboardingError):
    status_code = 422
    code = "ONBOARDING_INPUT_INVALID"
    message = "The onboarding request input is invalid."


class OnboardingConflict(OnboardingError):
    status_code = 409
    code = "ONBOARDING_STATE_CONFLICT"
    message = "The onboarding request conflicts with its current state."


class DomainClaimUnavailable(OnboardingError):
    status_code = 409
    code = "DOMAIN_CLAIM_UNAVAILABLE"
    message = "This domain cannot be claimed."


class TenantSlugUnavailable(OnboardingError):
    status_code = 409
    code = "TENANT_SLUG_UNAVAILABLE"
    message = "This tenant slug cannot be used."


class IdempotencyConflict(OnboardingError):
    status_code = 409
    code = "IDEMPOTENCY_KEY_CONFLICT"
    message = "The idempotency key was already used for a different request."


class IdempotencyKeyInvalid(OnboardingError):
    status_code = 422
    code = "IDEMPOTENCY_KEY_INVALID"
    message = "A valid Idempotency-Key header is required."


class RequestExpired(OnboardingError):
    status_code = 410
    code = "ONBOARDING_REQUEST_EXPIRED"
    message = "The onboarding request has expired."


class ChallengeExpired(OnboardingError):
    status_code = 410
    code = "DOMAIN_CHALLENGE_EXPIRED"
    message = "The domain verification challenge has expired."


class ChallengeReplay(OnboardingError):
    status_code = 409
    code = "DOMAIN_CHALLENGE_REPLAYED"
    message = "The domain verification challenge cannot be reused."


class VerificationInProgress(OnboardingError):
    status_code = 409
    code = "DOMAIN_VERIFICATION_IN_PROGRESS"
    message = "A verification attempt is already in progress."


class VerificationRateLimited(OnboardingError):
    status_code = 429
    code = "DOMAIN_VERIFICATION_RATE_LIMITED"
    message = "Too many domain verification attempts were made."


class ChallengeIssueRateLimited(OnboardingError):
    status_code = 429
    code = "DOMAIN_CHALLENGE_ISSUE_LIMITED"
    message = "The challenge issuance limit has been reached for this request."


class RequestCreatorCannotReject(OnboardingError):
    status_code = 403
    code = "REQUEST_CREATOR_CANNOT_REJECT"
    message = "The request creator cannot reject this request."


class RequestCreatorCannotReview(OnboardingError):
    status_code = 403
    code = "ONBOARDING_CREATOR_CANNOT_REVIEW"
    message = "The request creator cannot review this request."


class ActivationReviewConflict(OnboardingError):
    status_code = 409
    code = "ONBOARDING_REVIEW_CONFLICT"
    message = "The onboarding request already has a conflicting review decision."


class ActivationBlocked(OnboardingError):
    code = "TENANT_ACTIVATION_BLOCKED"
    message = "Tenant activation prerequisites are not satisfied."

    def __init__(self, blockers: tuple[str, ...]) -> None:
        state_conflicts = {
            "REQUEST_NOT_REVIEWABLE",
            "REVIEW_NOT_APPROVED",
            "APPROVAL_AUDIT_NOT_ACKNOWLEDGED",
            "ACTIVATION_INTENT_NOT_ACKNOWLEDGED",
            "ACTIVATION_ATTEMPT_IN_PROGRESS",
            "ACTIVATION_ATTEMPT_NOT_PENDING",
            "ACTIVATION_ATTEMPT_NOT_FOUND",
            "ACTIVATION_ATTEMPT_CANCELLED",
            "DOMAIN_NOT_VERIFIED",
            "TENANT_ALREADY_ACTIVE",
            "DOMAIN_ALREADY_ACTIVE",
            "TENANT_DOMAIN_MISMATCH",
            "REQUEST_EXPIRED",
            "REVIEWER_CANNOT_ACTIVATE",
        }
        self.blockers = tuple(dict.fromkeys(blockers))
        self.status_code = 409 if state_conflicts.intersection(self.blockers) else 503
        super().__init__()


class DomainRegistryConflict(Exception):
    """Internal signal for the tenant-domain unique-index race."""


class DomainProofMismatch(OnboardingError):
    status_code = 422
    code = "DOMAIN_PROOF_NOT_FOUND"
    message = "The required DNS TXT proof was not found."


class DNSLookupUnavailable(OnboardingError):
    status_code = 503
    code = "DOMAIN_DNS_UNAVAILABLE"
    message = "DNS verification is temporarily unavailable."

