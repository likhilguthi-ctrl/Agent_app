class PlatformIdentityError(Exception):
    """Base error for the platform identity boundary."""


class PlatformIdentityNotConfigured(PlatformIdentityError):
    """Platform OIDC settings are absent or not sufficient for secure validation."""


class PlatformIdentityUnavailable(PlatformIdentityError):
    """The configured issuer could not be safely reached or its metadata is invalid."""


class PlatformRequestAuthenticationUnavailable(PlatformIdentityError):
    """No approved server-side HTTP authentication flow can resolve an operator principal."""


class InvalidPlatformIdentity(PlatformIdentityError):
    """The external identity assertion failed OIDC or MFA validation."""


class PlatformOperatorNotAuthorized(PlatformIdentityError):
    """The verified external subject is not an active, authorized operator."""


class PlatformRequestForgery(PlatformIdentityError):
    """A platform state-changing request failed CSRF or Origin validation."""


class PlatformAuthorizationUnavailable(PlatformIdentityError):
    """Server-side platform permission state could not be safely loaded."""
