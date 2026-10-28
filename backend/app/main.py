from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_v1_router
from app.core.config import Settings, get_settings
from app.core.exceptions import install_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.oidc_callback_logging import OIDCCallbackQueryScrubbingMiddleware
from app.core.request_id import RequestIdMiddleware
from app.db.session import dispose_database_engine
from app.modules.messaging.contracts import MessagingContractCodec
from app.modules.messaging.factory import create_messaging_provider
from app.modules.messaging.provider import MessagingProvider
from app.modules.messaging.service import MessagingService
from app.modules.outbox.service import create_audit_sink
from app.modules.outbox.sink import AuditSink
from app.modules.platform_identity.provider import (
    OIDCPlatformIdentityProvider,
    PlatformIdentityProvider,
)
from app.modules.platform_identity.dependencies import (
    DatabasePlatformPrincipalResolver,
    PlatformPrincipalResolver,
)

logger = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    messaging_provider: MessagingProvider | None = None,
    messaging_codec: MessagingContractCodec | None = None,
    platform_identity_provider: PlatformIdentityProvider | None = None,
    platform_principal_resolver: PlatformPrincipalResolver | None = None,
    audit_sink: AuditSink | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    if app_settings.environment != "test" and (
        platform_identity_provider is not None or platform_principal_resolver is not None
    ):
        raise ValueError("Platform identity provider and principal resolver overrides are test-only.")
    configure_logging(app_settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("application_starting", extra={"environment": app_settings.environment})
        provider: MessagingProvider | None = None
        identity_provider: PlatformIdentityProvider | None = None
        configured_audit_sink: AuditSink | None = None
        try:
            provider = (
                messaging_provider
                if messaging_provider is not None
                else create_messaging_provider(app_settings, codec=messaging_codec)
            )
            app.state.messaging_service = MessagingService(provider)
            identity_provider = platform_identity_provider
            if identity_provider is None and app_settings.platform_oidc_configured:
                identity_provider = OIDCPlatformIdentityProvider(app_settings)
            configured_audit_sink = audit_sink or create_audit_sink(app_settings)
            app.state.platform_identity_provider = identity_provider
            app.state.audit_sink = configured_audit_sink
            yield
        finally:
            try:
                if provider is not None:
                    await provider.aclose()
            finally:
                try:
                    if identity_provider is not None:
                        await identity_provider.aclose()
                finally:
                    try:
                        if configured_audit_sink is not None:
                            await configured_audit_sink.aclose()
                    finally:
                        dispose_database_engine()
                        logger.info("application_stopping")

    app = FastAPI(
        title=app_settings.app_name,
        version="0.1.0",
        description=(
            "Foundational API for the planned shared, tenant-aware modular monolith. "
            "Tenant context is available to tenant-scoped routers. Messaging transport and "
            "tenant-bound community OTP authentication, and centralized tenant-aware RBAC "
            "evaluation are present. Authorized tenant members can read the permission "
            "catalog and minimal tenant audit summaries; role administration and Agent "
            "Platform business workflows are not implemented."
        ),
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.platform_identity_provider = None
    app.state.platform_principal_resolver = platform_principal_resolver or (
        DatabasePlatformPrincipalResolver() if app_settings.platform_oidc_login_configured else None
    )
    app.state.audit_sink = audit_sink or create_audit_sink(app_settings)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.frontend_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
        expose_headers=["X-Request-ID"],
    )
    # Added after CORS so request IDs also cover preflight and error responses.
    app.add_middleware(RequestIdMiddleware)
    # OIDC callbacks carry a one-use authorization code and state in the query.
    # Scrub them after route dispatch, before the ASGI server emits access logs.
    app.add_middleware(OIDCCallbackQueryScrubbingMiddleware)

    install_exception_handlers(app)

    @app.get("/health", tags=["health"], summary="Liveness check")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"], summary="Security and database readiness")
    async def ready(request: Request, response: Response) -> dict[str, object]:
        from app.api.v1.health import readiness

        return await readiness(request, response)

    app.include_router(api_v1_router, prefix=app_settings.api_v1_prefix)
    return app


app = create_app()
