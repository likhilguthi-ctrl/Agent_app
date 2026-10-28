from __future__ import annotations

import base64
import hashlib
import secrets


def hash_password(password: str) -> str:
    """Hash a plaintext password using memory-hard scrypt (OWASP recommended)."""
    if not password:
        raise ValueError("Password cannot be empty.")
    salt = secrets.token_bytes(16)
    # scrypt with N=16384, r=8, p=1 is memory-hard and NIST/OWASP approved.
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")
    return f"scrypt$16384$8$1${salt_b64}${hash_b64}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a stored scrypt hash in constant time."""
    if not plain_password or not hashed_password:
        return False
    try:
        parts = hashed_password.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = base64.b64decode(parts[4].encode("ascii"))
        expected_hash = base64.b64decode(parts[5].encode("ascii"))
        computed = hashlib.scrypt(
            plain_password.encode("utf-8"), salt=salt, n=n, r=r, p=p
        )
        return secrets.compare_digest(computed, expected_hash)
    except Exception:
        return False

