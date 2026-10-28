from __future__ import annotations

import secrets

_REF_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_agent_ref_id() -> str:
    """Generate a clean, unambiguous 8-character uppercase alphanumeric reference ID."""
    return "".join(secrets.choice(_REF_CHARS) for _ in range(8))

