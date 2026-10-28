from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Protocol

from fastapi import Depends, Header, Request, Response

from app.modules.platform_identity.errors import (
    InvalidPlatformIdentity,
    PlatformIdentityNotConfigured,
    PlatformRequestForgery,
    PlatformRequestAuthenticationUnavailable,
)
from app.modules.platform_identity.service import PlatformPrincipal
from app.modules.platform_identity.oidc_login import SQLAlchemyPlatformOIDCSessionStore


class PlatformPrincipalResolver(Protocol):
    """Server-only adapter that resolves an already authenticated operator session.

    No client field, tenant role, email domain, or unsigned token is a principal.
    """

    async def resolve(self, request: Request) -> PlatformPrincipal: ...


class DatabasePlatformPrincipalResolver:
    """Resolve only an active operator session issued by the OIDC callback."""

    def __init__(self, session_store: SQLAlchemyPlatformOIDCSessionStore | None = None) -> None:
        self._session_store = session_store

    async def resolve(self, request: Request) -> PlatformPrincipal:
        settings = request.app.state.settings
        if not settings.platform_oidc_login_configured:
            raise PlatformIdentityNotConfigured
        raw_token = request.cookies.get(settings.platform_oidc_session_cookie_name)
        if not raw_token or len(raw_token) > 128:
            raise InvalidPlatformIdentity
        store = self._session_store or SQLAlchemyPlatformOIDCSessionStore()
        principal = store.resolve_session(raw_token, now=datetime.now(UTC))
        if principal is None:
            raise InvalidPlatformIdentity
        return principal


async def get_platform_operator_principal(request: Request) -> PlatformPrincipal:
    settings = request.app.state.settings
    resolver: PlatformPrincipalResolver | None = getattr(
        request.app.state, "platform_principal_resolver", None
    )
    test_resolver_override = bool(
        settings.environment == "test"
        and settings.platform_oidc_configured
        and resolver is not None
        and not isinstance(resolver, DatabasePlatformPrincipalResolver)
    )
    if not settings.platform_oidc_configured or (
        not settings.platform_oidc_login_configured and not test_resolver_override
    ):
        raise PlatformIdentityNotConfigured
    if resolver is None:
        raise PlatformRequestAuthenticationUnavailable
    principal = await resolver.resolve(request)
    now = datetime.now(UTC)
    principal_amr = getattr(principal, "amr", None)
    principal_acr = getattr(principal, "acr", None)
    if (
        not isinstance(principal, PlatformPrincipal)
        or principal.issuer != settings.platform_oidc_issuer
        or not isinstance(principal.subject, str)
        or not principal.subject
        or not isinstance(principal.authenticated_at, datetime)
        or not isinstance(principal_amr, tuple)
        or not all(isinstance(value, str) for value in principal_amr)
        or not set(settings.platform_oidc_required_amr_list).issubset(set(principal_amr))
        or (
            settings.platform_oidc_required_acr_list
            and principal_acr not in settings.platform_oidc_required_acr_list
        )
        or principal.authenticated_at.tzinfo is None
        or principal.authenticated_at > now
        or (now - principal.authenticated_at).total_seconds()
        > settings.platform_oidc_max_auth_age_seconds
    ):
        raise InvalidPlatformIdentity
    return principal


async def require_platform_csrf(
    request: Request,
    response: Response,
    principal: PlatformPrincipal = Depends(get_platform_operator_principal),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token", max_length=64),
) -> None:
    """Require the current platform session's CSRF token and an exact trusted Origin."""
    response.headers["Cache-Control"] = "no-store"
    settings = request.app.state.settings
    origin = request.headers.get("origin")
    if (
        principal.session_id is None
        or principal.csrf_token is None
        or csrf_header is None
        or origin not in settings.frontend_origin_list
        or not secrets.compare_digest(csrf_header, principal.csrf_token)
    ):
        raise PlatformRequestForgery
