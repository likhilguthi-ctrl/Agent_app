"""CLI command to safely bootstrap Super Admin, Admin, and Agent accounts.

Accepts credentials securely via command line arguments, environment variables,
or interactive masked input (getpass). All passwords are validated and hashed using
modern scrypt before persistence. Idempotent: existing accounts are detected and
safely preserved unless --update-password is explicitly provided.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import sys
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.modules.authentication.models import AccountCredential, OtpRateBucket
from app.modules.authentication.password import hash_password
from app.modules.authorization.models import Role, UserRoleAssignment
from app.modules.identity.models import TenantMembership, User
from app.modules.messaging.service import normalize_mobile_e164
from app.modules.platform_identity.models import (
    PlatformOperator,
    PlatformOperatorRoleAssignment,
    PlatformRole,
)
from app.modules.tenants.models import Tenant

DEFAULT_SUPER_ADMIN_USERNAME = "super_admin"
DEFAULT_SUPER_ADMIN_PASSWORD = "SuperAdmin@123"
DEFAULT_SUPER_ADMIN_MOBILE = "9100209205"
DEFAULT_SUPER_ADMIN_ROLE = "SUPER_ADMIN"

_USERNAME_REGEX = re.compile(r"^[a-zA-Z0-9_.@-]{3,100}$")
_SLUG_REGEX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_username(username: str) -> str:
    cleaned = username.strip().lower()
    if not _USERNAME_REGEX.match(cleaned):
        raise ValueError(
            "Username must be between 3 and 100 characters and contain only "
            "alphanumeric characters, underscores, hyphens, dots, or @."
        )
    return cleaned


def validate_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long.")
    if len(password) > 256:
        raise ValueError("Password must not exceed 256 characters.")
    return password


def validate_tenant_slug(slug: str) -> str:
    cleaned = slug.strip().lower()
    if not _SLUG_REGEX.match(cleaned):
        raise ValueError(
            "Tenant slug must be between 1 and 63 lowercase alphanumeric characters or hyphens."
        )
    return cleaned


def bootstrap_super_admin(
    session: Session,
    *,
    username: str = DEFAULT_SUPER_ADMIN_USERNAME,
    password: str = DEFAULT_SUPER_ADMIN_PASSWORD,
    mobile_number: str | None = DEFAULT_SUPER_ADMIN_MOBILE,
    must_change_password: bool = True,
    update_password: bool = False,
) -> bool:
    """Idempotently bootstrap a SUPER_ADMIN account.

    Returns True if an account was created or updated, False if already exists unchanged.
    """
    now = datetime.now(UTC)
    existing_cred = session.execute(
        select(AccountCredential).where(AccountCredential.username == username)
    ).scalar_one_or_none()

    if existing_cred is not None:
        if update_password:
            existing_cred.password_hash = hash_password(password)
            existing_cred.status = "active"
            existing_cred.updated_at = now
            return True
        return False

    clean_mobile: str | None = None
    if mobile_number:
        raw_m = mobile_number.strip()
        if not raw_m.startswith("+"):
            raw_m = f"+91{raw_m}" if len(raw_m) == 10 else f"+{raw_m}"
        clean_mobile = normalize_mobile_e164(raw_m)

    # Ensure a platform grantor operator exists for role assignment provenance
    grantor = session.execute(
        select(PlatformOperator).where(PlatformOperator.subject == "system:root")
    ).scalar_one_or_none()
    if grantor is None:
        grantor = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject="system:root",
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(grantor)
        session.flush()

    # Ensure platform operator exists
    subject = f"operator:{username}"
    operator = session.execute(
        select(PlatformOperator).where(PlatformOperator.subject == subject)
    ).scalar_one_or_none()
    if operator is None:
        operator = PlatformOperator(
            id=uuid4(),
            issuer="https://identity.community-ott.local",
            subject=subject,
            mobile_e164=clean_mobile,
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(operator)
        session.flush()
    else:
        if clean_mobile and not operator.mobile_e164:
            operator.mobile_e164 = clean_mobile
            session.flush()

    # Ensure platform role exists
    role = session.execute(
        select(PlatformRole).where(PlatformRole.code == "super_admin")
    ).scalar_one_or_none()
    if role is None:
        role = PlatformRole(
            id=uuid4(),
            code="super_admin",
            display_name="Super Administrator",
            status="active",
            created_at=now,
        )
        session.add(role)
        session.flush()

    # Ensure role assignment exists
    assignment = session.execute(
        select(PlatformOperatorRoleAssignment).where(
            PlatformOperatorRoleAssignment.operator_id == operator.id,
            PlatformOperatorRoleAssignment.role_id == role.id,
            PlatformOperatorRoleAssignment.status == "active",
        )
    ).scalar_one_or_none()
    if assignment is None:
        session.add(
            PlatformOperatorRoleAssignment(
                id=uuid4(),
                operator_id=operator.id,
                role_id=role.id,
                assigned_by_operator_id=grantor.id,
                status="active",
                valid_from=now,
                created_at=now,
            )
        )
        session.flush()

    # Create account credential
    session.add(
        AccountCredential(
            id=uuid4(),
            username=username,
            password_hash=hash_password(password),
            account_type="platform_operator",
            operator_id=operator.id,
            must_change_password=must_change_password,
            status="active",
            created_at=now,
            updated_at=now,
        )
    )
    return True


def bootstrap_tenant_account(
    session: Session,
    *,
    role_type: str,  # "ADMIN" or "AGENT"
    username: str,
    password: str,
    tenant_slug: str,
    update_password: bool = False,
) -> bool:
    """Idempotently bootstrap an ADMIN or AGENT account bound to a tenant.

    Returns True if an account was created or updated, False if already exists unchanged.
    """
    now = datetime.now(UTC)
    existing_cred = session.execute(
        select(AccountCredential).where(AccountCredential.username == username)
    ).scalar_one_or_none()

    if existing_cred is not None:
        if update_password:
            existing_cred.password_hash = hash_password(password)
            existing_cred.status = "active"
            existing_cred.updated_at = now
            return True
        return False

    # Find or create tenant
    tenant = session.execute(
        select(Tenant).where(Tenant.slug == tenant_slug)
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(
            id=uuid4(),
            slug=tenant_slug,
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()
    elif tenant.status != "active":
        tenant.status = "active"
        tenant.updated_at = now
        session.flush()

    # Create user
    user = User(
        id=uuid4(),
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.flush()

    # Create tenant membership
    # Generate unique synthetic mobile number: +1555XXXXXXX
    random_digits = "".join(secrets.choice("0123456789") for _ in range(7))
    membership = TenantMembership(
        id=uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        mobile_e164=f"+1555{random_digits}",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(membership)
    session.flush()

    # Find or create role with permissions
    from app.modules.authorization.seeding import seed_tenant_role_permissions
    admin_r, agent_r = seed_tenant_role_permissions(session, tenant.id)
    role = admin_r if role_type == "ADMIN" else agent_r

    # Create role assignment
    session.add(
        UserRoleAssignment(
            id=uuid4(),
            tenant_id=tenant.id,
            membership_id=membership.id,
            role_id=role.id,
            assigned_by_membership_id=membership.id,
            status="active",
            valid_from=now,
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()

    # Create account credential
    session.add(
        AccountCredential(
            id=uuid4(),
            username=username,
            password_hash=hash_password(password),
            account_type="tenant_user",
            user_id=user.id,
            status="active",
            created_at=now,
            updated_at=now,
        )
    )
    return True


def bootstrap_super_admin_idempotent(
    factory: sessionmaker[Session] | None = None,
    *,
    username: str = DEFAULT_SUPER_ADMIN_USERNAME,
    password: str = DEFAULT_SUPER_ADMIN_PASSWORD,
    mobile_number: str = DEFAULT_SUPER_ADMIN_MOBILE,
    must_change_password: bool = True,
) -> bool:
    """Safely and idempotently bootstrap the Super Admin account if not existing."""
    session_factory = factory or get_session_factory()
    with session_factory.begin() as session:
        return bootstrap_super_admin(
            session,
            username=username,
            password=password,
            mobile_number=mobile_number,
            must_change_password=must_change_password,
            update_password=False,
        )


def run_bootstrap(
    *,
    role: str,
    username: str,
    password: str,
    mobile_number: str | None = None,
    must_change_password: bool = True,
    tenant_slug: str | None = None,
    update_password: bool = False,
    factory: sessionmaker[Session] | None = None,
) -> bool:
    session_factory = factory or get_session_factory()
    normalized_role = role.strip().upper()
    if normalized_role not in {"SUPER_ADMIN", "ADMIN", "AGENT"}:
        raise ValueError("Role must be SUPER_ADMIN, ADMIN, or AGENT.")

    normalized_username = validate_username(username)
    validated_password = validate_password(password)

    with session_factory.begin() as session:
        if normalized_role == "SUPER_ADMIN":
            return bootstrap_super_admin(
                session,
                username=normalized_username,
                password=validated_password,
                mobile_number=mobile_number or DEFAULT_SUPER_ADMIN_MOBILE,
                must_change_password=must_change_password,
                update_password=update_password,
            )
        else:
            if not tenant_slug:
                raise ValueError(f"--tenant-slug is required for role {normalized_role}.")
            normalized_slug = validate_tenant_slug(tenant_slug)
            return bootstrap_tenant_account(
                session,
                role_type=normalized_role,
                username=normalized_username,
                password=validated_password,
                tenant_slug=normalized_slug,
                update_password=update_password,
            )


def reset_rate_limit(
    session: Session,
    *,
    username: str | None = None,
    mobile_number: str | None = None,
) -> int:
    """Safely reset OtpRateBucket entries for a given username or mobile number."""
    target_mobile: str | None = None
    if mobile_number:
        target_mobile = normalize_mobile_e164(mobile_number)
    elif username:
        normalized_username = username.strip().lower()
        cred = session.execute(
            select(AccountCredential).where(AccountCredential.username == normalized_username)
        ).scalar_one_or_none()
        if cred:
            if cred.account_type == "platform_operator" and cred.operator_id:
                op = session.get(PlatformOperator, cred.operator_id)
                if op:
                    target_mobile = op.mobile_e164
            elif cred.account_type == "tenant_user" and cred.user_id:
                m = session.execute(
                    select(TenantMembership).where(
                        TenantMembership.user_id == cred.user_id,
                        TenantMembership.status == "active",
                    )
                ).scalars().first()
                if m:
                    target_mobile = m.mobile_e164
    if not target_mobile:
        raise ValueError("Could not resolve registered mobile number for rate limit reset.")

    result = session.execute(
        delete(OtpRateBucket).where(OtpRateBucket.mobile_e164 == target_mobile)
    )
    return result.rowcount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely bootstrap Super Admin, Admin, or Agent accounts with hashed credentials."
    )
    parser.add_argument(
        "--role",
        help="Account role: SUPER_ADMIN, ADMIN, or AGENT. (or env BOOTSTRAP_ROLE)",
        default=os.environ.get("BOOTSTRAP_ROLE", "SUPER_ADMIN"),
    )
    parser.add_argument(
        "--username",
        help="Account username. (or env BOOTSTRAP_USERNAME)",
        default=os.environ.get("BOOTSTRAP_USERNAME", DEFAULT_SUPER_ADMIN_USERNAME),
    )
    parser.add_argument(
        "--password",
        help="Account password. If omitted, prompts securely. (or env BOOTSTRAP_PASSWORD)",
        default=os.environ.get("BOOTSTRAP_PASSWORD", DEFAULT_SUPER_ADMIN_PASSWORD),
    )
    parser.add_argument(
        "--mobile",
        help="Registered mobile number. (or env BOOTSTRAP_MOBILE)",
        default=os.environ.get("BOOTSTRAP_MOBILE", DEFAULT_SUPER_ADMIN_MOBILE),
    )
    parser.add_argument(
        "--tenant-slug",
        help="Tenant community slug (required for ADMIN and AGENT). (or env BOOTSTRAP_TENANT_SLUG)",
        default=os.environ.get("BOOTSTRAP_TENANT_SLUG"),
    )
    parser.add_argument(
        "--update-password",
        action="store_true",
        help="Update password if account already exists.",
    )
    parser.add_argument(
        "--reset-rate-limit",
        action="store_true",
        help="Reset OTP rate limit buckets for the specified username or mobile.",
    )

    args = parser.parse_args(argv)

    if args.reset_rate_limit:
        try:
            factory = get_session_factory()
            with factory.begin() as session:
                count = reset_rate_limit(
                    session,
                    username=args.username,
                    mobile_number=args.mobile if args.mobile != DEFAULT_SUPER_ADMIN_MOBILE else None,
                )
                print(f"Successfully cleared {count} rate limit record(s).")
                return 0
        except Exception as exc:
            print(f"Error resetting rate limit: {exc}", file=sys.stderr)
            return 1

    if not args.role:
        parser.error("Role is required via --role or BOOTSTRAP_ROLE.")
    if not args.username:
        parser.error("Username is required via --username or BOOTSTRAP_USERNAME.")

    password = args.password
    if not password:
        password = getpass.getpass("Enter account password: ")

    try:
        changed = run_bootstrap(
            role=args.role,
            username=args.username,
            password=password,
            mobile_number=args.mobile,
            tenant_slug=args.tenant_slug,
            update_password=args.update_password,
        )
        if changed:
            role_display = args.role.upper()
            target_info = f" in tenant '{args.tenant_slug}'" if args.tenant_slug else ""
            print(f"Successfully provisioned {role_display} account '{args.username.lower()}'{target_info}.")
        else:
            print(
                f"Account '{args.username.lower()}' already exists. No changes made. "
                "Use --update-password to reset password."
            )
        return 0
    except Exception as exc:
        print(f"Error provisioning account: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

