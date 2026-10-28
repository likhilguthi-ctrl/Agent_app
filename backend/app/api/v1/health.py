from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response, status
from starlette.concurrency import run_in_threadpool

from app.db.session import check_database_connectivity
from app.modules.outbox.repository import SQLAlchemyOutboxRepository
from app.modules.platform_identity.dependencies import DatabasePlatformPrincipalResolver
from app.modules.platform_identity.repository import SQLAlchemyPlatformIdentityRepository
from app.db.session import get_session_factory

router = APIRouter()


@router.get("/health", summary="Versioned liveness check")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def readiness(request: Request, response: Response) -> dict[str, object]:
    settings = request.app.state.settings
    try:
        await run_in_threadpool(check_database_connectivity)
        database_verified = True
    except Exception:
        database_verified = False

    identity_provider = getattr(request.app.state, "platform_identity_provider", None)
    identity_configured = bool(settings.platform_oidc_configured)
    identity_verified = False
    if identity_configured:
        try:
            identity_verified = bool(await identity_provider.check_provider())
        except Exception:
            identity_verified = False

    principal_resolver = getattr(request.app.state, "platform_principal_resolver", None)
    operator_login_configured = bool(
        settings.platform_oidc_login_configured
        or (
            settings.environment == "test"
            and principal_resolver is not None
            and not isinstance(principal_resolver, DatabasePlatformPrincipalResolver)
        )
    )
    request_authentication_available = bool(
        identity_configured
        and identity_verified
        and principal_resolver is not None
        and operator_login_configured
    )

    onboarding_authority_configured = False
    if database_verified:
        try:
            def check_onboarding_authority() -> bool:
                with get_session_factory()() as session:
                    return SQLAlchemyPlatformIdentityRepository().has_onboarding_authority(
                        session, now=datetime.now(UTC)
                    )

            onboarding_authority_configured = await run_in_threadpool(check_onboarding_authority)
        except Exception:
            onboarding_authority_configured = False

    audit_sink = getattr(request.app.state, "audit_sink", None)
    sink_configured = bool(settings.audit_sink_configuration_complete)
    sink_verified = False
    if sink_configured and audit_sink is not None and audit_sink.supported:
        try:
            sink_verified = bool(await audit_sink.check_health())
        except Exception:
            sink_verified = False

    worker_ready = bool(
        settings.outbox_worker_enabled
        and settings.audit_sink_configuration_complete
        and audit_sink is not None
        and audit_sink.supported
        and sink_verified
    )
    if worker_ready:
        try:
            worker_ready = SQLAlchemyOutboxRepository().has_recent_verified_worker(
                now=datetime.now(UTC),
                ttl_seconds=settings.outbox_worker_heartbeat_ttl_seconds,
                adapter_name=settings.audit_sink_adapter or "unconfigured",
            )
        except Exception:
            worker_ready = False
    provisioning_eligible = bool(
        settings.environment in {"production", "staging"}
        and database_verified
        and identity_verified
        and request_authentication_available
        and onboarding_authority_configured
        and sink_verified
        and worker_ready
    )
    checks = {
        "api_process": "alive",
        "database_verified": database_verified,
        "operator_identity_configured": identity_configured,
        "operator_identity_verified": identity_verified,
        "operator_login_configured": operator_login_configured,
        "operator_request_authentication_available": request_authentication_available,
        "platform_onboarding_authority_configured": onboarding_authority_configured,
        "external_audit_delivery_configured": sink_configured,
        "external_audit_delivery_verified": sink_verified,
        "outbox_worker_ready": worker_ready,
        "privileged_provisioning_eligible": provisioning_eligible,
    }
    response.status_code = (
        status.HTTP_200_OK if provisioning_eligible else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {"status": "ready" if provisioning_eligible else "not_ready", "checks": checks}


@router.get("/health/ready", summary="Security and database readiness")
async def versioned_readiness(request: Request, response: Response) -> dict[str, object]:
    return await readiness(request, response)

