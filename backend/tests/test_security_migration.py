from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine, event, inspect, text

from app.db.base import Base
from app.modules.authorization.models import Permission, Role, RolePermission, UserRoleAssignment  # noqa: F401
from app.modules.authentication.models import AuthSession, OtpChallenge, OtpRateBucket  # noqa: F401
from app.modules.identity.models import TenantMembership, User  # noqa: F401
from app.modules.onboarding.models import (  # noqa: F401
    DomainVerificationChallenge,
    TenantActivationAttempt,
    TenantOnboardingReview,
    TenantOnboardingRequest,
)
from app.modules.platform_identity.models import (  # noqa: F401
    PlatformOperator,
    PlatformOperatorSession,
    PlatformOIDCLoginTransaction,
    PlatformOperatorRoleAssignment,
    PlatformPermission,
    PlatformRole,
    PlatformRolePermission,
)
from app.modules.audit.models import AuditEvent  # noqa: F401
from app.modules.outbox.models import (  # noqa: F401
    AuditOutboxAttempt,
    AuditOutboxEvent,
    AuditOutboxWorkerHeartbeat,
)
from app.modules.tenants.models import Tenant, TenantDomain  # noqa: F401


MIGRATION_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
REVISION_FILES = (
    "20261011_01_create_tenant_registry.py",
    "20261012_02_create_community_identities.py",
    "20261012_03_auth_sessions_and_otp_controls.py",
    "20261012_04_centralized_tenant_rbac.py",
    "20261012_05_platform_security_foundations.py",
    "20261012_06_tenant_onboarding.py",
    "20261012_07_independent_activation_review.py",
    "20261012_08_activation_outcome_ack_resolution_gate.py",
    "20261013_09_platform_oidc_login_sessions.py",
    "20261015_10_account_credentials.py",
    "20261016_11_phase5_agent_user_storage_commission.py",
    "20261016_12_mobile_otp_and_registration_verification.py",
    "20261017_13_support_tickets.py",
    "20261017_14_agent_appointments_and_approvals.py",
    "20261017_15_approval_decisions_unique_appointment.py",
    "20261018_16_member_relationships.py",
    "20261021_17_financial_foundation.py",
    "20261021_18_subscription_and_package_billing.py",
    "20261021_19_razorpay_payments_and_lifecycle.py",
    "20261021_20_agent_payout_governance_and_workflow.py",
    "20261022_21_notification_status_webhooks.py",
    "20261023_22_account_credentials_must_change_password.py",
    "20261024_23_create_super_admin_notifications.py",
    "20261024_24_add_region_state_and_canonical_locations.py",
    "20261024_25_deduplicate_otp_challenges_and_add_unique_constraint.py",
    "20261024_26_target_management_schema.py",
    "20261024_27_add_dispatch_id_to_otp_challenges.py",
    "20261026_28_registration_workflow_upgrade.py",
    "20261026_29_subscriber_whatsapp_consent_and_payment.py",
    "20261027_30_member_subscriptions_notes_jsonb.py",
    "20261027_31_location_master_and_hierarchy.py",
    "20261027_32_remove_village_level_data.py",
    "20261028_33_community_specific_subscription_plans.py",
)


def load_revision(filename: str):
    path = MIGRATION_DIR / filename
    spec = importlib.util.spec_from_file_location(f"migration_{filename[:13]}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_revision_is_additive_single_head() -> None:
    revisions = [load_revision(filename) for filename in REVISION_FILES]
    assert revisions[4].revision == "20261012_05"
    assert revisions[4].down_revision == revisions[3].revision == "20261012_04"
    assert revisions[5].revision == "20261012_06"
    assert revisions[5].down_revision == revisions[4].revision
    assert revisions[6].revision == "20261012_07"
    assert revisions[6].down_revision == revisions[5].revision
    assert revisions[7].revision == "20261012_08"
    assert revisions[7].down_revision == revisions[6].revision
    assert revisions[8].revision == "20261013_09"
    assert revisions[8].down_revision == revisions[7].revision
    assert revisions[9].revision == "20261015_10"
    assert revisions[9].down_revision == revisions[8].revision
    assert revisions[10].revision == "20261016_11"
    assert revisions[10].down_revision == revisions[9].revision
    assert revisions[11].revision == "20261016_12"
    assert revisions[11].down_revision == revisions[10].revision
    assert revisions[12].revision == "20261017_13"
    assert revisions[12].down_revision == revisions[11].revision
    assert revisions[13].revision == "20261017_14"
    assert revisions[13].down_revision == revisions[12].revision
    assert revisions[14].revision == "20261017_15"
    assert revisions[14].down_revision == revisions[13].revision
    assert revisions[15].revision == "20261018_16"
    assert revisions[15].down_revision == revisions[14].revision
    assert revisions[16].revision == "20261021_17"
    assert revisions[16].down_revision == revisions[15].revision
    assert revisions[17].revision == "20261021_18"
    assert revisions[17].down_revision == revisions[16].revision
    assert revisions[18].revision == "20261021_19"
    assert revisions[18].down_revision == revisions[17].revision
    assert revisions[19].revision == "20261021_20"
    assert revisions[19].down_revision == revisions[18].revision
    assert revisions[20].revision == "20261022_21"
    assert revisions[20].down_revision == revisions[19].revision
    assert revisions[21].revision == "20261023_22"
    assert revisions[21].down_revision == revisions[20].revision
    assert revisions[22].revision == "20261024_23"
    assert revisions[22].down_revision == revisions[21].revision
    assert revisions[23].revision == "20261024_24"
    assert revisions[23].down_revision == revisions[22].revision
    assert revisions[24].revision == "20261024_25"
    assert revisions[24].down_revision == revisions[23].revision
    assert revisions[25].revision == "20261024_26"
    assert revisions[25].down_revision == revisions[24].revision
    assert revisions[26].revision == "20261024_27"
    assert revisions[26].down_revision == revisions[25].revision
    assert revisions[27].revision == "20261026_28"
    assert revisions[27].down_revision == revisions[26].revision
    assert revisions[28].revision == "20261026_29"
    assert revisions[28].down_revision == revisions[27].revision
    assert revisions[29].revision == "20261027_30"
    assert revisions[29].down_revision == revisions[28].revision
    assert revisions[30].revision == "20261027_31"
    assert revisions[30].down_revision == revisions[29].revision
    assert revisions[31].revision == "20261027_32"
    assert revisions[31].down_revision == revisions[30].revision
    assert revisions[32].revision == "20261028_33"
    assert revisions[32].down_revision == revisions[31].revision
    assert sum(1 for filename in MIGRATION_DIR.glob("*.py") if "20261012_08" in filename.name) == 1
    config = Config(str(MIGRATION_DIR.parent.parent / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATION_DIR.parent))
    assert ScriptDirectory.from_config(config).get_heads() == ["20261028_33"]


def test_additive_migration_applies_to_isolated_sqlite_and_seeds_no_grants() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    parent_table_names = (
        "tenants",
        "tenant_domains",
        "users",
        "tenant_memberships",
        "otp_challenges",
        "otp_rate_buckets",
        "auth_sessions",
        "permissions",
        "roles",
        "role_permissions",
        "user_role_assignments",
    )
    parent_metadata = MetaData()
    for table_name in parent_table_names:
        Base.metadata.tables[table_name].to_metadata(parent_metadata)
    parent_metadata.create_all(engine)

    revision = load_revision("20261012_05_platform_security_foundations.py")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            revision.upgrade()

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "platform_operators",
        "platform_permissions",
        "platform_roles",
        "platform_role_permissions",
        "platform_operator_role_assignments",
        "audit_events",
        "audit_outbox_events",
        "audit_outbox_attempts",
        "audit_outbox_worker_heartbeats",
    } <= tables
    assert "uq_platform_operators_issuer_subject" in {
        item["name"] for item in inspector.get_unique_constraints("platform_operators")
    }
    assert "ix_audit_events_tenant_recorded" in {
        item["name"] for item in inspector.get_indexes("audit_events")
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM platform_permissions")) > 0
        assert connection.scalar(text("SELECT count(*) FROM platform_operators")) == 0
        assert connection.scalar(text("SELECT count(*) FROM platform_roles")) == 0
        assert connection.scalar(text("SELECT count(*) FROM platform_operator_role_assignments")) == 0
    engine.dispose()


def test_onboarding_migration_is_additive_and_seeds_no_operator_grants() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    parent_table_names = (
        "tenants",
        "tenant_domains",
        "users",
        "tenant_memberships",
        "otp_challenges",
        "otp_rate_buckets",
        "auth_sessions",
        "permissions",
        "roles",
        "role_permissions",
        "user_role_assignments",
    )
    parent_metadata = MetaData()
    for table_name in parent_table_names:
        Base.metadata.tables[table_name].to_metadata(parent_metadata)
    parent_metadata.create_all(engine)

    revision_05 = load_revision("20261012_05_platform_security_foundations.py")
    revision_06 = load_revision("20261012_06_tenant_onboarding.py")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            revision_05.upgrade()
            revision_06.upgrade()

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"tenant_onboarding_requests", "domain_verification_challenges"} <= tables
    assert "uq_tenant_onboarding_requests_pending_domain" in {
        item["name"] for item in inspector.get_indexes("tenant_onboarding_requests")
    }
    assert "uq_domain_verification_challenges_current" in {
        item["name"] for item in inspector.get_indexes("domain_verification_challenges")
    }
    assert "ix_audit_events_actor_type_occurred" in {
        item["name"] for item in inspector.get_indexes("audit_events")
    }
    with engine.connect() as connection:
        permissions = set(
            connection.scalars(
                text("SELECT code FROM platform_permissions WHERE code LIKE 'platform.%'")
            )
        )
        assert "platform.domain.challenge" in permissions
        assert "platform.onboarding.cancel" in permissions
        assert connection.scalar(text("SELECT count(*) FROM platform_operators")) == 0
        assert connection.scalar(text("SELECT count(*) FROM platform_roles")) == 0
        assert connection.scalar(text("SELECT count(*) FROM platform_operator_role_assignments")) == 0
    engine.dispose()


def test_independent_activation_review_migration_is_additive_on_isolated_sqlite() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    parent_table_names = (
        "tenants",
        "tenant_domains",
        "users",
        "tenant_memberships",
        "otp_challenges",
        "otp_rate_buckets",
        "auth_sessions",
        "permissions",
        "roles",
        "role_permissions",
        "user_role_assignments",
    )
    parent_metadata = MetaData()
    for table_name in parent_table_names:
        Base.metadata.tables[table_name].to_metadata(parent_metadata)
    parent_metadata.create_all(engine)

    revisions = [
        load_revision("20261012_05_platform_security_foundations.py"),
        load_revision("20261012_06_tenant_onboarding.py"),
        load_revision("20261012_07_independent_activation_review.py"),
        load_revision("20261012_08_activation_outcome_ack_resolution_gate.py"),
        load_revision("20261013_09_platform_oidc_login_sessions.py"),
        load_revision("20261015_10_account_credentials.py"),
    ]
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for revision in revisions:
                revision.upgrade()

    inspector = inspect(engine)
    assert {"tenant_onboarding_reviews", "tenant_activation_attempts", "account_credentials"} <= set(
        inspector.get_table_names()
    )
    assert "uq_tenant_onboarding_reviews_request" in {
        item["name"] for item in inspector.get_unique_constraints("tenant_onboarding_reviews")
    }
    assert "uq_tenant_activation_attempts_pending" in {
        item["name"] for item in inspector.get_indexes("tenant_activation_attempts")
    }
    assert "completion_audit_event_id" in {
        column["name"] for column in inspector.get_columns("tenant_activation_attempts")
    }
    assert "uq_tenant_activation_attempts_completion_event" in {
        item["name"]
        for item in inspector.get_unique_constraints("tenant_activation_attempts")
    }
    assert {"platform_oidc_login_transactions", "platform_operator_sessions"} <= set(
        inspector.get_table_names()
    )
    request_checks = {
        item["name"]: item["sqltext"]
        for item in inspector.get_check_constraints("tenant_onboarding_requests")
    }
    assert "'ACTIVE'" in request_checks["ck_tenant_onboarding_requests_state"]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tenant_onboarding_reviews")) == 0
        assert connection.scalar(text("SELECT count(*) FROM tenant_activation_attempts")) == 0
        assert connection.scalar(text("SELECT count(*) FROM platform_operators")) == 0
        assert connection.scalar(text("SELECT count(*) FROM platform_operator_role_assignments")) == 0
    engine.dispose()


def test_approval_decisions_unique_constraint_migration_is_additive_and_reversible(tmp_path: Path) -> None:
    db_path = tmp_path / "migration-test-15.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    rev_15 = load_revision("20261017_15_approval_decisions_unique_appointment.py")

    # Create table for testing migration 15
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            import sqlalchemy as sa
            from alembic import op
            op.create_table(
                "approval_decisions",
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("appointment_id", sa.Uuid(), nullable=False),
                sa.Column("decision", sa.String(), nullable=False),
            )

    # Now upgrade rev 15
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            rev_15.upgrade()

    inspector = inspect(engine)
    uqs = {item["name"] for item in inspector.get_unique_constraints("approval_decisions")}
    assert "uq_approval_decisions_appointment_id" in uqs

    # Test downgrade
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            rev_15.downgrade()

    inspector_down = inspect(engine)
    uqs_down = {item["name"] for item in inspector_down.get_unique_constraints("approval_decisions")}
    assert "uq_approval_decisions_appointment_id" not in uqs_down

    engine.dispose()

