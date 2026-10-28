from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.error_responses import error_response
from app.core.request_id import request_id_context
from app.modules.platform_identity.dependencies import get_platform_operator_principal
from app.modules.platform_identity.errors import (
    InvalidPlatformIdentity,
    PlatformIdentityError,
    PlatformIdentityNotConfigured,
    PlatformOperatorNotAuthorized,
)
from app.modules.platform_identity.oidc_login import (
    PlatformOIDCLoginService,
    SQLAlchemyPlatformOIDCSessionStore,
)
from app.modules.platform_identity.provider import PlatformIdentityProvider
from app.modules.platform_identity.service import PlatformPrincipal

router = APIRouter(prefix="/platform/auth/oidc", tags=["platform authentication"])
_BINDING_COOKIE = "community_platform_oidc_binding"
_BINDING_COOKIE_PATH_SUFFIX = "/platform/auth/oidc"


class PlatformIdentityResponse(BaseModel):
    operator_id: str
    csrf_token: str
    expires_at: datetime


def get_platform_oidc_session_store() -> SQLAlchemyPlatformOIDCSessionStore:
    return SQLAlchemyPlatformOIDCSessionStore()


def get_platform_oidc_login_service(request: Request) -> PlatformOIDCLoginService:
    settings = request.app.state.settings
    provider: PlatformIdentityProvider | None = getattr(
        request.app.state, "platform_identity_provider", None
    )
    if not settings.platform_oidc_login_configured:
        raise PlatformIdentityNotConfigured
    if provider is None:
        raise PlatformIdentityNotConfigured
    return PlatformOIDCLoginService(
        settings,
        provider,
        store=get_platform_oidc_session_store(),
    )


@router.get("/start", summary="Begin platform operator OIDC login")
async def start_oidc_login(
    request: Request,
    service: Annotated[PlatformOIDCLoginService, Depends(get_platform_oidc_login_service)],
) -> Response:
    settings = request.app.state.settings
    login = await service.start()
    response = RedirectResponse(login.authorization_url, status_code=status.HTTP_302_FOUND)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.set_cookie(
        key=_BINDING_COOKIE,
        value=login.browser_binding,
        max_age=settings.platform_oidc_login_ttl_seconds,
        path=f"/backend-api{settings.api_v1_prefix}{_BINDING_COOKIE_PATH_SUFFIX}",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/callback", summary="Complete platform operator OIDC login")
async def complete_oidc_login(
    request: Request,
    service: Annotated[PlatformOIDCLoginService, Depends(get_platform_oidc_login_service)],
    state: Annotated[str | None, Query(max_length=256)] = None,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    error: Annotated[str | None, Query(max_length=128)] = None,
) -> Response:
    settings = request.app.state.settings
    binding = request.cookies.get(_BINDING_COOKIE)
    if error is not None or not state or not code or not binding:
        if error is not None:
            await service.record_authentication_failure(
                reason_code="identity_provider_error",
                outcome="failure",
                request_id=request_id_context.get(),
            )
        elif not state or not binding:
            await service.record_authentication_failure(
                reason_code="login_callback_binding_missing",
                outcome="denied",
                request_id=request_id_context.get(),
            )
        elif not code:
            await service.record_authentication_failure(
                reason_code="login_callback_code_missing",
                outcome="denied",
                request_id=request_id_context.get(),
            )
        response = error_response(
            status.HTTP_401_UNAUTHORIZED,
            "PLATFORM_AUTHENTICATION_FAILED",
            "Platform authentication could not be completed.",
            request_id_context.get(),
        )
        _clear_login_cookies(response, request)
        return response
    try:
        completion = await service.complete(
            state=state,
            code=code,
            browser_binding=binding,
            request_id=request_id_context.get(),
        )
    except (InvalidPlatformIdentity, PlatformOperatorNotAuthorized):
        response = error_response(
            status.HTTP_401_UNAUTHORIZED,
            "PLATFORM_AUTHENTICATION_FAILED",
            "Platform authentication could not be completed.",
            request_id_context.get(),
        )
        _clear_login_cookies(response, request)
        return response
    except PlatformIdentityError:
        response = error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "PLATFORM_AUTHENTICATION_UNAVAILABLE",
            "Platform authentication is temporarily unavailable.",
            request_id_context.get(),
        )
        _clear_login_cookies(response, request)
        return response

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.set_cookie(
        key=settings.platform_oidc_session_cookie_name,
        value=completion.raw_session_token,
        max_age=settings.platform_oidc_session_ttl_seconds,
        path=settings.platform_oidc_session_cookie_path,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.platform_oidc_session_cookie_samesite,
    )
    _delete_binding_cookie(response, request)
    return response


@router.get("/me", response_model=PlatformIdentityResponse, summary="Read current operator identity")
async def current_platform_identity(
    response: Response,
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
) -> PlatformIdentityResponse:
    response.headers["Cache-Control"] = "no-store"
    if principal.csrf_token is None or principal.expires_at is None:
        raise InvalidPlatformIdentity
    return PlatformIdentityResponse(
        operator_id=str(principal.operator_id),
        csrf_token=principal.csrf_token,
        expires_at=principal.expires_at,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke operator session")
async def logout_platform_operator(
    request: Request,
    response: Response,
    principal: Annotated[PlatformPrincipal, Depends(get_platform_operator_principal)],
    store: Annotated[SQLAlchemyPlatformOIDCSessionStore, Depends(get_platform_oidc_session_store)],
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token", max_length=64)] = None,
) -> Response:
    response.headers["Cache-Control"] = "no-store"
    settings = request.app.state.settings
    if (
        principal.session_id is None
        or principal.csrf_token is None
        or csrf_header is None
        or not secrets.compare_digest(csrf_header, principal.csrf_token)
        or not _request_origin_allowed(request, settings.frontend_origin_list)
    ):
        denied = error_response(
            status.HTTP_403_FORBIDDEN,
            "CSRF_VALIDATION_FAILED",
            "The request could not be validated.",
            request_id_context.get(),
        )
        denied.headers["Cache-Control"] = "no-store"
        return denied
    await run_in_threadpool(
        store.revoke_session,
        session_id=principal.session_id,
        operator_id=principal.operator_id,
        now=datetime.now(UTC),
        request_id=request_id_context.get(),
    )
    response.delete_cookie(
        key=settings.platform_oidc_session_cookie_name,
        path=settings.platform_oidc_session_cookie_path,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.platform_oidc_session_cookie_samesite,
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


def _request_origin_allowed(request: Request, allowed_origins: list[str]) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if not referer:
            return False
        parsed = urlsplit(referer)
        if not parsed.scheme or not parsed.netloc:
            return False
        origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin in allowed_origins


def _clear_login_cookies(response: Response, request: Request) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    _delete_binding_cookie(response, request)


def _delete_binding_cookie(response: Response, request: Request) -> None:
    settings = request.app.state.settings
    response.delete_cookie(
        key=_BINDING_COOKIE,
        path=f"/backend-api{settings.api_v1_prefix}{_BINDING_COOKIE_PATH_SUFFIX}",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
