class PermissionDenied(Exception):
    """The authenticated principal does not satisfy the authorization policy."""


class UnknownPermissionCode(PermissionDenied):
    """A policy requested a permission that is not present in the catalog."""


class AuthorizationDataUnavailable(Exception):
    """Authorization state could not be safely loaded or evaluated."""
