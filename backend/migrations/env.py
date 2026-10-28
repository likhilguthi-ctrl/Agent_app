from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.db.base import Base
from app.modules.authorization.models import Permission, Role, RolePermission, UserRoleAssignment  # noqa: F401
from app.modules.authentication.models import AccountCredential, AuthSession, OtpChallenge, OtpRateBucket  # noqa: F401
from app.modules.identity.models import TenantMembership, User  # noqa: F401
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
from app.modules.onboarding.models import (  # noqa: F401
    DomainVerificationChallenge,
    TenantActivationAttempt,
    TenantOnboardingReview,
    TenantOnboardingRequest,
)
from app.modules.outbox.models import (  # noqa: F401
    AuditOutboxAttempt,
    AuditOutboxEvent,
    AuditOutboxWorkerHeartbeat,
)
from app.modules.tenants.models import CommissionAuditLog, Tenant, TenantDomain  # noqa: F401
from app.modules.agents.models import AgentProfile, SubscriberProfile  # noqa: F401
from app.modules.agent_appointments.models import AgentAppointment  # noqa: F401
from app.modules.approval_workflows.models import ApprovalDecision  # noqa: F401
from app.modules.registration_photos.models import StoredObject  # noqa: F401
from app.modules.relationships.models import MemberRelationship  # noqa: F401
from app.modules.agent_lifecycle.models import AgentLifecycleRequest  # noqa: F401
from app.modules.commissions.models import (  # noqa: F401
    CommissionAccount,
    CommissionJournal,
    CommissionLedgerEntry,
)
from app.modules.subscriptions.models import (  # noqa: F401
    MemberSubscription,
    SubscriptionPlan,
)
from app.modules.payments.models import (  # noqa: F401
    PaymentOrder,
    PaymentTransaction,
    PaymentWebhookEvent,
)
from app.modules.payouts.models import PayoutRequest  # noqa: F401
from app.modules.messaging.models import (  # noqa: F401
    NotificationMessage,
    NotificationWebhookEvent,
)
from app.modules.targets.models import (  # noqa: F401
    TargetAchievementAdjustment,
    TargetAssignment,
    TargetType,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
if settings.database_url is None:
    raise RuntimeError("DATABASE_URL must be set before running Alembic migrations.")

database_url = settings.database_url.get_secret_value()
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

