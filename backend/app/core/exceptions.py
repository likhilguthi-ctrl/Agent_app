from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_id import request_id_context
from app.core.error_responses import error_response, internal_server_error_response
from app.core.tenant_context import TenantResolutionError
from app.db.session import DatabaseUnavailable
from app.modules.authentication.otp_login import LoginRejected
from app.modules.authentication.abuse_control import AbuseControlUnavailable, OtpRateLimited
from app.modules.authentication.repository import AuthenticationStorageUnavailable
from app.modules.authentication.service import AuthenticationConfigurationUnavailable
from app.modules.authentication.errors import TenantMembershipRequired
from app.modules.identity.errors import MembershipStorageUnavailable
from app.modules.audit.errors import AuditStorageUnavailable
from app.modules.messaging.errors import MessagingError
from app.modules.authorization.errors import AuthorizationDataUnavailable, PermissionDenied
from app.modules.platform_identity.errors import (
    InvalidPlatformIdentity,
    PlatformAuthorizationUnavailable,
    PlatformIdentityNotConfigured,
    PlatformIdentityUnavailable,
    PlatformOperatorNotAuthorized,
    PlatformRequestForgery,
    PlatformRequestAuthenticationUnavailable,
)
from app.modules.onboarding.errors import OnboardingError

logger = logging.getLogger(__name__)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return error_response(
        status_code,
        code,
        message,
        request_id_context.get(),
        headers,
    )


_SAFE_HTTP_MESSAGES = {
    400: "The request could not be completed.",
    401: "Authentication is required.",
    403: "You are not authorized to perform this action.",
    404: "The requested resource was not found.",
    405: "The request method is not supported.",
    409: "The request conflicts with the current resource state.",
    422: "Request validation failed.",
    429: "Too many requests. Try again later.",
}


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DatabaseUnavailable)
    async def handle_database_unavailable(_: Request, __: DatabaseUnavailable) -> JSONResponse:
        return error_response(
            503,
            "DATABASE_UNAVAILABLE",
            "Database-backed operations are temporarily unavailable.",
            request_id_context.get(),
        )

    @app.exception_handler(TenantResolutionError)
    async def handle_tenant_resolution_error(_: Request, __: TenantResolutionError) -> JSONResponse:
        return error_response(
            404,
            "TENANT_NOT_RESOLVED",
            "Tenant context could not be resolved.",
            request_id_context.get(),
        )

    @app.exception_handler(LoginRejected)
    async def handle_login_rejected(_: Request, __: LoginRejected) -> JSONResponse:
        return error_response(
            401,
            "AUTHENTICATION_FAILED",
            "Login could not be completed.",
            request_id_context.get(),
        )

    @app.exception_handler(TenantMembershipRequired)
    async def handle_missing_tenant_membership(
        _: Request, __: TenantMembershipRequired
    ) -> JSONResponse:
        return error_response(
            403,
            "TENANT_MEMBERSHIP_REQUIRED",
            "An active community membership is required.",
            request_id_context.get(),
        )

    @app.exception_handler(PermissionDenied)
    async def handle_permission_denied(_: Request, __: PermissionDenied) -> JSONResponse:
        return error_response(
            403,
            "PERMISSION_DENIED",
            "You are not authorized to perform this action.",
            request_id_context.get(),
        )

    @app.exception_handler(AuthorizationDataUnavailable)
    async def handle_authorization_unavailable(
        _: Request, __: AuthorizationDataUnavailable
    ) -> JSONResponse:
        return error_response(
            503,
            "AUTHORIZATION_UNAVAILABLE",
            "Authorization could not be safely evaluated.",
            request_id_context.get(),
        )

    @app.exception_handler(MembershipStorageUnavailable)
    async def handle_membership_storage_unavailable(
        _: Request, __: MembershipStorageUnavailable
    ) -> JSONResponse:
        return error_response(
            503,
            "MEMBERSHIP_STORAGE_UNAVAILABLE",
            "Membership data is temporarily unavailable.",
            request_id_context.get(),
        )

    @app.exception_handler(AuditStorageUnavailable)
    async def handle_audit_storage_unavailable(
        _: Request, exc: AuditStorageUnavailable
    ) -> JSONResponse:
        logger.warning(
            "audit_storage_unavailable",
            extra={"exception_type": type(exc).__name__},
        )
        return error_response(
            503,
            "AUDIT_STORAGE_UNAVAILABLE",
            "Audit records are temporarily unavailable.",
            request_id_context.get(),
        )

    @app.exception_handler(PlatformIdentityNotConfigured)
    @app.exception_handler(PlatformIdentityUnavailable)
    @app.exception_handler(PlatformRequestAuthenticationUnavailable)
    @app.exception_handler(PlatformAuthorizationUnavailable)
    async def handle_platform_authentication_unavailable(
        _: Request, __: Exception
    ) -> JSONResponse:
        return error_response(
            503,
            "PLATFORM_AUTHENTICATION_UNAVAILABLE",
            "Platform authentication or authorization is temporarily unavailable.",
            request_id_context.get(),
        )

    @app.exception_handler(InvalidPlatformIdentity)
    async def handle_invalid_platform_identity(
        _: Request, __: InvalidPlatformIdentity
    ) -> JSONResponse:
        return error_response(
            401,
            "PLATFORM_AUTHENTICATION_FAILED",
            "Platform authentication could not be completed.",
            request_id_context.get(),
        )

    @app.exception_handler(PlatformOperatorNotAuthorized)
    async def handle_platform_permission_denied(
        _: Request, __: PlatformOperatorNotAuthorized
    ) -> JSONResponse:
        return error_response(
            403,
            "PLATFORM_PERMISSION_DENIED",
            "You are not authorized to perform this platform action.",
            request_id_context.get(),
        )

    @app.exception_handler(PlatformRequestForgery)
    async def handle_platform_request_forgery(
        _: Request, __: PlatformRequestForgery
    ) -> JSONResponse:
        return error_response(
            403,
            "CSRF_VALIDATION_FAILED",
            "The request could not be validated.",
            request_id_context.get(),
            {"Cache-Control": "no-store"},
        )

    @app.exception_handler(OnboardingError)
    async def handle_onboarding_error(_: Request, exc: OnboardingError) -> JSONResponse:
        headers = (
            {"Retry-After": str(exc.retry_after_seconds)}
            if exc.retry_after_seconds is not None
            else None
        )
        blockers = getattr(exc, "blockers", None)
        if blockers is not None:
            return JSONResponse(
                status_code=exc.status_code,
                headers=headers,
                content={
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "requestId": request_id_context.get(),
                        "blockers": list(blockers),
                    }
                },
            )
        return error_response(
            exc.status_code,
            exc.code,
            exc.message,
            request_id_context.get(),
            headers,
        )

    @app.exception_handler(MessagingError)
    async def handle_messaging_error(_: Request, __: MessagingError) -> JSONResponse:
        return error_response(
            503,
            "MESSAGING_UNAVAILABLE",
            "The messaging service is temporarily unavailable.",
            request_id_context.get(),
        )

    @app.exception_handler(OtpRateLimited)
    async def handle_otp_rate_limit(_: Request, exc: OtpRateLimited) -> JSONResponse:
        return error_response(
            429,
            "OTP_RATE_LIMITED",
            "Too many authentication attempts. Try again later.",
            request_id_context.get(),
            {"Retry-After": str(exc.retry_after_seconds)},
        )

    @app.exception_handler(AbuseControlUnavailable)
    @app.exception_handler(AuthenticationStorageUnavailable)
    @app.exception_handler(AuthenticationConfigurationUnavailable)
    async def handle_authentication_unavailable(_: Request, __: Exception) -> JSONResponse:
        return error_response(
            503,
            "AUTHENTICATION_UNAVAILABLE",
            "Authentication is temporarily unavailable.",
            request_id_context.get(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 500:
            return internal_server_error_response(request_id_context.get())
        code = "HTTP_ERROR"
        message = _SAFE_HTTP_MESSAGES.get(
            exc.status_code,
            "The request could not be completed.",
        )
        if isinstance(exc.detail, dict):
            if "message" in exc.detail:
                message = str(exc.detail["message"])
            elif "detail" in exc.detail:
                message = str(exc.detail["detail"])
        elif isinstance(exc.detail, str) and exc.detail:
            message = exc.detail
        req_id = request_id_context.get()
        logger.warning(
            "http_exception: status_code=%s code=%s message=%s request_id=%s",
            exc.status_code,
            code,
            message,
            req_id,
        )
        return _error_response(exc.status_code, code, message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        # Do not echo request values; they may contain personal data or secrets.
        return _error_response(422, "VALIDATION_ERROR", "Request validation failed.")

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_request_error",
            extra={"exception_type": type(exc).__name__},
        )
        return internal_server_error_response(request_id_context.get())
