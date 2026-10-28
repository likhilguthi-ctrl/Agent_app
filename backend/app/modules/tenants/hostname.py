from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_hostname(authority: str | None) -> str:
    """Normalize a DNS Host authority, removing a valid port and trailing dot."""
    if authority is None:
        raise ValueError("A hostname is required.")

    raw = authority.strip()
    if not raw or any(ord(char) < 33 or ord(char) == 127 for char in raw):
        raise ValueError("The hostname is malformed.")
    if any(char in raw for char in ("/", "\\", "?", "#", "@", ",")):
        raise ValueError("The hostname is malformed.")

    try:
        parsed = urlsplit(f"//{raw}")
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("The hostname is malformed.") from exc

    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
        or port == 0
        or hostname is None
    ):
        raise ValueError("The hostname is malformed.")

    try:
        normalized = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("The hostname is malformed.") from exc

    if normalized.endswith(".."):
        raise ValueError("The hostname is malformed.")
    if normalized.endswith("."):
        normalized = normalized[:-1]

    if len(normalized) > 253:
        raise ValueError("The hostname is malformed.")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise ValueError("Tenant domains must be DNS hostnames, not IP addresses.")

    labels = normalized.split(".")
    if not labels or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise ValueError("The hostname is malformed.")
    return normalized
