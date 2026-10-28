from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.modules.authentication.api import router as authentication_router
from app.modules.audit.api import platform_activity_router, router as audit_router
from app.modules.authorization.api import permission_router, role_router
from app.modules.identity.api import router as membership_router
from app.modules.onboarding.api import router as onboarding_router
from app.modules.registration_photos.api import (
    router as registration_photos_router,
    storage_router,
)
from app.modules.platform_identity.api import router as platform_identity_router
from app.modules.tenants.api import (
    community_regions_router,
    platform_admin_router,
    router as platform_communities_router,
)
from app.modules.agents.api import router as agents_router
from app.modules.agent_appointments.api import router as agent_appointments_router
from app.modules.support.api import router as support_router, support_tickets_router
from app.modules.relationships.api import (
    agent_descendants_router,
    members_relationships_router,
)
from app.modules.agent_lifecycle.api import (
    agent_item_lifecycle_router,
    community_lifecycle_router,
    platform_lifecycle_router,
)
from app.modules.commissions.api import router as commissions_router
from app.modules.subscriptions.api import (
    platform_plans_router,
    tenant_plans_router,
    tenant_subscriptions_router,
)
from app.modules.payments.api import payments_router, payment_webhooks_router
from app.modules.payouts.api import (
    community_payouts_router,
    platform_payouts_router,
)
from app.modules.messaging.api import (
    notification_webhooks_router,
    notifications_router,
)
from app.modules.notifications.api import router as in_app_notifications_router
from app.modules.targets.api import router as targets_router
from app.modules.locations.api import router as locations_router
from app.modules.hierarchy.api import router as hierarchy_router
from app.modules.profiles.api import router as profiles_router

api_v1_router = APIRouter()
api_v1_router.include_router(health_router, tags=["health"])
api_v1_router.include_router(locations_router)
api_v1_router.include_router(hierarchy_router)
api_v1_router.include_router(authentication_router)
api_v1_router.include_router(audit_router)
api_v1_router.include_router(platform_activity_router)

api_v1_router.include_router(permission_router)
api_v1_router.include_router(role_router)
api_v1_router.include_router(membership_router)
api_v1_router.include_router(onboarding_router)
api_v1_router.include_router(registration_photos_router)
api_v1_router.include_router(storage_router)
api_v1_router.include_router(platform_identity_router)
api_v1_router.include_router(platform_communities_router)
api_v1_router.include_router(platform_admin_router)
api_v1_router.include_router(community_regions_router)
api_v1_router.include_router(agents_router)
api_v1_router.include_router(agent_appointments_router)
api_v1_router.include_router(support_router)
api_v1_router.include_router(support_tickets_router)
api_v1_router.include_router(members_relationships_router)
api_v1_router.include_router(agent_descendants_router)
api_v1_router.include_router(community_lifecycle_router)
api_v1_router.include_router(agent_item_lifecycle_router)
api_v1_router.include_router(platform_lifecycle_router)
api_v1_router.include_router(commissions_router)
api_v1_router.include_router(platform_plans_router)
api_v1_router.include_router(tenant_plans_router)
api_v1_router.include_router(tenant_subscriptions_router)
api_v1_router.include_router(payments_router)
api_v1_router.include_router(payment_webhooks_router)
api_v1_router.include_router(community_payouts_router)
api_v1_router.include_router(platform_payouts_router)
api_v1_router.include_router(notification_webhooks_router)
api_v1_router.include_router(notifications_router)
api_v1_router.include_router(in_app_notifications_router)
api_v1_router.include_router(targets_router)
api_v1_router.include_router(profiles_router)

