from __future__ import annotations

import asyncio
import json
import time
import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import httpx
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet

from app.core.config import Settings
from app.modules.platform_identity.errors import (
    InvalidPlatformIdentity,
    PlatformIdentityNotConfigured,
    PlatformIdentityUnavailable,
)


@dataclass(frozen=True, slots=True)
class ExternalOperatorIdentity:
    issuer: str
    subject: str
    audience: str
    auth_time: datetime
    acr: str | None
    amr: tuple[str, ...]


class PlatformIdentityProvider(Protocol):
    """Provider-neutral boundary for an OIDC operator authorization-code flow."""

    async def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str: ...

    async def exchange_authorization_code(
        self, *, code: str, redirect_uri: str, code_verifier: str
    ) -> str: ...

    async def validate_identity_token(
        self, identity_token: str, *, expected_nonce: str
    ) -> ExternalOperatorIdentity: ...

    async def check_provider(self) -> bool: ...

    async def aclose(self) -> None: ...


class OIDCPlatformIdentityProvider:
    """Validate OIDC ID tokens against configured issuer discovery and signing keys.

    The authorization-code flow is provider-neutral and uses metadata only from the
    configured issuer. It does not select an IdP or provision operators.
    """

    _MAX_TOKEN_LENGTH = 16384
    _MAX_DOCUMENT_BYTES = 1_000_000
    _PRIVATE_JWK_FIELDS = {"d", "p", "q", "dp", "dq", "qi", "k"}

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock=time.time,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.platform_oidc_discovery_timeout_seconds,
                read=settings.platform_oidc_discovery_timeout_seconds,
                write=settings.platform_oidc_discovery_timeout_seconds,
                pool=settings.platform_oidc_discovery_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
        )
        self._cache_lock = asyncio.Lock()
        self._cached_jwks: dict[str, object] | None = None
        self._cached_discovery: dict[str, object] | None = None
        self._jwks_cache_expires_at = 0.0
        self._provider_algorithms: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return self._settings.platform_oidc_configured

    async def check_provider(self) -> bool:
        if not self.configured:
            return False
        try:
            await self._load_jwks(force=True)
        except (PlatformIdentityNotConfigured, PlatformIdentityUnavailable):
            return False
        return True

    async def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        """Build an OIDC authorization request from validated issuer metadata."""
        if not self._settings.platform_oidc_login_configured:
            raise PlatformIdentityNotConfigured
        if redirect_uri != self._settings.platform_oidc_redirect_uri:
            raise InvalidPlatformIdentity
        if (
            not isinstance(state, str)
            or len(state) < 32
            or len(state) > 256
            or not isinstance(nonce, str)
            or len(nonce) < 32
            or len(nonce) > 256
            or not isinstance(code_challenge, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge)
        ):
            raise InvalidPlatformIdentity
        await self._load_jwks()
        discovery = self._cached_discovery or {}
        endpoint = discovery.get("authorization_endpoint")
        if not isinstance(endpoint, str) or not self._valid_provider_url(endpoint):
            raise PlatformIdentityUnavailable
        client_id = self._settings.platform_oidc_client_id
        if not client_id:
            raise PlatformIdentityNotConfigured
        parameters = {
            "client_id": client_id,
            "response_type": "code",
            "response_mode": "query",
            "scope": "openid",
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "max_age": str(self._settings.platform_oidc_max_auth_age_seconds),
        }
        acr_values = self._settings.platform_oidc_required_acr_list
        if acr_values:
            parameters["acr_values"] = " ".join(acr_values)
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{urlencode(parameters)}"

    async def exchange_authorization_code(
        self, *, code: str, redirect_uri: str, code_verifier: str
    ) -> str:
        """Redeem a code using confidential-client auth and PKCE; return only its ID token."""
        if not self._settings.platform_oidc_login_configured:
            raise PlatformIdentityNotConfigured
        if redirect_uri != self._settings.platform_oidc_redirect_uri:
            raise InvalidPlatformIdentity
        if (
            not isinstance(code, str)
            or not code
            or len(code) > 2048
            or not isinstance(code_verifier, str)
            or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier)
        ):
            raise InvalidPlatformIdentity
        await self._load_jwks()
        discovery = self._cached_discovery or {}
        endpoint = discovery.get("token_endpoint")
        client_id = self._settings.platform_oidc_client_id
        client_secret = self._settings.platform_oidc_client_secret
        if (
            not isinstance(endpoint, str)
            or not self._valid_provider_url(endpoint)
            or not client_id
            or client_secret is None
        ):
            raise PlatformIdentityNotConfigured
        try:
            async with self._client.stream(
                "POST",
                endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                auth=(client_id, client_secret.get_secret_value()),
                headers={"Accept": "application/json"},
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                content = await self._read_bounded(response)
            payload = json.loads(content)
        except PlatformIdentityUnavailable:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise PlatformIdentityUnavailable from exc
        if not isinstance(payload, dict):
            raise InvalidPlatformIdentity
        identity_token = payload.get("id_token")
        if not isinstance(identity_token, str) or not identity_token:
            raise InvalidPlatformIdentity
        return identity_token

    async def validate_identity_token(
        self, identity_token: str, *, expected_nonce: str
    ) -> ExternalOperatorIdentity:
        if not self.configured or self._settings.platform_oidc_issuer is None:
            raise PlatformIdentityNotConfigured
        if (
            not isinstance(identity_token, str)
            or not identity_token
            or len(identity_token) > self._MAX_TOKEN_LENGTH
            or identity_token.count(".") != 2
        ):
            raise InvalidPlatformIdentity from None
        if not isinstance(expected_nonce, str) or len(expected_nonce) < 32:
            raise InvalidPlatformIdentity from None

        header = self._read_untrusted_header(identity_token)
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if (
            not isinstance(algorithm, str)
            or algorithm not in self._settings.platform_oidc_algorithm_list
            or not isinstance(key_id, str)
            or not key_id
            or any(field in header for field in ("jku", "jwk", "x5u", "x5c"))
        ):
            raise InvalidPlatformIdentity from None

        jwks = await self._load_jwks()
        key_set = self._select_key_set(jwks, key_id, algorithm)
        if key_set is None:
            jwks = await self._load_jwks(force=True)
            key_set = self._select_key_set(jwks, key_id, algorithm)
        if key_set is None:
            raise InvalidPlatformIdentity from None
        if self._provider_algorithms and algorithm not in self._provider_algorithms:
            raise InvalidPlatformIdentity from None

        try:
            decoded = jwt.decode(
                identity_token,
                key_set,
                algorithms=self._settings.platform_oidc_algorithm_list,
            )
            claims = dict(decoded.claims)
            oidc_claims = CodeIDToken(claims, decoded.header)
            now = self._clock()
            oidc_claims.validate(
                now=int(now),
                leeway=self._settings.platform_oidc_clock_skew_seconds,
            )
        except Exception as exc:
            # Do not leak token/parser details to callers or logs.
            raise InvalidPlatformIdentity from exc

        issuer = claims.get("iss")
        subject = claims.get("sub")
        audience = claims.get("aud")
        if issuer != self._settings.platform_oidc_issuer:
            raise InvalidPlatformIdentity from None
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise InvalidPlatformIdentity from None
        if isinstance(audience, str):
            audiences = [audience]
        elif isinstance(audience, list) and audience and all(isinstance(item, str) for item in audience):
            audiences = audience
        else:
            raise InvalidPlatformIdentity from None
        client_id = self._settings.platform_oidc_client_id
        if not client_id or client_id not in audiences:
            raise InvalidPlatformIdentity from None
        authorized_party = claims.get("azp")
        if (len(audiences) > 1 and authorized_party != client_id) or (
            authorized_party is not None and authorized_party != client_id
        ):
            raise InvalidPlatformIdentity from None
        if claims.get("nonce") != expected_nonce:
            raise InvalidPlatformIdentity from None

        acr = claims.get("acr")
        amr_value = claims.get("amr", ())
        if not isinstance(amr_value, list) or not all(isinstance(item, str) for item in amr_value):
            if amr_value == ():
                amr_value = []
            else:
                raise InvalidPlatformIdentity from None
        if len(amr_value) > 16 or any(not item or len(item) > 64 for item in amr_value):
            raise InvalidPlatformIdentity from None
        if isinstance(acr, str) and len(acr) > 255:
            raise InvalidPlatformIdentity from None
        required_acr = self._settings.platform_oidc_required_acr_list
        required_amr = self._settings.platform_oidc_required_amr_list
        if required_acr and (not isinstance(acr, str) or acr not in required_acr):
            raise InvalidPlatformIdentity from None
        if required_amr and not set(required_amr).issubset(set(amr_value)):
            raise InvalidPlatformIdentity from None

        auth_time_value = claims.get("auth_time")
        if isinstance(auth_time_value, bool) or not isinstance(auth_time_value, (int, float)):
            raise InvalidPlatformIdentity from None
        if auth_time_value > now + self._settings.platform_oidc_clock_skew_seconds:
            raise InvalidPlatformIdentity from None
        if now - auth_time_value > self._settings.platform_oidc_max_auth_age_seconds:
            raise InvalidPlatformIdentity from None

        return ExternalOperatorIdentity(
            issuer=issuer,
            subject=subject,
            audience=client_id,
            auth_time=datetime.fromtimestamp(auth_time_value, tz=UTC),
            acr=acr if isinstance(acr, str) else None,
            amr=tuple(amr_value),
        )

    async def _load_jwks(self, *, force: bool = False) -> dict[str, object]:
        if not self.configured or self._settings.platform_oidc_issuer is None:
            raise PlatformIdentityNotConfigured
        async with self._cache_lock:
            now = self._clock()
            if not force and self._cached_jwks is not None and now < self._jwks_cache_expires_at:
                return self._cached_jwks
            issuer = self._settings.platform_oidc_issuer
            discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
            try:
                discovery = await self._get_json(discovery_url)
                if discovery.get("issuer") != issuer:
                    raise PlatformIdentityUnavailable
                jwks_uri = discovery.get("jwks_uri")
                if not isinstance(jwks_uri, str) or not self._valid_provider_url(jwks_uri):
                    raise PlatformIdentityUnavailable
                authorization_endpoint = discovery.get("authorization_endpoint")
                token_endpoint = discovery.get("token_endpoint")
                if (
                    not isinstance(authorization_endpoint, str)
                    or not self._valid_provider_url(authorization_endpoint)
                    or not isinstance(token_endpoint, str)
                    or not self._valid_provider_url(token_endpoint)
                ):
                    raise PlatformIdentityUnavailable
                jwks = await self._get_json(jwks_uri)
            except PlatformIdentityUnavailable:
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise PlatformIdentityUnavailable from exc

            keys = jwks.get("keys")
            if not isinstance(keys, list) or not keys or len(keys) > 100:
                raise PlatformIdentityUnavailable
            algorithms = discovery.get("id_token_signing_alg_values_supported", [])
            if (
                not isinstance(algorithms, list)
                or not algorithms
                or not all(isinstance(item, str) for item in algorithms)
                or not set(algorithms).intersection(self._settings.platform_oidc_algorithm_list)
            ):
                raise PlatformIdentityUnavailable
            self._provider_algorithms = tuple(algorithms)
            self._cached_discovery = discovery
            public_keys = [
                key
                for key in keys
                if isinstance(key, dict)
                and isinstance(key.get("kid"), str)
                and key.get("use") in (None, "sig")
                and key.get("alg") in (None, *self._settings.platform_oidc_algorithm_list)
                and (key.get("key_ops") is None or (
                    isinstance(key.get("key_ops"), list) and "verify" in key["key_ops"]
                ))
                and not self._PRIVATE_JWK_FIELDS.intersection(key)
                and self._secure_key_parameters(key)
            ]
            if not public_keys:
                raise PlatformIdentityUnavailable
            try:
                KeySet.import_key_set({"keys": public_keys})
            except Exception as exc:
                raise PlatformIdentityUnavailable from exc
            self._cached_jwks = {"keys": public_keys}
            self._jwks_cache_expires_at = now + self._settings.platform_oidc_jwks_cache_seconds
            return self._cached_jwks

    async def _get_json(self, url: str) -> dict[str, object]:
        async with self._client.stream(
            "GET", url, headers={"Accept": "application/json"}, follow_redirects=False
        ) as response:
            response.raise_for_status()
            content = await self._read_bounded(response)
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise PlatformIdentityUnavailable
        return payload

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        length = 0
        async for chunk in response.aiter_bytes():
            length += len(chunk)
            if length > self._MAX_DOCUMENT_BYTES:
                raise PlatformIdentityUnavailable
            chunks.append(chunk)
        return b"".join(chunks)

    def _valid_provider_url(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError:
            return False
        allowed_schemes = {"https", "http"} if self._settings.environment == "test" else {"https"}
        return bool(
            parsed.scheme in allowed_schemes
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )

    def _select_key_set(
        self, jwks: dict[str, object], key_id: str, algorithm: str
    ) -> KeySet | None:
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            return None
        matches = []
        for key in keys:
            if not isinstance(key, dict) or key.get("kid") != key_id:
                continue
            if key.get("use") not in (None, "sig"):
                continue
            if key.get("alg") not in (None, algorithm):
                continue
            key_ops = key.get("key_ops")
            if key_ops is not None and (not isinstance(key_ops, list) or "verify" not in key_ops):
                continue
            if self._PRIVATE_JWK_FIELDS.intersection(key):
                continue
            matches.append(key)
        if len(matches) != 1:
            return None
        try:
            return KeySet.import_key_set({"keys": matches})
        except Exception:
            return None

    @staticmethod
    def _secure_key_parameters(key: dict[str, object]) -> bool:
        if key.get("kty") == "RSA":
            modulus = key.get("n")
            exponent = key.get("e")
            if not isinstance(modulus, str) or not isinstance(exponent, str):
                return False
            try:
                padded = modulus + "=" * (-len(modulus) % 4)
                return int.from_bytes(base64.urlsafe_b64decode(padded), "big").bit_length() >= 2048
            except (ValueError, TypeError):
                return False
        if key.get("kty") == "EC":
            return (
                key.get("crv") == "P-256"
                and isinstance(key.get("x"), str)
                and isinstance(key.get("y"), str)
            )
        return False

    @staticmethod
    def _read_untrusted_header(token: str) -> dict[str, object]:
        try:
            encoded = token.split(".", 1)[0]
            padding = "=" * (-len(encoded) % 4)
            value = json.loads(base64.urlsafe_b64decode(encoded + padding))
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError, json.JSONDecodeError):
            return {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
