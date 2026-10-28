from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.core.tenant_context import TenantContext
from app.modules.authentication.repository import AuthenticatedSession, IssuedSession
from app.modules.identity.contracts import CommunityMembership


class AuthenticationProviderError(Exception):
    """Base class for provider-independent authentication failures."""


class AuthenticationRejected(AuthenticationProviderError):
    """Credentials or one-time proof were rejected."""


class AuthenticationExpired(AuthenticationProviderError):
    """An authentication challenge, proof, or session has expired."""


class AuthenticationProviderUnavailable(AuthenticationProviderError):
    """No explicitly configured authentication provider is available."""


class AuthenticationProviderMisconfigured(AuthenticationProviderError):
    """The explicitly selected provider is missing required configuration."""


@dataclass(frozen=True, slots=True)
class AuthenticationChallenge:
    challenge_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedCredentialProof:
    """Provider-created proof handle; it contains no caller-selected identity or scope."""

    proof_id: UUID


@dataclass(frozen=True, slots=True)
class IssuedAuthenticationSession:
    session: IssuedSession
    raw_token: str = field(repr=False)


class AuthenticationProvider(Protocol):
    """Provider-neutral community authentication and existing-session lifecycle contract.

    TenantContext must be resolved by the backend before any provider call. Implementations
    must not treat request-supplied tenant, role, or identity fields as authority.
    """

    async def start_authentication(
        self,
        tenant: TenantContext,
        *,
        username: str,
        password: str,
    ) -> AuthenticationChallenge: ...

    async def verify_otp(
        self,
        tenant: TenantContext,
        *,
        challenge_id: UUID,
        otp: str,
    ) -> VerifiedCredentialProof: ...

    async def resolve_identity(
        self,
        tenant: TenantContext,
        proof: VerifiedCredentialProof,
    ) -> CommunityMembership: ...

    async def create_session(
        self,
        tenant: TenantContext,
        membership: CommunityMembership,
    ) -> IssuedAuthenticationSession: ...

    async def revoke_session(
        self,
        tenant: TenantContext,
        authenticated: AuthenticatedSession,
    ) -> None: ...


class UnconfiguredAuthenticationProvider:
    """Fail-closed provider used when no provider was explicitly configured.

    This does not select a development provider when a future real provider is absent.
    Existing OTP and platform OIDC routes are intentionally not adapted to this boundary.
    """

    async def start_authentication(
        self,
        tenant: TenantContext,
        *,
        username: str,
        password: str,
    ) -> AuthenticationChallenge:
        del tenant, username, password
        raise AuthenticationProviderUnavailable

    async def verify_otp(
        self,
        tenant: TenantContext,
        *,
        challenge_id: UUID,
        otp: str,
    ) -> VerifiedCredentialProof:
        del tenant, challenge_id, otp
        raise AuthenticationProviderUnavailable

    async def resolve_identity(
        self,
        tenant: TenantContext,
        proof: VerifiedCredentialProof,
    ) -> CommunityMembership:
        del tenant, proof
        raise AuthenticationProviderUnavailable

    async def create_session(
        self,
        tenant: TenantContext,
        membership: CommunityMembership,
    ) -> IssuedAuthenticationSession:
        del tenant, membership
        raise AuthenticationProviderUnavailable

    async def revoke_session(
        self,
        tenant: TenantContext,
        authenticated: AuthenticatedSession,
    ) -> None:
        del tenant, authenticated
        raise AuthenticationProviderUnavailable
