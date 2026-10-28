from __future__ import annotations

from app.core.config import Settings
from app.modules.messaging.contracts import MessagingContractCodec
from app.modules.messaging.errors import MessagingConfigurationError
from app.modules.messaging.http_provider import HttpMessagingProvider
from app.modules.messaging.provider import MessagingProvider


def create_messaging_provider(
    settings: Settings,
    codec: MessagingContractCodec | None = None,
) -> MessagingProvider:
    if settings.messaging_provider == "mock":
        if settings.environment != "test":
            raise MessagingConfigurationError(
                "The mock messaging provider is restricted to automated tests."
            )
        from app.modules.messaging.testing import MockMessagingProvider

        return MockMessagingProvider(environment=settings.environment)
    return HttpMessagingProvider(settings, codec=codec)
