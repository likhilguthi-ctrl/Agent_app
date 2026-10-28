from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.exceptions import install_exception_handlers
from app.core.config import Settings
from app.api.v1.router import api_v1_router
from app.core.tenant_context import TenantContext
from app.db.base import Base
from app.modules.authentication.api import (
    get_authenticated_session,
    get_authentication_repository,
)
from app.modules.audit.models import AuditEvent
from app.modules.audit.errors import AuditStorageUnavailable
from app.modules.audit.repository import (
    AuditEventSummary,
    SQLAlchemyAuditEventRepository,
    get_audit_event_repository,
)
from app.modules.authentication.repository import AuthenticatedSession
from app.modules.authorization.catalog import PERMISSION_DESCRIPTIONS, PermissionCode
from app.modules.authorization.dependencies import require_permission
from app.modules.authorization.errors import PermissionDenied, UnknownPermissionCode
from app.modules.authorization.models import Permission, Role, RolePermission, UserRoleAssignment
from app.modules.authorization.repository import (
    SQLAlchemyAuthorizationRepository,
    get_authorization_repository,
)
from app.modules.authorization.service import AuthorizationRequest, AuthorizationService
from app.modules.identity.models import TenantMembership, User
from app.modules.identity.errors import MembershipStorageUnavailable
from app.modules.identity.inventory import (
    SQLAlchemyMembershipInventoryRepository,
    get_membership_inventory_repository,
)
from app.modules.tenants.dependencies import get_tenant_context
from app.modules.tenants.models import Tenant


@pytest.fixture
def authz_harness():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    tenant_a, tenant_b = uuid4(), uuid4()
    user_a, user_b = uuid4(), uuid4()
    membership_a, membership_b = uuid4(), uuid4()
    with factory.begin() as session:
        session.add_all(
            [
                Tenant(id=tenant_a, slug="authz-a", status="active"),
                Tenant(id=tenant_b, slug="authz-b", status="active"),
                User(id=user_a, status="active"),
                User(id=user_b, status="active"),
            ]
        )
        session.flush()
        session.add_all(
            [
                TenantMembership(
                    id=membership_a,
                    tenant_id=tenant_a,
                    user_id=user_a,
                    mobile_e164="+14155550123",
                    status="active",
                ),
                TenantMembership(
                    id=membership_b,
                    tenant_id=tenant_b,
                    user_id=user_b,
                    mobile_e164="+14155550124",
                    status="active",
                ),
            ]
        )

    def identity(tenant_id=tenant_a, user_id=user_a, membership_id=membership_a):
        return AuthenticatedSession(
            session_id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            membership_id=membership_id,
            csrf_token="test-csrf",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            user_status="active",
            membership_status="active",
            tenant_status="active",
        )

    def tenant_context(tenant_id=tenant_a, slug="authz-a", host="a.example.test"):
        return TenantContext(tenant_id, slug, host, uuid4())

    def create_role(
        *,
        tenant_id=tenant_a,
        membership_id=membership_a,
        codes=(),
        delegable=(),
        assignment_status="active",
        role_status="active",
        permission_status="active",
        valid_from=None,
        valid_until=None,
        assigned_by_membership_id=None,
    ):
        role_id = uuid4()
        now = datetime.now(UTC)
        with factory.begin() as session:
            session.add(
                Role(
                    id=role_id,
                    tenant_id=tenant_id,
                    code=f"test_{str(role_id)[:8]}",
                    display_name="Test role",
                    status=role_status,
                )
            )
            session.flush()
            for code in codes:
                code = PermissionCode(code)
                if session.get(Permission, code.value) is None:
                    session.add(Permission(code=code.value, description=PERMISSION_DESCRIPTIONS[code]))
                session.add(
                    RolePermission(
                        tenant_id=tenant_id,
                        role_id=role_id,
                        permission_code=code.value,
                        status=permission_status,
                        is_delegable=code in delegable,
                        revoked_at=now if permission_status == "revoked" else None,
                    )
                )
            if membership_id is not None:
                session.add(
                    UserRoleAssignment(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        membership_id=membership_id,
                        role_id=role_id,
                        assigned_by_membership_id=assigned_by_membership_id or membership_id,
                        status=assignment_status,
                        valid_from=valid_from or now - timedelta(minutes=1),
                        valid_until=valid_until,
                        revoked_at=now if assignment_status == "revoked" else None,
                    )
                )
        return role_id

    repository = SQLAlchemyAuthorizationRepository(factory)
    try:
        yield {
            "engine": engine,
            "factory": factory,
            "repository": repository,
            "service": AuthorizationService(repository),
            "tenant_a": tenant_a,
            "tenant_b": tenant_b,
            "user_a": user_a,
            "user_b": user_b,
            "membership_a": membership_a,
            "membership_b": membership_b,
            "identity": identity,
            "tenant_context": tenant_context,
            "create_role": create_role,
        }
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def require_read(harness) -> AuthorizationRequest:
    return harness["service"].require(
        tenant=harness["tenant_context"](),
        authenticated=harness["identity"](),
        permission=PermissionCode.AGENT_READ,
    )


def test_registered_active_permission_is_granted(authz_harness) -> None:
    authz_harness["create_role"](codes=[PermissionCode.AGENT_READ])

    result = require_read(authz_harness)

    assert result.permission is PermissionCode.AGENT_READ
    assert result.principal.tenant_id == authz_harness["tenant_a"]
    assert result.active_permissions == frozenset({PermissionCode.AGENT_READ})


@pytest.mark.parametrize("assignment_status", ["inactive", "revoked"])
def test_inactive_or_revoked_assignment_does_not_grant(authz_harness, assignment_status) -> None:
    authz_harness["create_role"](
        codes=[PermissionCode.AGENT_READ], assignment_status=assignment_status
    )

    with pytest.raises(PermissionDenied):
        require_read(authz_harness)


def test_missing_assignment_and_inactive_role_or_permission_deny(authz_harness) -> None:
    authz_harness["create_role"](
        membership_id=None,
        codes=[PermissionCode.AGENT_READ],
    )
    authz_harness["create_role"](
        codes=[PermissionCode.AGENT_READ], role_status="inactive"
    )
    authz_harness["create_role"](
        codes=[PermissionCode.AGENT_READ], permission_status="revoked"
    )

    with pytest.raises(PermissionDenied):
        require_read(authz_harness)


def test_expired_assignment_does_not_grant(authz_harness) -> None:
    now = datetime.now(UTC)
    authz_harness["create_role"](
        codes=[PermissionCode.AGENT_READ],
        valid_from=now - timedelta(hours=2),
        valid_until=now - timedelta(hours=1),
    )

    with pytest.raises(PermissionDenied):
        require_read(authz_harness)


def test_inactive_membership_and_disabled_user_cannot_authorize(authz_harness) -> None:
    authz_harness["create_role"](codes=[PermissionCode.AGENT_READ])
    with authz_harness["factory"].begin() as session:
        session.get(TenantMembership, authz_harness["membership_a"]).status = "inactive"

    with pytest.raises(PermissionDenied):
        require_read(authz_harness)

    with authz_harness["factory"].begin() as session:
        session.get(TenantMembership, authz_harness["membership_a"]).status = "active"
        session.get(User, authz_harness["user_a"]).status = "inactive"
    with pytest.raises(PermissionDenied):
        require_read(authz_harness)


def test_session_or_role_from_another_tenant_is_not_accepted(authz_harness) -> None:
    authz_harness["create_role"](
        tenant_id=authz_harness["tenant_b"],
        membership_id=authz_harness["membership_b"],
        codes=[PermissionCode.AGENT_READ],
    )

    with pytest.raises(PermissionDenied):
        require_read(authz_harness)
    with pytest.raises(PermissionDenied):
        authz_harness["service"].require(
            tenant=authz_harness["tenant_context"](),
            authenticated=authz_harness["identity"](
                tenant_id=authz_harness["tenant_b"],
                user_id=authz_harness["user_b"],
                membership_id=authz_harness["membership_b"],
            ),
            permission=PermissionCode.AGENT_READ,
        )


def test_inactive_tenant_denies_even_with_assignment(authz_harness) -> None:
    authz_harness["create_role"](codes=[PermissionCode.AGENT_READ])
    with authz_harness["factory"].begin() as session:
        session.get(Tenant, authz_harness["tenant_a"]).status = "inactive"

    with pytest.raises(PermissionDenied):
        require_read(authz_harness)


def test_unknown_permission_code_fails_closed(authz_harness) -> None:
    with pytest.raises(UnknownPermissionCode):
        authz_harness["service"].require(
            tenant=authz_harness["tenant_context"](),
            authenticated=authz_harness["identity"](),
            permission="platform.root",
        )


def test_resource_constraints_compose_with_rbac(authz_harness) -> None:
    authz_harness["create_role"](codes=[PermissionCode.AGENT_READ])

    class DenyResource:
        def allows(self, _request):
            return False

    with pytest.raises(PermissionDenied):
        authz_harness["service"].require(
            tenant=authz_harness["tenant_context"](),
            authenticated=authz_harness["identity"](),
            permission=PermissionCode.AGENT_READ,
            resource={"region_id": "untrusted-data-is-not-a-grant"},
            constraints=[DenyResource()],
        )


def test_tenant_id_and_role_request_parameters_cannot_select_another_grant(authz_harness) -> None:
    """The authorization dependency consumes only resolver/session dependencies."""
    authz_harness["create_role"](
        tenant_id=authz_harness["tenant_b"],
        membership_id=authz_harness["membership_b"],
        codes=[PermissionCode.AGENT_READ],
    )
    router = APIRouter()

    @router.get("/protected")
    async def protected(
        authorized: AuthorizationRequest = Depends(require_permission(PermissionCode.AGENT_READ)),
    ):
        return {"tenant_id": str(authorized.tenant.tenant_id)}

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = lambda: authz_harness["tenant_context"]()
    app.dependency_overrides[get_authenticated_session] = lambda: authz_harness["identity"]()
    app.dependency_overrides[get_authorization_repository] = lambda: authz_harness["repository"]

    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                f"/protected?tenant_id={authz_harness['tenant_b']}&role=platform_admin"
            )

    response = asyncio.run(call())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_assignment_delegation_requires_explicitly_delegable_permissions(authz_harness) -> None:
    actor_role_id = authz_harness["create_role"](
        codes=[PermissionCode.ROLE_ASSIGN, PermissionCode.AGENT_READ]
    )
    target_role_id = authz_harness["create_role"](
        membership_id=None,
        codes=[PermissionCode.AGENT_READ],
    )

    with pytest.raises(PermissionDenied):
        authz_harness["service"].authorize_role_assignment(
            tenant=authz_harness["tenant_context"](),
            authenticated=authz_harness["identity"](),
        recipient_membership_id=authz_harness["membership_a"],
            role_id=target_role_id,
        )

    with authz_harness["factory"].begin() as session:
        link = session.get(
            RolePermission,
            (authz_harness["tenant_a"], actor_role_id, PermissionCode.AGENT_READ.value),
        )
        link.is_delegable = True

    authz_harness["service"].authorize_role_assignment(
        tenant=authz_harness["tenant_context"](),
        authenticated=authz_harness["identity"](),
        recipient_membership_id=authz_harness["membership_a"],
        role_id=target_role_id,
    )


def test_assignment_delegation_denies_ungranted_or_cross_tenant_privilege(authz_harness) -> None:
    authz_harness["create_role"](
        codes=[PermissionCode.ROLE_ASSIGN, PermissionCode.AGENT_READ],
        delegable=[PermissionCode.AGENT_READ],
    )
    escalated_role_id = authz_harness["create_role"](
        membership_id=None,
        codes=[PermissionCode.AGENT_READ, PermissionCode.AGENT_APPROVE],
    )
    other_tenant_role_id = authz_harness["create_role"](
        tenant_id=authz_harness["tenant_b"],
        membership_id=None,
        codes=[PermissionCode.AGENT_READ],
    )

    for target_role_id in [escalated_role_id, other_tenant_role_id]:
        with pytest.raises(PermissionDenied):
            authz_harness["service"].authorize_role_assignment(
                tenant=authz_harness["tenant_context"](),
                authenticated=authz_harness["identity"](),
                recipient_membership_id=authz_harness["membership_a"],
                role_id=target_role_id,
            )


def test_permission_assignment_requires_delegation_authority(authz_harness) -> None:
    authz_harness["create_role"](
        codes=[PermissionCode.ROLE_PERMISSION_ASSIGN, PermissionCode.AGENT_READ],
        delegable=[PermissionCode.AGENT_READ],
    )
    target_role_id = authz_harness["create_role"](membership_id=None)
    service = authz_harness["service"]
    service.authorize_permission_assignment(
        tenant=authz_harness["tenant_context"](),
        authenticated=authz_harness["identity"](),
        role_id=target_role_id,
        permission=PermissionCode.AGENT_READ,
    )
    with pytest.raises(PermissionDenied):
        service.authorize_permission_assignment(
            tenant=authz_harness["tenant_context"](),
            authenticated=authz_harness["identity"](),
            role_id=target_role_id,
            permission=PermissionCode.AGENT_APPROVE,
        )


def test_tenant_consistent_foreign_keys_and_single_active_assignment(authz_harness) -> None:
    role_a = authz_harness["create_role"](
        membership_id=None, codes=[PermissionCode.AGENT_READ]
    )
    role_b = authz_harness["create_role"](
        tenant_id=authz_harness["tenant_b"],
        membership_id=None,
        codes=[PermissionCode.AGENT_READ],
    )
    with authz_harness["factory"]() as session:
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=authz_harness["tenant_a"],
                membership_id=authz_harness["membership_a"],
                role_id=role_b,
                assigned_by_membership_id=authz_harness["membership_a"],
                status="active",
                valid_from=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    with authz_harness["factory"]() as session:
        session.add(
            RolePermission(
                tenant_id=authz_harness["tenant_a"],
                role_id=role_b,
                permission_code=PermissionCode.AGENT_READ.value,
                status="active",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    with authz_harness["factory"]() as session:
        session.add(
            RolePermission(
                tenant_id=authz_harness["tenant_a"],
                role_id=role_a,
                permission_code="agent.unregistered",
                status="active",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    with authz_harness["factory"]() as session:
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=authz_harness["tenant_a"],
                membership_id=authz_harness["membership_a"],
                role_id=role_a,
                assigned_by_membership_id=authz_harness["membership_a"],
                status="active",
                valid_from=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        session.commit()
        session.add(
            UserRoleAssignment(
                id=uuid4(),
                tenant_id=authz_harness["tenant_a"],
                membership_id=authz_harness["membership_a"],
                role_id=role_a,
                assigned_by_membership_id=authz_harness["membership_a"],
                status="active",
                valid_from=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_permission_catalog_and_migration_seed_match() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20261012_04_centralized_tenant_rbac.py"
    )
    spec = importlib.util.spec_from_file_location("phase5_rbac_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert dict(module.PERMISSION_SEED) == {
        code.value: description for code, description in PERMISSION_DESCRIPTIONS.items()
    }
    assert {code.value for code in PermissionCode} == set(PERMISSION_DESCRIPTIONS)


def test_model_permissions_have_no_role_or_grant_defaults() -> None:
    assert Role.__table__.c.status.server_default.arg == "inactive"
    assert UserRoleAssignment.__table__.c.status.server_default.arg == "inactive"
    assert RolePermission.__table__.c.status.server_default.arg == "inactive"
    assert RolePermission.__table__.c.is_delegable.server_default.arg.text == "false"
    assert list(Permission.__table__.primary_key.columns.keys()) == ["code"]


def _permission_catalog_app(authz_harness, *, authenticated=True):
    app = FastAPI()
    app.state.settings = Settings()
    install_exception_handlers(app)
    app.include_router(api_v1_router, prefix="/api/v1")
    app.dependency_overrides[get_tenant_context] = lambda: authz_harness["tenant_context"]()
    app.dependency_overrides[get_authorization_repository] = lambda: authz_harness["repository"]
    app.dependency_overrides[get_audit_event_repository] = lambda: (
        SQLAlchemyAuditEventRepository(authz_harness["factory"])
    )
    if authenticated:
        app.dependency_overrides[get_authenticated_session] = lambda: authz_harness["identity"]()
    else:
        app.dependency_overrides[get_authentication_repository] = lambda: object()
    return app


def _get_permission_catalog(app):
    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/api/v1/permissions")

    return asyncio.run(call())


def _get_memberships(app, *, limit=50, offset=0, tenant_id=None):
    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            query = f"limit={limit}&offset={offset}"
            if tenant_id is not None:
                query += f"&tenant_id={tenant_id}"
            return await client.get(f"/api/v1/memberships?{query}")

    return asyncio.run(call())


def _get_tenant_roles(app, *, limit=50, offset=0, tenant_id=None):
    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                f"/api/v1/roles?limit={limit}&offset={offset}"
                + (f"&tenant_id={tenant_id}" if tenant_id is not None else "")
            )

    return asyncio.run(call())


def _get_audit_events(app):
    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/api/v1/audit-events")

    return asyncio.run(call())


def test_permission_catalog_route_requires_permission_and_returns_registered_codes(
    authz_harness,
) -> None:
    authz_harness["create_role"](codes=[PermissionCode.PERMISSION_READ])
    app = _permission_catalog_app(authz_harness)

    response = _get_permission_catalog(app)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    entries = response.json()["permissions"]
    assert [entry["code"] for entry in entries] == sorted(
        code.value for code in PermissionCode
    )
    assert entries[0] == {
        "code": "agent.approve",
        "description": PERMISSION_DESCRIPTIONS[PermissionCode.AGENT_APPROVE],
    }


def test_permission_catalog_route_denies_missing_grant_and_unauthenticated_request(
    authz_harness,
) -> None:
    denied = _get_permission_catalog(_permission_catalog_app(authz_harness))
    unauthenticated = _get_permission_catalog(
        _permission_catalog_app(authz_harness, authenticated=False)
    )

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
    assert unauthenticated.status_code == 401


def test_role_list_route_requires_permission_and_returns_only_resolved_tenant_roles(
    authz_harness,
) -> None:
    tenant_role_id = authz_harness["create_role"](
        codes=[PermissionCode.ROLE_READ]
    )
    authz_harness["create_role"](
        tenant_id=authz_harness["tenant_b"],
        membership_id=authz_harness["membership_b"],
        codes=[PermissionCode.ROLE_READ],
    )
    app = _permission_catalog_app(authz_harness)

    response = _get_tenant_roles(
        app, limit=1, tenant_id=authz_harness["tenant_b"]
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "roles": [
            {
                "id": str(tenant_role_id),
                "code": f"test_{str(tenant_role_id)[:8]}",
                "display_name": "Test role",
                "status": "active",
                "permissions": ["role.read"],
            }
        ]
    }
    assert "membership_id" not in response.text
    assert str(authz_harness["tenant_b"]) not in response.text


def test_role_list_route_denies_missing_permission_and_unauthenticated_request(
    authz_harness,
) -> None:
    denied = _get_tenant_roles(_permission_catalog_app(authz_harness))
    unauthenticated = _get_tenant_roles(
        _permission_catalog_app(authz_harness, authenticated=False)
    )

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
    assert unauthenticated.status_code == 401


def test_role_list_route_rejects_unbounded_pagination(authz_harness) -> None:
    authz_harness["create_role"](codes=[PermissionCode.ROLE_READ])

    response = _get_tenant_roles(
        _permission_catalog_app(authz_harness), limit=101
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_role_list_route_fails_closed_when_role_storage_is_unavailable(
    authz_harness,
) -> None:
    authz_harness["create_role"](codes=[PermissionCode.ROLE_READ])

    class UnavailableRoleRepository:
        def load_state(self, **_kwargs):
            return authz_harness["repository"].load_state(**_kwargs)

        def list_roles_for_tenant(self, **_kwargs):
            from app.modules.authorization.errors import AuthorizationDataUnavailable

            raise AuthorizationDataUnavailable

    app = _permission_catalog_app(authz_harness)
    app.dependency_overrides[get_authorization_repository] = (
        lambda: UnavailableRoleRepository()
    )

    response = _get_tenant_roles(app)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTHORIZATION_UNAVAILABLE"


def test_membership_list_route_requires_permission_and_returns_minimal_tenant_data(
    authz_harness,
) -> None:
    authz_harness["create_role"](codes=[PermissionCode.MEMBER_READ])
    second_user_id = uuid4()
    second_membership_id = uuid4()
    with authz_harness["factory"].begin() as session:
        session.add(User(id=second_user_id, status="inactive"))
        session.flush()
        session.add(
            TenantMembership(
                id=second_membership_id,
                tenant_id=authz_harness["tenant_a"],
                user_id=second_user_id,
                mobile_e164="+14155550125",
                status="inactive",
            )
        )

    app = _permission_catalog_app(authz_harness)
    app.dependency_overrides[get_membership_inventory_repository] = lambda: (
        SQLAlchemyMembershipInventoryRepository(authz_harness["factory"])
    )
    response = _get_memberships(app)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    memberships = response.json()["memberships"]
    assert {membership["id"] for membership in memberships} == {
        str(authz_harness["membership_a"]),
        str(second_membership_id),
    }
    inactive = next(
        membership for membership in memberships if membership["id"] == str(second_membership_id)
    )
    assert inactive["status"] == "inactive"
    assert inactive["user_status"] == "inactive"
    assert "mobile_e164" not in response.text
    assert "14155550125" not in response.text
    assert "tenant_id" not in response.text
    assert "csrf_token" not in response.text
    first_page = _get_memberships(app, limit=1, offset=0)
    second_page = _get_memberships(app, limit=1, offset=1)
    assert first_page.status_code == second_page.status_code == 200
    assert len(first_page.json()["memberships"]) == 1
    assert len(second_page.json()["memberships"]) == 1
    assert (
        first_page.json()["memberships"][0]["id"]
        != second_page.json()["memberships"][0]["id"]
    )


def test_membership_list_route_denies_missing_permission_and_unauthenticated_request(
    authz_harness,
) -> None:
    denied = _get_memberships(_permission_catalog_app(authz_harness))
    unauthenticated = _get_memberships(
        _permission_catalog_app(authz_harness, authenticated=False)
    )

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
    assert unauthenticated.status_code == 401


def test_membership_list_route_ignores_client_tenant_and_bounds_pagination(
    authz_harness,
) -> None:
    authz_harness["create_role"](codes=[PermissionCode.MEMBER_READ])
    app = _permission_catalog_app(authz_harness)
    app.dependency_overrides[get_membership_inventory_repository] = lambda: (
        SQLAlchemyMembershipInventoryRepository(authz_harness["factory"])
    )

    cross_tenant_attempt = _get_memberships(
        app, tenant_id=authz_harness["tenant_b"]
    )
    unbounded = _get_memberships(app, limit=101)

    assert cross_tenant_attempt.status_code == 200
    assert {
        membership["id"]
        for membership in cross_tenant_attempt.json()["memberships"]
    } == {str(authz_harness["membership_a"])}
    assert unbounded.status_code == 422
    assert unbounded.json()["error"]["code"] == "VALIDATION_ERROR"


def test_membership_list_route_fails_closed_when_storage_is_unavailable(
    authz_harness,
) -> None:
    authz_harness["create_role"](codes=[PermissionCode.MEMBER_READ])

    class UnavailableMembershipRepository:
        def list_for_tenant(self, **_kwargs):
            raise MembershipStorageUnavailable

    app = _permission_catalog_app(authz_harness)
    app.dependency_overrides[get_membership_inventory_repository] = (
        lambda: UnavailableMembershipRepository()
    )

    response = _get_memberships(app)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MEMBERSHIP_STORAGE_UNAVAILABLE"


def test_role_assignment_and_revocation_routes_remain_unavailable(authz_harness) -> None:
    app = _permission_catalog_app(authz_harness)

    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assignment = await client.post(
                f"/api/v1/memberships/{authz_harness['membership_a']}/roles",
                json={"role_id": str(uuid4())},
            )
            revocation = await client.delete(
                f"/api/v1/memberships/{authz_harness['membership_a']}/roles/{uuid4()}"
            )
            return assignment, revocation

    assignment, revocation = asyncio.run(call())

    assert assignment.status_code == 404
    assert revocation.status_code == 404


def test_permission_catalog_route_does_not_accept_another_tenants_grant(authz_harness) -> None:
    authz_harness["create_role"](
        tenant_id=authz_harness["tenant_b"],
        membership_id=authz_harness["membership_b"],
        codes=[PermissionCode.PERMISSION_READ],
    )
    app = _permission_catalog_app(authz_harness)

    response = _get_permission_catalog(app)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_audit_read_requires_permission_and_is_tenant_scoped(authz_harness) -> None:
    authz_harness["create_role"](codes=[PermissionCode.AUDIT_READ])
    event_ids = {}
    for tenant_id, user_id in (
        (authz_harness["tenant_a"], authz_harness["user_a"]),
        (authz_harness["tenant_b"], authz_harness["user_b"]),
    ):
        event_id = uuid4()
        event_ids[tenant_id] = event_id
        with authz_harness["factory"].begin() as session:
            session.add(
                AuditEvent(
                    id=event_id,
                    event_type="agent.appointment",
                    occurred_at=datetime.now(UTC),
                    actor_category="tenant_member",
                    actor_ref=str(user_id),
                    tenant_id=tenant_id,
                    target_entity_type="agent_appointment",
                    target_entity_ref=str(uuid4()),
                    action="agent.create",
                    outcome="pending",
                    decision_metadata={"reason_code": "appointment.pending"},
                )
            )
    app = _permission_catalog_app(authz_harness)

    response = _get_audit_events(app)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["id"] == str(event_ids[authz_harness["tenant_a"]])
    assert events[0]["action"] == "agent.create"
    assert events[0]["target_entity_type"] == "agent_appointment"
    assert "actor_ref" not in events[0]
    assert "target_entity_ref" not in events[0]
    assert "decision_metadata" not in events[0]


def test_audit_read_denies_members_without_audit_permission(authz_harness) -> None:
    response = _get_audit_events(_permission_catalog_app(authz_harness))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_audit_read_fails_closed_when_storage_is_unavailable(authz_harness) -> None:
    authz_harness["create_role"](codes=[PermissionCode.AUDIT_READ])

    class UnavailableAuditRepository:
        def list_for_tenant(
            self, *, tenant_id: UUID, limit: int, offset: int
        ) -> list[AuditEventSummary]:
            _ = tenant_id, limit, offset
            raise AuditStorageUnavailable

    app = _permission_catalog_app(authz_harness)
    app.dependency_overrides[get_audit_event_repository] = (
        lambda: UnavailableAuditRepository()
    )

    response = _get_audit_events(app)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUDIT_STORAGE_UNAVAILABLE"
