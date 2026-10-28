from __future__ import annotations

from enum import StrEnum


class PermissionCode(StrEnum):
    """Registered operation identifiers; role names are deliberately absent."""

    AGENT_CREATE = "agent.create"
    AGENT_READ = "agent.read"
    AGENT_UPDATE = "agent.update"
    AGENT_CANCEL = "agent.cancel"
    AGENT_APPROVE = "agent.approve"
    AGENT_REJECT = "agent.reject"
    AGENT_SUSPEND = "agent.suspend"
    AGENT_REVOKE = "agent.revoke"

    ROLE_CREATE = "role.create"
    ROLE_READ = "role.read"
    ROLE_UPDATE = "role.update"
    ROLE_DELETE = "role.delete"
    ROLE_ASSIGN = "role.assign"
    ROLE_REVOKE = "role.revoke"
    PERMISSION_READ = "permission.read"
    ROLE_PERMISSION_ASSIGN = "role_permission.assign"
    ROLE_PERMISSION_REVOKE = "role_permission.revoke"

    REGION_CREATE = "region.create"
    REGION_READ = "region.read"
    REGION_UPDATE = "region.update"
    REGION_MANAGE = "region.manage"
    REGION_ASSIGN = "region.assign"

    MEMBER_CREATE = "member.create"
    MEMBER_READ = "member.read"
    MEMBER_UPDATE = "member.update"
    MEMBER_DEACTIVATE = "member.deactivate"
    MEMBER_RELATIONSHIP_MANAGE = "member.relationship.manage"
    MEMBER_STATS_READ = "member.stats.read"
    MEMBER_DESCENDANTS_READ = "member.descendants.read"
    MEMBER_DESCENDANTS_CREATE = "member.descendants.create"

    APPROVAL_READ = "approval.read"
    AUDIT_READ = "audit.read"
    NOTIFICATION_READ = "notification.read"
    NOTIFICATION_MANAGE = "notification.manage"
    COMMISSION_READ = "commission.read"
    COMMISSION_MANAGE = "commission.manage"
    SUBSCRIPTION_READ = "subscription.read"
    SUBSCRIPTION_MANAGE = "subscription.manage"
    PAYMENT_READ = "payment.read"
    PAYMENT_MANAGE = "payment.manage"

    PAYOUT_REQUEST = "payout.request"
    PAYOUT_REVIEW = "payout.review"
    PAYOUT_READ = "payout.read"
    PAYOUT_DISBURSE = "payout.disburse"

    TARGET_READ = "target.read"
    TARGET_MANAGE = "target.manage"
    TARGET_ALLOCATE = "target.allocate"


PERMISSION_DESCRIPTIONS: dict[PermissionCode, str] = {
    PermissionCode.AGENT_CREATE: "Submit an agent appointment.",
    PermissionCode.AGENT_READ: "Read agents within applicable tenant and scope policies.",
    PermissionCode.AGENT_UPDATE: "Update permitted agent profile fields.",
    PermissionCode.AGENT_CANCEL: "Cancel a pending appointment subject to policy.",
    PermissionCode.AGENT_APPROVE: "Approve an eligible appointment.",
    PermissionCode.AGENT_REJECT: "Reject an eligible appointment.",
    PermissionCode.AGENT_SUSPEND: "Suspend an active agent assignment.",
    PermissionCode.AGENT_REVOKE: "Revoke an agent assignment.",
    PermissionCode.ROLE_CREATE: "Create a tenant role.",
    PermissionCode.ROLE_READ: "Read tenant roles.",
    PermissionCode.ROLE_UPDATE: "Update a tenant role.",
    PermissionCode.ROLE_DELETE: "Delete a tenant role when policy permits.",
    PermissionCode.ROLE_ASSIGN: "Assign an authorized tenant role to a membership.",
    PermissionCode.ROLE_REVOKE: "Revoke a tenant role assignment.",
    PermissionCode.PERMISSION_READ: "Read the platform permission catalog.",
    PermissionCode.ROLE_PERMISSION_ASSIGN: "Assign an authorized permission to a tenant role.",
    PermissionCode.ROLE_PERMISSION_REVOKE: "Revoke a permission from a tenant role.",
    PermissionCode.REGION_CREATE: "Create a tenant region.",
    PermissionCode.REGION_READ: "Read tenant regions.",
    PermissionCode.REGION_UPDATE: "Update a tenant region.",
    PermissionCode.REGION_MANAGE: "Manage tenant regions within applicable scope policy.",
    PermissionCode.REGION_ASSIGN: "Assign a membership to a tenant region.",
    PermissionCode.MEMBER_CREATE: "Create a tenant member.",
    PermissionCode.MEMBER_READ: "Read members within applicable tenant and scope policies.",
    PermissionCode.MEMBER_UPDATE: "Update permitted member fields.",
    PermissionCode.MEMBER_DEACTIVATE: "Deactivate a tenant member.",
    PermissionCode.MEMBER_RELATIONSHIP_MANAGE: "Manage tenant member relationships.",
    PermissionCode.MEMBER_STATS_READ: "Read permitted member statistics.",
    PermissionCode.MEMBER_DESCENDANTS_READ: "Read permitted descendant information.",
    PermissionCode.MEMBER_DESCENDANTS_CREATE: "Create an allowed descendant relationship.",
    PermissionCode.APPROVAL_READ: "Read approval requests within applicable scope policies.",
    PermissionCode.AUDIT_READ: "Read permitted tenant audit records.",
    PermissionCode.NOTIFICATION_READ: "Read tenant notifications.",
    PermissionCode.NOTIFICATION_MANAGE: "Manage tenant notifications.",
    PermissionCode.COMMISSION_READ: "Read financial commission accounts and ledger statements.",
    PermissionCode.COMMISSION_MANAGE: "Manage financial accounts and post ledger adjustments.",
    PermissionCode.SUBSCRIPTION_READ: "Read subscriptions and available subscription plans.",
    PermissionCode.SUBSCRIPTION_MANAGE: "Create and manage subscriber subscriptions within community context.",
    PermissionCode.PAYMENT_READ: "Read payment transactions and gateway payment orders.",
    PermissionCode.PAYMENT_MANAGE: "Initiate and verify subscription payment transactions.",
    PermissionCode.PAYOUT_REQUEST: "Submit a payout request to withdraw earned commissions.",
    PermissionCode.PAYOUT_REVIEW: "Review and approve or reject community agent payout requests.",
    PermissionCode.PAYOUT_READ: "Read payout requests and statement history.",
    PermissionCode.PAYOUT_DISBURSE: "Authorize and execute fund disbursement for approved payouts.",
    PermissionCode.TARGET_READ: "Read assigned target metrics and performance summaries.",
    PermissionCode.TARGET_MANAGE: "Create, configure, and manage administrative target plans.",
    PermissionCode.TARGET_ALLOCATE: "Allocate and cascade targets to reporting team members.",
}


def permission_code(value: PermissionCode | str) -> PermissionCode:
    """Parse only registered permission identifiers; unknown values fail closed."""
    if isinstance(value, PermissionCode):
        return value
    try:
        return PermissionCode(value)
    except (TypeError, ValueError) as exc:
        from app.modules.authorization.errors import UnknownPermissionCode

        raise UnknownPermissionCode from exc


def validate_permission_catalog() -> None:
    """Catch catalog omissions and malformed codes as the application imports."""
    import re

    if set(PERMISSION_DESCRIPTIONS) != set(PermissionCode):
        raise RuntimeError("Permission code and description registries are inconsistent.")
    if any(re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*)+", code.value) is None for code in PermissionCode):
        raise RuntimeError("Permission catalog contains an invalid identifier.")


validate_permission_catalog()
