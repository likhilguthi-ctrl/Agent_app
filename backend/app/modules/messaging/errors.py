from __future__ import annotations


class MessagingError(Exception):
    """Safe base for messaging integration failures."""


class MessagingConfigurationError(MessagingError):
    """Required local provider configuration is absent or invalid."""


class MessagingContractUnavailable(MessagingError):
    """The external service request/response mapping is not yet configured."""


class MessagingContractError(MessagingError):
    """The configured contract adapter could not safely interpret a response."""


class MessagingProviderUnavailable(MessagingError):
    """The external service could not be reached within configured timeouts."""


class MessagingProviderRejected(MessagingError):
    """The external service returned a non-success HTTP status."""
