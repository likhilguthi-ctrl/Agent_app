class TenantMembershipRequired(Exception):
    """The caller has a valid session but no active membership in the resolved tenant."""
