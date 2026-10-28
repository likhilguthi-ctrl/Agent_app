from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.core.config import Settings
from app.modules.onboarding import activation as activation_module
from app.modules.onboarding.activation import TenantActivationService
from app.modules.platform_identity.service import PlatformPrincipal


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def begin(self):
        return _Transaction()

    def flush(self):
        pass


class _Authorization:
    def authorize_in_transaction(self, *_args, **_kwargs):
        return True


def test_activation_links_its_completion_event_before_returning(monkeypatch) -> None:
    """Exercise the in-memory transaction path; this does not contact a database or sink."""
    now = datetime(2026, 10, 12, tzinfo=UTC)
    request_id, tenant_id, domain_id, attempt_id = (uuid4() for _ in range(4))
    completion_event_id = uuid4()
    onboarding = SimpleNamespace(
        id=request_id,
        tenant_id=tenant_id,
        tenant_domain_id=domain_id,
        state="PENDING_ACTIVATION_REVIEW",
        expires_at=now + timedelta(days=1),
        version=1,
        updated_at=now,
    )
    tenant = SimpleNamespace(id=tenant_id, status="inactive", updated_at=now)
    domain = SimpleNamespace(
        id=domain_id,
        tenant_id=tenant_id,
        hostname="community.example",
        is_active=False,
        updated_at=now,
    )
    attempt = SimpleNamespace(
        id=attempt_id,
        status="PENDING_AUDIT_ACK",
        completed_at=None,
        completion_audit_event_id=None,
    )

    class _Repository:
        def __init__(self, _session):
            pass

        def get_request(self, _request_id, *, lock):
            assert lock
            return onboarding

        def get_activation_attempt(self, _request_id, _attempt_id, *, lock):
            assert lock
            return attempt

        def get_review(self, _request_id, *, lock):
            assert lock
            return SimpleNamespace(decision="approve")

        def get_tenant(self, _tenant_id, *, lock):
            assert lock
            return tenant

        def get_domain(self, _domain_id, *, lock):
            assert lock
            return domain

    monkeypatch.setattr(activation_module, "TenantOnboardingRepository", _Repository)
    service = TenantActivationService(
        Settings(_env_file=None, environment="test"),
        session_factory=_Session,
        authorization=_Authorization(),
        audit_service=object(),
        clock=lambda: now,
    )

    async def no_external_health_blockers(_principal):
        return []

    # Isolate the completion-event linkage from the separate, fail-closed gate tests.
    monkeypatch.setattr(service._policy, "external_blockers", no_external_health_blockers)
    monkeypatch.setattr(service._policy, "local_blockers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        service,
        "_record_event",
        lambda *_args, **_kwargs: SimpleNamespace(event_id=completion_event_id),
    )
    principal = PlatformPrincipal(
        operator_id=uuid4(),
        issuer="https://issuer.example.test",
        subject="operator-subject",
        authenticated_at=now,
        acr="mfa",
        amr=("pwd", "mfa"),
    )

    result = asyncio.run(
        service.activate(
            principal,
            request_id,
            attempt_id,
            request_id="activation-binding-test",
            now=now,
        )
    )

    assert result.tenant_active is True
    assert result.domain_active is True
    assert result.tenant_resolution_enabled is False
    assert attempt.completion_audit_event_id == completion_event_id
