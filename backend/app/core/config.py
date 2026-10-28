from __future__ import annotations

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from math import isfinite
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _is_loopback_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme in {"http", "https"}
            and bool(hostname)
            and (
                hostname == "localhost"
                or hostname.endswith(".localhost")
                or ip_address(hostname).is_loopback
            )
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


class Settings(BaseSettings):
    app_name: str = "Community OTT Agent Platform API"
    environment: str = "development"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    frontend_origins: str = ""
    trusted_proxy_cidrs: str = ""
    database_url: SecretStr | None = None
    messaging_provider: Literal["http", "mock"] = "http"
    messaging_api_base_url: str | None = None
    messaging_send_otp_path: str | None = None
    messaging_verify_otp_path: str | None = None
    messaging_send_whatsapp_path: str | None = None
    messaging_send_otp_method: str | None = None
    messaging_verify_otp_method: str | None = None
    messaging_send_whatsapp_method: str | None = None
    messaging_api_key: SecretStr | None = None
    messaging_auth_header_name: str | None = None
    messaging_auth_value_prefix: str = ""
    messaging_connect_timeout_seconds: float = 3.0
    messaging_read_timeout_seconds: float = 10.0
    otp_challenge_ttl_seconds: int | None = None
    development_static_otp_enabled: bool = False
    development_static_otp_code: SecretStr | None = None
    otp_log_to_console: bool = False
    otp_skip_notification_dispatch: bool = False
    otp_send_cooldown_seconds: int = 60
    otp_send_window_seconds: int = 600
    otp_send_limit: int = 3
    otp_verify_window_seconds: int = 900
    otp_verify_limit: int = 5
    auth_session_ttl_seconds: int = 43200
    auth_session_cookie_name: str = "community_session"
    auth_session_cookie_path: str = "/backend-api/api/v1"
    auth_session_cookie_secure: bool | None = None
    auth_session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    platform_oidc_issuer: str | None = None
    platform_oidc_client_id: str | None = None
    platform_oidc_client_secret: SecretStr | None = None
    platform_oidc_redirect_uri: str | None = None
    platform_oidc_transaction_encryption_key: SecretStr | None = None
    platform_oidc_required_acr_values: str = ""
    platform_oidc_required_amr_values: str = ""
    platform_oidc_allowed_algorithms: str = "RS256,ES256,PS256"
    platform_oidc_discovery_timeout_seconds: float = 3.0
    platform_oidc_jwks_cache_seconds: int = 300
    platform_oidc_clock_skew_seconds: int = 60
    platform_oidc_max_auth_age_seconds: int = 300
    platform_oidc_login_ttl_seconds: int = 300
    platform_oidc_session_ttl_seconds: int = 3600
    platform_oidc_session_cookie_name: str = "community_platform_session"
    platform_oidc_session_cookie_path: str = "/backend-api/api/v1/platform"
    platform_oidc_session_cookie_samesite: Literal["lax"] = "lax"
    audit_sink_adapter: str | None = None
    audit_sink_endpoint: str | None = None
    audit_sink_auth_token: SecretStr | None = None
    audit_sink_ack_verification_key: SecretStr | None = None
    audit_sink_connect_timeout_seconds: float = 3.0
    audit_sink_read_timeout_seconds: float = 10.0
    outbox_worker_enabled: bool = False
    outbox_worker_poll_interval_seconds: float = 5.0
    outbox_worker_batch_size: int = 25
    outbox_worker_lease_seconds: int = 60
    outbox_worker_heartbeat_ttl_seconds: int = 60
    outbox_max_attempts: int = 12
    outbox_retry_base_seconds: int = 5
    outbox_retry_max_seconds: int = 900
    onboarding_request_ttl_seconds: int = 604800
    domain_challenge_ttl_seconds: int = 1800
    domain_challenge_issue_limit: int = 3
    domain_challenge_verify_limit: int = 5
    domain_challenge_issue_operator_limit: int = 20
    domain_challenge_issue_operator_window_seconds: int = 3600
    domain_challenge_verify_operator_limit: int = 60
    domain_challenge_verify_operator_window_seconds: int = 900
    domain_dns_query_timeout_seconds: float = 3.0
    domain_dns_max_records: int = 50
    domain_dns_max_response_bytes: int = 8192
    onboarding_expiry_batch_size: int = 100
    r2_account_id: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket_name: str | None = None
    r2_endpoint_url: str | None = None
    r2_region: str = "auto"
    identity_encryption_key: SecretStr | None = None
    notification_service_url: str = "http://localhost:8100"
    notification_service_base_url: str | None = None
    notification_service_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "NOTIFICATION_SERVICE_API_KEY",
            "X_API_KEY",
            "x_api_key",
            "NOTIFICATION_X_API_KEY",
            "notification_service_api_key",
        ),
    )
    notification_webhook_secret: SecretStr | None = None
    notification_webhook_timestamp_tolerance_seconds: int = 300
    notification_webhook_callback_url: str | None = None
    notification_service_id: str = "community-ott"
    notification_channel: Literal["WHATSAPP", "SMS"] = "WHATSAPP"
    notification_debug_logging: bool = False
    otp_delivery_mode: Literal["development_dummy", "notification_service", "mock"] = "notification_service"
    development_dummy_otp: str = "123456"
    super_admin_mobile: str = "+15550000001"
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None
    razorpay_mode: Literal["test", "live"] = "test"
    payment_service_url: str = "http://handorg.duckdns.org"
    payment_service_base_url: str | None = None
    payment_service_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PAYMENT_SERVICE_API_KEY",
            "payment_service_api_key",
            "PAYMENT_API_KEY",
        ),
    )
    payment_service_webhook_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PAYMENT_WEBHOOK_SECRET",
            "payment_webhook_secret",
        ),
    )
    payment_delivery_mode: Literal["payment_service", "mock", "development"] = "payment_service"

    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        env_ignore_empty=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    @field_validator("app_name")
    @classmethod
    def validate_app_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("APP_NAME must not be empty.")
        return value.strip()

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ENVIRONMENT must not be empty.")
        normalized = value.strip().lower()
        normalized = {"dev": "development", "prod": "production", "testing": "test"}.get(
            normalized,
            normalized,
        )
        if normalized not in {"development", "test", "staging", "production"}:
            raise ValueError("ENVIRONMENT must be development, test, staging or production.")
        return normalized

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be a supported Python logging level.")
        return normalized

    @field_validator("api_v1_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        if value.rstrip("/") != "/api/v1":
            raise ValueError("API_V1_PREFIX must remain /api/v1.")
        return "/api/v1"

    @field_validator("frontend_origins")
    @classmethod
    def validate_frontend_origins(cls, value: str) -> str:
        origins = [origin.strip() for origin in value.split(",") if origin.strip()]
        if "*" in origins:
            raise ValueError("FRONTEND_ORIGINS must contain explicit origins, not a wildcard.")

        for origin in origins:
            try:
                parsed = urlsplit(origin)
                parsed.port  # Validate malformed port values without retaining them.
            except ValueError as exc:
                raise ValueError("FRONTEND_ORIGINS contains an invalid origin.") from exc

            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or "*" in parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("FRONTEND_ORIGINS must contain valid HTTP(S) origins only.")

        return ",".join(origins)

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def validate_trusted_proxy_cidrs(cls, value: str) -> str:
        networks: list[str] = []
        for entry in value.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                network = ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError("TRUSTED_PROXY_CIDRS must contain valid IPs or CIDRs.") from exc
            if network.prefixlen == 0:
                raise ValueError("TRUSTED_PROXY_CIDRS must not trust every IP address.")
            networks.append(str(network))
        return ",".join(dict.fromkeys(networks))

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            parsed = make_url(value.get_secret_value())
        except ArgumentError as exc:
            raise ValueError("DATABASE_URL must be a valid PostgreSQL SQLAlchemy URL.") from exc
        if parsed.drivername != "postgresql+psycopg" or not parsed.host or not parsed.database:
            raise ValueError("DATABASE_URL must use postgresql+psycopg and include a host and database.")
        return value

    @field_validator("messaging_api_base_url")
    @classmethod
    def validate_messaging_api_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        try:
            parsed = urlsplit(candidate)
            parsed.port
        except ValueError as exc:
            raise ValueError("MESSAGING_API_BASE_URL must be a valid HTTP(S) URL.") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MESSAGING_API_BASE_URL must be a valid HTTP(S) URL without credentials.")
        return candidate.rstrip("/")

    @field_validator("notification_service_base_url", "notification_service_url")
    @classmethod
    def validate_notification_service_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        try:
            parsed = urlsplit(candidate)
            parsed.port
        except ValueError as exc:
            raise ValueError("NOTIFICATION_SERVICE_BASE_URL must be a valid HTTP(S) URL.") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("NOTIFICATION_SERVICE_BASE_URL must be a valid HTTP(S) URL without credentials.")
        return candidate.rstrip("/")

    @field_validator("notification_channel", mode="before")
    @classmethod
    def validate_notification_channel(cls, value: object) -> str:
        if isinstance(value, str):
            val = value.strip().upper()
            if val in {"WHATSAPP", "SMS"}:
                return val
        return str(value)

    @field_validator(
        "messaging_send_otp_path",
        "messaging_verify_otp_path",
        "messaging_send_whatsapp_path",
    )
    @classmethod
    def validate_messaging_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        parsed = urlsplit(candidate)
        if (
            not candidate.startswith("/")
            or candidate.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or any(segment in {".", ".."} for segment in candidate.split("/"))
            or any(character.isspace() for character in candidate)
        ):
            raise ValueError("Messaging endpoint paths must be paths on the configured base URL.")
        return candidate

    @field_validator(
        "messaging_send_otp_method",
        "messaging_verify_otp_method",
        "messaging_send_whatsapp_method",
    )
    @classmethod
    def normalize_messaging_method(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Messaging endpoint methods must be POST, PUT, PATCH or DELETE.")
        return normalized

    @field_validator("messaging_auth_header_name")
    @classmethod
    def validate_messaging_auth_header_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate or any(
            not (c.isascii() and (c.isalnum() or c in "!#$%&'*+-.^_`|~"))
            for c in candidate
        ):
            raise ValueError("MESSAGING_AUTH_HEADER_NAME must be a valid HTTP header name.")
        if candidate.lower() in {
            "connection", "content-length", "host", "transfer-encoding", "x-request-id"
        }:
            raise ValueError("MESSAGING_AUTH_HEADER_NAME is reserved and cannot be used.")
        return candidate

    @field_validator("messaging_auth_value_prefix")
    @classmethod
    def validate_messaging_auth_value_prefix(cls, value: str) -> str:
        if not value.isascii() or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("MESSAGING_AUTH_VALUE_PREFIX must be printable ASCII without controls.")
        return value

    @field_validator("messaging_api_key")
    @classmethod
    def validate_messaging_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        if not secret.strip() or "\r" in secret or "\n" in secret:
            raise ValueError("MESSAGING_API_KEY must be non-empty and contain no line breaks.")
        return value

    @field_validator("platform_oidc_issuer")
    @classmethod
    def validate_platform_oidc_issuer(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        try:
            parsed = urlsplit(candidate)
            parsed.port
        except ValueError as exc:
            raise ValueError("PLATFORM_OIDC_ISSUER must be a valid HTTPS issuer URL.") from exc
        environment = info.data.get("environment", "development")
        allowed_scheme = {"https", "http"} if environment == "test" else {"https"}
        if (
            parsed.scheme not in allowed_scheme
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(candidate) > 500
        ):
            raise ValueError("PLATFORM_OIDC_ISSUER must be an exact HTTPS issuer URL without query or fragment.")
        return candidate

    @field_validator("platform_oidc_client_id")
    @classmethod
    def validate_platform_oidc_client_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate or any(ord(char) < 33 or ord(char) == 127 for char in candidate):
            raise ValueError("PLATFORM_OIDC_CLIENT_ID must be a non-empty identifier.")
        return candidate

    @field_validator("platform_oidc_client_secret")
    @classmethod
    def validate_platform_oidc_client_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        if not secret.strip() or "\r" in secret or "\n" in secret:
            raise ValueError("PLATFORM_OIDC_CLIENT_SECRET must be non-empty and contain no line breaks.")
        return value

    @field_validator("platform_oidc_transaction_encryption_key")
    @classmethod
    def validate_platform_oidc_transaction_key(
        cls, value: SecretStr | None
    ) -> SecretStr | None:
        if value is None:
            return None
        import base64

        secret = value.get_secret_value()
        try:
            decoded = base64.urlsafe_b64decode(secret.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("PLATFORM_OIDC_TRANSACTION_ENCRYPTION_KEY must be a Fernet key.") from exc
        if len(decoded) != 32 or len(secret) != 44:
            raise ValueError("PLATFORM_OIDC_TRANSACTION_ENCRYPTION_KEY must be a Fernet key.")
        return value

    @field_validator("platform_oidc_redirect_uri")
    @classmethod
    def validate_platform_oidc_redirect_uri(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        try:
            parsed = urlsplit(candidate)
            parsed.port
        except ValueError as exc:
            raise ValueError("PLATFORM_OIDC_REDIRECT_URI must be an absolute HTTPS callback URL.") from exc
        environment = info.data.get("environment", "development")
        expected_path = f"/backend-api{info.data.get('api_v1_prefix', '/api/v1')}/platform/auth/oidc/callback"
        if (
            parsed.scheme not in ({"https", "http"} if environment == "test" else {"https"})
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != expected_path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PLATFORM_OIDC_REDIRECT_URI must use the exact same-origin proxy callback path.")
        return candidate

    @field_validator("platform_oidc_session_cookie_path")
    @classmethod
    def validate_platform_cookie_path(cls, value: str, info) -> str:
        expected = f"/backend-api{info.data.get('api_v1_prefix', '/api/v1')}/platform"
        if value != expected:
            raise ValueError("PLATFORM_OIDC_SESSION_COOKIE_PATH must match the same-origin backend proxy path.")
        return value

    @field_validator("platform_oidc_session_cookie_name")
    @classmethod
    def validate_platform_session_cookie_name(cls, value: str) -> str:
        if not value or any(
            not (char.isascii() and (char.isalnum() or char in "!#$%&'*+-.^_`|~"))
            for char in value
        ):
            raise ValueError("PLATFORM_OIDC_SESSION_COOKIE_NAME must be a valid cookie token.")
        return value

    @field_validator("platform_oidc_login_ttl_seconds")
    @classmethod
    def validate_platform_login_ttl(cls, value: int) -> int:
        if value < 60 or value > 900:
            raise ValueError("PLATFORM_OIDC_LOGIN_TTL_SECONDS must be between 60 and 900.")
        return value

    @field_validator("platform_oidc_session_ttl_seconds")
    @classmethod
    def validate_platform_session_ttl(cls, value: int) -> int:
        if value < 60 or value > 43200:
            raise ValueError("PLATFORM_OIDC_SESSION_TTL_SECONDS must be between 60 and 43200.")
        return value

    @field_validator("platform_oidc_required_acr_values", "platform_oidc_required_amr_values")
    @classmethod
    def normalize_oidc_assurance_values(cls, value: str) -> str:
        values = [part.strip() for part in value.split(",") if part.strip()]
        if any(any(ord(char) < 33 or ord(char) == 127 for char in item) for item in values):
            raise ValueError("OIDC assurance values must not contain control characters.")
        return ",".join(dict.fromkeys(values))

    @field_validator("platform_oidc_allowed_algorithms")
    @classmethod
    def validate_platform_oidc_algorithms(cls, value: str) -> str:
        supported = {"RS256", "PS256", "ES256"}
        algorithms = [part.strip() for part in value.split(",") if part.strip()]
        if not algorithms or len(set(algorithms)) != len(algorithms) or not set(algorithms) <= supported:
            raise ValueError("PLATFORM_OIDC_ALLOWED_ALGORITHMS must be a non-empty safe asymmetric allowlist.")
        return ",".join(algorithms)

    @field_validator("platform_oidc_discovery_timeout_seconds", "audit_sink_connect_timeout_seconds", "audit_sink_read_timeout_seconds")
    @classmethod
    def validate_security_http_timeout(cls, value: float) -> float:
        if not isfinite(value) or value <= 0 or value > 30:
            raise ValueError("Security integration timeouts must be greater than zero and no more than 30 seconds.")
        return value

    @field_validator(
        "platform_oidc_jwks_cache_seconds",
        "platform_oidc_clock_skew_seconds",
        "platform_oidc_max_auth_age_seconds",
        "outbox_worker_batch_size",
        "outbox_worker_lease_seconds",
        "outbox_worker_heartbeat_ttl_seconds",
        "outbox_max_attempts",
        "outbox_retry_base_seconds",
        "outbox_retry_max_seconds",
    )
    @classmethod
    def validate_security_positive_limits(cls, value: int) -> int:
        if value <= 0 or value > 86400:
            raise ValueError("Security integration limits must be between 1 and 86400.")
        return value

    @field_validator("outbox_worker_poll_interval_seconds")
    @classmethod
    def validate_outbox_poll_interval(cls, value: float) -> float:
        if not isfinite(value) or value < 0.1 or value > 300:
            raise ValueError("OUTBOX_WORKER_POLL_INTERVAL_SECONDS must be between 0.1 and 300.")
        return value

    @field_validator("domain_dns_query_timeout_seconds")
    @classmethod
    def validate_domain_dns_timeout(cls, value: float) -> float:
        if not isfinite(value) or value < 0.1 or value > 10:
            raise ValueError("DOMAIN_DNS_QUERY_TIMEOUT_SECONDS must be between 0.1 and 10.")
        return value

    @field_validator(
        "domain_challenge_ttl_seconds",
        "domain_challenge_issue_limit",
        "domain_challenge_verify_limit",
        "domain_challenge_issue_operator_limit",
        "domain_challenge_issue_operator_window_seconds",
        "domain_challenge_verify_operator_limit",
        "domain_challenge_verify_operator_window_seconds",
        "domain_dns_max_records",
        "domain_dns_max_response_bytes",
        "onboarding_expiry_batch_size",
    )
    @classmethod
    def validate_onboarding_limits(cls, value: int) -> int:
        if value <= 0 or value > 86400:
            raise ValueError("Onboarding limits must be between 1 and 86400.")
        return value

    @field_validator("onboarding_request_ttl_seconds")
    @classmethod
    def validate_onboarding_request_ttl(cls, value: int) -> int:
        if value < 60 or value > 2_592_000:
            raise ValueError("ONBOARDING_REQUEST_TTL_SECONDS must be between 60 and 2592000.")
        return value

    @field_validator("audit_sink_adapter")
    @classmethod
    def validate_audit_sink_adapter_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", candidate):
            raise ValueError("AUDIT_SINK_ADAPTER must be a short adapter identifier.")
        return candidate

    @field_validator("audit_sink_endpoint")
    @classmethod
    def validate_audit_sink_endpoint(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        try:
            parsed = urlsplit(candidate)
            parsed.port
        except ValueError as exc:
            raise ValueError("AUDIT_SINK_ENDPOINT must be a valid HTTPS URL.") from exc
        environment = info.data.get("environment", "development")
        allowed_scheme = {"https", "http"} if environment == "test" else {"https"}
        if (
            parsed.scheme not in allowed_scheme
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("AUDIT_SINK_ENDPOINT must use HTTPS and contain no embedded credentials or fragment.")
        return candidate

    @field_validator("audit_sink_auth_token", "audit_sink_ack_verification_key")
    @classmethod
    def validate_audit_sink_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        if not secret.strip() or "\r" in secret or "\n" in secret:
            raise ValueError("Audit sink credentials and acknowledgement keys must be non-empty single-line values.")
        return value

    @field_validator("messaging_connect_timeout_seconds", "messaging_read_timeout_seconds")
    @classmethod
    def validate_messaging_timeout(cls, value: float) -> float:
        if not isfinite(value) or value <= 0 or value > 120:
            raise ValueError("Messaging timeouts must be greater than zero and no more than 120 seconds.")
        return value

    @field_validator(
        "otp_challenge_ttl_seconds",
        "otp_send_cooldown_seconds",
        "otp_send_window_seconds",
        "otp_send_limit",
        "otp_verify_window_seconds",
        "otp_verify_limit",
        "auth_session_ttl_seconds",
    )
    @classmethod
    def validate_auth_limits(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value <= 0 or value > 604800:
            raise ValueError("Authentication time and attempt limits must be between 1 and 604800.")
        return value

    @field_validator("auth_session_cookie_name")
    @classmethod
    def validate_session_cookie_name(cls, value: str) -> str:
        if not value or any(not (char.isascii() and (char.isalnum() or char in "!#$%&'*+-.^_`|~")) for char in value):
            raise ValueError("AUTH_SESSION_COOKIE_NAME must be a valid cookie token.")
        return value

    @field_validator("development_static_otp_code")
    @classmethod
    def validate_development_static_otp_code(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        if not secret.isascii() or not secret.isdigit() or not 6 <= len(secret) <= 12:
            raise ValueError("DEVELOPMENT_STATIC_OTP_CODE must contain 6 to 12 digits.")
        return value

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.development_static_otp_enabled:
            if self.environment != "development":
                raise ValueError(
                    "DEVELOPMENT_STATIC_OTP_ENABLED is allowed only in development."
                )
            if self.development_static_otp_code is None:
                raise ValueError(
                    "DEVELOPMENT_STATIC_OTP_CODE is required when static development OTP is enabled."
                )
            if self.otp_challenge_ttl_seconds is None:
                raise ValueError(
                    "OTP_CHALLENGE_TTL_SECONDS is required when static development OTP is enabled."
                )
            if not self.frontend_origin_list or any(
                not _is_loopback_origin(origin) for origin in self.frontend_origin_list
            ):
                raise ValueError(
                    "Static development OTP requires explicit loopback FRONTEND_ORIGINS."
                )
        elif self.development_static_otp_code is not None:
            raise ValueError(
                "DEVELOPMENT_STATIC_OTP_CODE requires DEVELOPMENT_STATIC_OTP_ENABLED=true."
            )

        if self.otp_log_to_console:
            if self.environment in {"production", "staging"}:
                raise ValueError(
                    "OTP_LOG_TO_CONSOLE is not permitted in production or staging."
                )
            if self.environment != "development":
                raise ValueError(
                    "OTP_LOG_TO_CONSOLE is allowed only in development."
                )

        if self.otp_skip_notification_dispatch:
            if self.environment in {"production", "staging"}:
                raise ValueError(
                    "OTP_SKIP_NOTIFICATION_DISPATCH is not permitted in production or staging."
                )
            if self.environment != "development":
                raise ValueError(
                    "OTP_SKIP_NOTIFICATION_DISPATCH is allowed only in development."
                )

        if self.notification_debug_logging:
            if self.environment in {"production", "staging"}:
                raise ValueError(
                    "NOTIFICATION_DEBUG_LOGGING is not permitted in production or staging."
                )
            if self.environment != "development":
                raise ValueError(
                    "NOTIFICATION_DEBUG_LOGGING is allowed only in development."
                )
        expected_auth_cookie_path = f"/backend-api{self.api_v1_prefix}"
        if self.auth_session_cookie_path != expected_auth_cookie_path:
            raise ValueError(
                "AUTH_SESSION_COOKIE_PATH must match the same-origin backend auth proxy path."
            )
        if self.messaging_provider == "mock" and self.environment != "test":
            raise ValueError("MESSAGING_PROVIDER=mock is allowed only in automated test configuration.")
        if self.messaging_api_key is not None and self.messaging_auth_header_name is None:
            raise ValueError("MESSAGING_AUTH_HEADER_NAME is required when MESSAGING_API_KEY is configured.")
        if self.messaging_auth_header_name is not None and self.messaging_api_key is None:
            raise ValueError("MESSAGING_API_KEY is required when MESSAGING_AUTH_HEADER_NAME is configured.")


        if self.environment in {"production", "staging"}:
            if self.database_url is None:
                raise ValueError("DATABASE_URL is required in production or staging.")
            if not self.frontend_origin_list:
                raise ValueError("FRONTEND_ORIGINS must be configured in production or staging.")
            if any(not origin.startswith("https://") for origin in self.frontend_origin_list):
                raise ValueError("Production and staging frontend origins must use HTTPS.")
            parsed_database_url = make_url(self.database_url.get_secret_value())
            if (parsed_database_url.password or "").strip().lower() in {
                "replace_me",
                "changeme",
                "change_me",
                "your_password",
                "password",
            }:
                raise ValueError("Production DATABASE_URL must not use a placeholder password.")
            if self.messaging_provider != "http":
                raise ValueError("Production and staging require MESSAGING_PROVIDER=http.")
            if not self.messaging_api_base_url:
                raise ValueError("MESSAGING_API_BASE_URL is required in production or staging.")
            if not all(
                (
                    self.messaging_send_otp_path,
                    self.messaging_verify_otp_path,
                    self.messaging_send_otp_method,
                    self.messaging_verify_otp_method,
                )
            ):
                raise ValueError(
                    "Production and staging require configured OTP endpoint paths and methods."
                )
            if urlsplit(self.messaging_api_base_url).scheme != "https":
                raise ValueError("Production and staging messaging endpoints must use HTTPS.")
            if self.otp_challenge_ttl_seconds is None:
                raise ValueError(
                    "OTP_CHALLENGE_TTL_SECONDS must be set to the verified messaging-provider TTL."
                )
            if self.otp_delivery_mode == "development_dummy":
                raise ValueError("OTP_DELIVERY_MODE=development_dummy is strictly forbidden in production or staging.")
            if self.otp_skip_notification_dispatch:
                raise ValueError("OTP_SKIP_NOTIFICATION_DISPATCH=true is strictly forbidden in production or staging.")
            if self.otp_log_to_console:
                raise ValueError("OTP_LOG_TO_CONSOLE=true is strictly forbidden in production or staging.")
            if self.development_static_otp_enabled:
                raise ValueError("DEVELOPMENT_STATIC_OTP_ENABLED=true is strictly forbidden in production or staging.")
            if self.otp_delivery_mode == "notification_service":
                if not self.notification_service_base_url:
                    raise ValueError("NOTIFICATION_SERVICE_BASE_URL is required in production or staging.")
                if urlsplit(self.notification_base_url).scheme != "https":
                    raise ValueError("Production and staging notification service endpoints must use HTTPS.")
                if self.notification_service_api_key is None or not self.notification_service_api_key.get_secret_value().strip():
                    raise ValueError("NOTIFICATION_SERVICE_API_KEY is required in production or staging when live delivery is enabled.")
                if self.notification_webhook_secret is None or not self.notification_webhook_secret.get_secret_value().strip():
                    raise ValueError("NOTIFICATION_WEBHOOK_SECRET is required in production or staging.")
                if self.notification_webhook_callback_url and not self.notification_webhook_callback_url.strip().startswith("https://"):
                    raise ValueError("NOTIFICATION_WEBHOOK_CALLBACK_URL must use HTTPS in production or staging.")
            if not self.session_cookie_secure:
                raise ValueError("Authentication session cookies must use Secure in production or staging.")
            if self.payment_delivery_mode in {"mock", "development"}:
                raise ValueError("PAYMENT_DELIVERY_MODE=mock or development is strictly forbidden in production or staging.")
            if self.environment == "production" and self.payment_service_url and not self.payment_service_url.startswith("https://"):
                raise ValueError("Production payment service endpoints must use HTTPS.")
        if self.auth_session_cookie_samesite == "none" and not self.session_cookie_secure:
            raise ValueError("SameSite=None authentication cookies require Secure.")

        oidc_values = (
            self.platform_oidc_issuer,
            self.platform_oidc_client_id,
            self.platform_oidc_required_acr_values,
            self.platform_oidc_required_amr_values,
        )
        if any(oidc_values) and not (
            self.platform_oidc_issuer
            and self.platform_oidc_client_id
            and (self.platform_oidc_required_acr_list or self.platform_oidc_required_amr_list)
        ):
            raise ValueError(
                "Platform OIDC requires issuer, client ID, and an explicit MFA/authentication-strength claim policy."
            )

        oidc_login_values = (
            self.platform_oidc_client_secret,
            self.platform_oidc_redirect_uri,
            self.platform_oidc_transaction_encryption_key,
        )
        if any(oidc_login_values) and not (
            self.platform_oidc_configured
            and all(oidc_login_values)
        ):
            raise ValueError(
                "Platform OIDC login requires a configured issuer, client, MFA policy, exact redirect URI, client secret, and transaction encryption key."
            )
        if self.platform_oidc_redirect_uri and self.environment in {"production", "staging"}:
            parsed_redirect = urlsplit(self.platform_oidc_redirect_uri)
            redirect_origin = f"{parsed_redirect.scheme}://{parsed_redirect.netloc}"
            if redirect_origin not in self.frontend_origin_list:
                raise ValueError(
                    "PLATFORM_OIDC_REDIRECT_URI origin must exactly match a configured FRONTEND_ORIGINS entry."
                )
        if self.platform_oidc_login_configured:
            expected_cookie_path = f"/backend-api{self.api_v1_prefix}/platform"
            if self.platform_oidc_session_cookie_path != expected_cookie_path:
                raise ValueError(
                    "PLATFORM_OIDC_SESSION_COOKIE_PATH must match the configured same-origin proxy path."
                )

        sink_values = (
            self.audit_sink_adapter,
            self.audit_sink_endpoint,
            self.audit_sink_auth_token,
            self.audit_sink_ack_verification_key,
        )
        if any(sink_values) and not all(sink_values):
            raise ValueError(
                "Audit sink adapter, endpoint, authentication token, and acknowledgement verification key must be configured together."
            )
        if self.outbox_worker_enabled and not self.audit_sink_configuration_complete:
            raise ValueError("OUTBOX_WORKER_ENABLED requires complete external audit sink configuration.")
        if self.outbox_retry_max_seconds < self.outbox_retry_base_seconds:
            raise ValueError("OUTBOX_RETRY_MAX_SECONDS must be at least OUTBOX_RETRY_BASE_SECONDS.")
        if self.outbox_worker_heartbeat_ttl_seconds < self.outbox_worker_poll_interval_seconds * 2:
            raise ValueError("OUTBOX_WORKER_HEARTBEAT_TTL_SECONDS must cover at least two worker poll intervals.")
        return self

    @property
    def frontend_origin_list(self) -> list[str]:
        return list(dict.fromkeys(origin for origin in self.frontend_origins.split(",") if origin))

    @property
    def session_cookie_secure(self) -> bool:
        if self.auth_session_cookie_secure is not None:
            return self.auth_session_cookie_secure
        return self.environment not in {"development", "test"}

    @property
    def trusted_proxy_network_list(self) -> tuple[IPv4Network | IPv6Network, ...]:
        return tuple(
            ip_network(network, strict=False)
            for network in self.trusted_proxy_cidrs.split(",")
            if network
        )

    @property
    def platform_oidc_required_acr_list(self) -> tuple[str, ...]:
        return tuple(value for value in self.platform_oidc_required_acr_values.split(",") if value)

    @property
    def platform_oidc_required_amr_list(self) -> tuple[str, ...]:
        return tuple(value for value in self.platform_oidc_required_amr_values.split(",") if value)

    @property
    def platform_oidc_algorithm_list(self) -> tuple[str, ...]:
        return tuple(value for value in self.platform_oidc_allowed_algorithms.split(",") if value)

    @property
    def platform_oidc_configured(self) -> bool:
        return bool(
            self.platform_oidc_issuer
            and self.platform_oidc_client_id
            and (self.platform_oidc_required_acr_list or self.platform_oidc_required_amr_list)
        )

    @property
    def platform_oidc_login_configured(self) -> bool:
        return bool(
            self.platform_oidc_configured
            and self.platform_oidc_client_secret
            and self.platform_oidc_redirect_uri
            and self.platform_oidc_transaction_encryption_key
        )

    @property
    def audit_sink_configuration_complete(self) -> bool:
        return bool(
            self.audit_sink_adapter
            and self.audit_sink_endpoint
            and self.audit_sink_auth_token
            and self.audit_sink_ack_verification_key
        )

    @property
    def notification_base_url(self) -> str:
        url = self.notification_service_base_url or self.notification_service_url or "http://localhost:8100"
        return url.rstrip("/")

    @property
    def payment_base_url(self) -> str:
        url = self.payment_service_base_url or self.payment_service_url or "http://handorg.duckdns.org"
        return url.rstrip("/")

    @property
    def effective_notification_webhook_callback_url(self) -> str:
        if self.notification_webhook_callback_url:
            return self.notification_webhook_callback_url.strip()
        if self.frontend_origin_list:
            base = self.frontend_origin_list[0].rstrip("/")
            return f"{base}/backend-api{self.api_v1_prefix}/webhooks/notifications/status"
        return f"http://localhost:8000{self.api_v1_prefix}/webhooks/notifications/status"

    @property
    def effective_payment_webhook_callback_url(self) -> str:
        if self.frontend_origin_list:
            base = self.frontend_origin_list[0].rstrip("/")
            return f"{base}/backend-api{self.api_v1_prefix}/webhooks/payments/status"
        return f"http://localhost:8000{self.api_v1_prefix}/webhooks/payments/status"

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def can_log_otp_to_console(self) -> bool:
        return self.environment == "development" and self.otp_log_to_console

    @property
    def can_skip_notification_dispatch(self) -> bool:
        return self.environment == "development" and self.otp_skip_notification_dispatch

    @property
    def can_debug_log_notifications(self) -> bool:
        return self.environment == "development" and self.notification_debug_logging

    @property
    def x_api_key(self) -> SecretStr | None:
        """Fixed X-API-Key loaded from .env configuration."""
        return self.notification_service_api_key



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
