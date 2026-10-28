from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.db.session import get_session_factory
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.platform_identity.errors import (
    InvalidPlatformIdentity,
    PlatformAuthorizationUnavailable,
    PlatformIdentityNotConfigured,
    PlatformIdentityUnavailable,
    PlatformOperatorNotAuthorized,
)
from app.modules.platform_identity.models import (
    PlatformOIDCLoginTransaction,
    PlatformOperator,
    PlatformOperatorSession,
)
from app.modules.platform_identity.provider import ExternalOperatorIdentity, PlatformIdentityProvider
from app.modules.platform_identity.repository import SQLAlchemyPlatformIdentityRepository
from app.modules.platform_identity.service import PlatformPrincipal


@dataclass(frozen=True, slots=True)
class LoginStart:
    authorization_url: str
    browser_binding: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginCompletion:
    principal: PlatformPrincipal
    raw_session_token: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class ClaimedLogin:
    transaction_id: UUID
    encrypted_flow_secrets: str
    redirect_uri: str


class SQLAlchemyPlatformOIDCSessionStore:
    """Durable one-use login state and opaque operator sessions."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        try:
            self._session_factory = session_factory or get_session_factory()
        except RuntimeError as exc:
            raise PlatformAuthorizationUnavailable from exc
        self._operators = SQLAlchemyPlatformIdentityRepository(self._session_factory)
        self._audit = AuditService()

    def create_transaction(self, transaction: PlatformOIDCLoginTransaction) -> None:
        try:
            with self._session_factory.begin() as session:
                session.execute(
                    delete(PlatformOIDCLoginTransaction).where(
                        PlatformOIDCLoginTransaction.expires_at <= transaction.created_at
                    )
                )
                session.add(transaction)
                session.flush()
        except SQLAlchemyError as exc:
            raise PlatformAuthorizationUnavailable from exc

    def record_authentication_failure(
        self,
        *,
        reason_code: str,
        outcome: str,
        request_id: str | None,
    ) -> None:
        try:
            with self._session_factory.begin() as session:
                self._audit.record_mandatory(
                    session,
                    AuditEventDraft(
                        event_type="platform.operator.authentication",
                        actor_category="unauthenticated",
                        actor_ref=None,
                        action="operator.authenticate",
                        outcome=outcome,
                        request_id=request_id,
                        decision_metadata={"reason_code": reason_code},
                    ),
                )
        except SQLAlchemyError as exc:
            raise PlatformAuthorizationUnavailable from exc

    def claim_transaction(
        self, *, state: str, browser_binding: str, now: datetime
    ) -> ClaimedLogin:
        if not state or len(state) > 256 or not browser_binding or len(browser_binding) > 256:
            raise InvalidPlatformIdentity
        state_digest = _digest(state)
        binding_digest = _digest(browser_binding)
        try:
            with self._session_factory.begin() as session:
                row = session.scalar(
                    select(PlatformOIDCLoginTransaction)
                    .where(
                        PlatformOIDCLoginTransaction.state_digest == state_digest,
                        PlatformOIDCLoginTransaction.browser_binding_digest == binding_digest,
                        PlatformOIDCLoginTransaction.expires_at > now,
                        PlatformOIDCLoginTransaction.exchange_started_at.is_(None),
                        PlatformOIDCLoginTransaction.consumed_at.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise InvalidPlatformIdentity
                claimed = session.execute(
                    update(PlatformOIDCLoginTransaction)
                    .where(
                        PlatformOIDCLoginTransaction.id == row.id,
                        PlatformOIDCLoginTransaction.exchange_started_at.is_(None),
                        PlatformOIDCLoginTransaction.consumed_at.is_(None),
                        PlatformOIDCLoginTransaction.expires_at > now,
                    )
                    .values(exchange_started_at=now)
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    raise InvalidPlatformIdentity
                return ClaimedLogin(row.id, row.encrypted_flow_secrets, row.redirect_uri)
        except InvalidPlatformIdentity:
            raise
        except SQLAlchemyError as exc:
            raise PlatformAuthorizationUnavailable from exc

    def complete_transaction(
        self,
        *,
        transaction_id: UUID,
        identity: ExternalOperatorIdentity,
        now: datetime,
        session_ttl_seconds: int,
        request_id: str | None,
    ) -> LoginCompletion:
        raw_session = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(48)
        try:
            with self._session_factory.begin() as session:
                transaction = session.scalar(
                    select(PlatformOIDCLoginTransaction)
                    .where(
                        PlatformOIDCLoginTransaction.id == transaction_id,
                        PlatformOIDCLoginTransaction.exchange_started_at.is_not(None),
                        PlatformOIDCLoginTransaction.consumed_at.is_(None),
                        PlatformOIDCLoginTransaction.expires_at > now,
                    )
                    .with_for_update()
                )
                if transaction is None:
                    raise InvalidPlatformIdentity
                transaction.consumed_at = now
                operator = self._operators.find_active_operator(
                    session, issuer=identity.issuer, subject=identity.subject
                )
                if operator is None:
                    self._audit.record_mandatory(
                        session,
                        AuditEventDraft(
                            event_type="platform.operator.authentication",
                            actor_category="unauthenticated",
                            actor_ref=None,
                            action="operator.authenticate",
                            outcome="denied",
                            request_id=request_id,
                            decision_metadata={"reason_code": "operator_not_provisioned"},
                        ),
                    )
                    unauthorized = True
                else:
                    unauthorized = False
                    platform_session = PlatformOperatorSession(
                        id=uuid4(),
                        operator_id=operator.id,
                        token_digest=_digest(raw_session),
                        csrf_token=csrf_token,
                        authenticated_at=identity.auth_time,
                        acr=identity.acr,
                        amr=list(identity.amr),
                        expires_at=now + timedelta(seconds=session_ttl_seconds),
                    )
                    session.add(platform_session)
                    self._audit.record_mandatory(
                        session,
                        AuditEventDraft(
                            event_type="platform.operator.authentication",
                            actor_category="platform_operator",
                            actor_ref=str(operator.id),
                            action="operator.authenticate",
                            outcome="success",
                            request_id=request_id,
                            decision_metadata={"auth_strength": "mfa_verified"},
                        ),
                    )
                    session.flush()
                    principal = PlatformPrincipal(
                        operator_id=operator.id,
                        issuer=identity.issuer,
                        subject=identity.subject,
                        authenticated_at=identity.auth_time,
                        acr=identity.acr,
                        amr=identity.amr,
                        session_id=platform_session.id,
                        csrf_token=csrf_token,
                        expires_at=platform_session.expires_at,
                    )
                    completion = LoginCompletion(principal, raw_session, csrf_token)
            if unauthorized:
                raise PlatformOperatorNotAuthorized
            return completion
        except (InvalidPlatformIdentity, PlatformOperatorNotAuthorized):
            raise
        except SQLAlchemyError as exc:
            raise PlatformAuthorizationUnavailable from exc

    def resolve_session(self, raw_token: str, *, now: datetime) -> PlatformPrincipal | None:
        if not raw_token or len(raw_token) > 128:
            return None
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(PlatformOperatorSession, PlatformOperator)
                    .join(PlatformOperator, PlatformOperator.id == PlatformOperatorSession.operator_id)
                    .where(
                        PlatformOperatorSession.token_digest == _digest(raw_token),
                        PlatformOperatorSession.revoked_at.is_(None),
                        PlatformOperatorSession.expires_at > now,
                        PlatformOperator.status == "active",
                    )
                ).one_or_none()
                if row is None:
                    return None
                platform_session, operator = row
                return PlatformPrincipal(
                    operator_id=operator.id,
                    issuer=operator.issuer,
                    subject=operator.subject,
                    authenticated_at=platform_session.authenticated_at,
                    acr=platform_session.acr,
                    amr=tuple(platform_session.amr),
                    session_id=platform_session.id,
                    csrf_token=platform_session.csrf_token,
                    expires_at=platform_session.expires_at,
                )
        except SQLAlchemyError as exc:
            raise PlatformAuthorizationUnavailable from exc

    def revoke_session(
        self,
        *,
        session_id: UUID,
        operator_id: UUID,
        now: datetime,
        request_id: str | None,
    ) -> None:
        try:
            with self._session_factory.begin() as session:
                revoked = session.execute(
                    update(PlatformOperatorSession)
                    .where(
                        PlatformOperatorSession.id == session_id,
                        PlatformOperatorSession.operator_id == operator_id,
                        PlatformOperatorSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                if revoked.rowcount == 1:
                    self._audit.record_mandatory(
                        session,
                        AuditEventDraft(
                            event_type="platform.operator.logout",
                            actor_category="platform_operator",
                            actor_ref=str(operator_id),
                            action="operator.logout",
                            outcome="success",
                            request_id=request_id,
                            target_entity_type="platform.operator_session",
                            target_entity_ref=str(session_id),
                            decision_metadata={"reason_code": "local_session_revoked"},
                        ),
                    )
        except SQLAlchemyError as exc:
            raise PlatformAuthorizationUnavailable from exc


class PlatformOIDCLoginService:
    """Authorization Code + PKCE flow for platform operators only."""

    def __init__(
        self,
        settings: Settings,
        provider: PlatformIdentityProvider,
        *,
        store: SQLAlchemyPlatformOIDCSessionStore | None = None,
        now=lambda: datetime.now(UTC),
    ) -> None:
        if not settings.platform_oidc_login_configured:
            raise PlatformIdentityNotConfigured
        self._settings = settings
        self._provider = provider
        self._store = store or SQLAlchemyPlatformOIDCSessionStore()
        self._now = now
        assert settings.platform_oidc_transaction_encryption_key is not None
        self._fernet = Fernet(
            settings.platform_oidc_transaction_encryption_key.get_secret_value().encode("ascii")
        )

    async def start(self) -> LoginStart:
        now = self._now()
        state = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(48)
        verifier = secrets.token_urlsafe(64)
        browser_binding = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        redirect_uri = self._settings.platform_oidc_redirect_uri
        if redirect_uri is None:
            raise PlatformIdentityNotConfigured
        authorization_url = await self._provider.authorization_url(
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            code_challenge=challenge,
        )
        secrets_payload = json.dumps(
            {"nonce": nonce, "code_verifier": verifier}, separators=(",", ":")
        ).encode()
        encrypted = self._fernet.encrypt(secrets_payload).decode("ascii")
        await run_in_threadpool(
            self._store.create_transaction,
            PlatformOIDCLoginTransaction(
                created_at=now,
                state_digest=_digest(state),
                browser_binding_digest=_digest(browser_binding),
                encrypted_flow_secrets=encrypted,
                redirect_uri=redirect_uri,
                expires_at=now + timedelta(seconds=self._settings.platform_oidc_login_ttl_seconds),
            ),
        )
        return LoginStart(
            authorization_url=authorization_url,
            browser_binding=browser_binding,
            expires_at=now + timedelta(seconds=self._settings.platform_oidc_login_ttl_seconds),
        )

    async def complete(
        self,
        *,
        state: str,
        code: str,
        browser_binding: str,
        request_id: str | None,
    ) -> LoginCompletion:
        try:
            claimed = await run_in_threadpool(
                self._store.claim_transaction,
                state=state,
                browser_binding=browser_binding,
                now=self._now(),
            )
        except InvalidPlatformIdentity:
            await self.record_authentication_failure(
                reason_code="login_state_rejected",
                outcome="denied",
                request_id=request_id,
            )
            raise
        try:
            payload = json.loads(self._fernet.decrypt(claimed.encrypted_flow_secrets.encode("ascii")))
            nonce = payload["nonce"]
            verifier = payload["code_verifier"]
        except (InvalidToken, ValueError, TypeError, KeyError) as exc:
            await self.record_authentication_failure(
                reason_code="login_transaction_invalid",
                outcome="denied",
                request_id=request_id,
            )
            raise InvalidPlatformIdentity from exc
        try:
            identity_token = await self._provider.exchange_authorization_code(
                code=code, redirect_uri=claimed.redirect_uri, code_verifier=verifier
            )
            identity = await self._provider.validate_identity_token(
                identity_token, expected_nonce=nonce
            )
        except InvalidPlatformIdentity:
            await self.record_authentication_failure(
                reason_code="invalid_external_identity",
                outcome="denied",
                request_id=request_id,
            )
            raise
        except PlatformIdentityUnavailable:
            await self.record_authentication_failure(
                reason_code="identity_provider_unavailable",
                outcome="failure",
                request_id=request_id,
            )
            raise
        authenticated_at = self._now()
        if not _has_required_assurance(self._settings, identity, now=authenticated_at):
            await self.record_authentication_failure(
                reason_code="authentication_assurance_rejected",
                outcome="denied",
                request_id=request_id,
            )
            raise InvalidPlatformIdentity
        return await run_in_threadpool(
            self._store.complete_transaction,
            transaction_id=claimed.transaction_id,
            identity=identity,
            now=authenticated_at,
            session_ttl_seconds=self._settings.platform_oidc_session_ttl_seconds,
            request_id=request_id,
        )

    async def record_authentication_failure(
        self, *, reason_code: str, outcome: str, request_id: str | None
    ) -> None:
        await run_in_threadpool(
            self._store.record_authentication_failure,
            reason_code=reason_code,
            outcome=outcome,
            request_id=request_id,
        )


def _has_required_assurance(
    settings: Settings, identity: ExternalOperatorIdentity, *, now: datetime
) -> bool:
    return bool(
        set(settings.platform_oidc_required_amr_list).issubset(set(identity.amr))
        and (
            not settings.platform_oidc_required_acr_list
            or identity.acr in settings.platform_oidc_required_acr_list
        )
        and identity.auth_time.tzinfo is not None
        and identity.auth_time <= now
        and (now - identity.auth_time).total_seconds()
        <= settings.platform_oidc_max_auth_age_seconds
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
