from __future__ import annotations

import base64
import os
import re
from typing import Any, Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AADHAAR_REGEX: Final = re.compile(r"^\d{12}$")
_PAN_REGEX: Final = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")

# Fallback deterministic dev key for testing when IDENTITY_ENCRYPTION_KEY is not set
_DEV_KEY: Final = b"01234567890123456789012345678901"


def get_encryption_key(key_input: Any | None = None) -> bytes:
    if key_input is None:
        try:
            from app.core.config import get_settings
            cfg_key = get_settings().identity_encryption_key
            if cfg_key:
                return get_encryption_key(cfg_key)
        except Exception:
            pass
        return _DEV_KEY
    if isinstance(key_input, bytes) and len(key_input) in (16, 24, 32):
        return key_input
    if hasattr(key_input, "get_secret_value"):
        key_str = key_input.get_secret_value()
    else:
        key_str = str(key_input)
    clean = key_str.strip()
    try:
        decoded = base64.b64decode(clean)
        if len(decoded) in (16, 24, 32):
            return decoded
    except Exception:
        pass
    raw = clean.encode("utf-8")
    if len(raw) in (16, 24, 32):
        return raw
    if len(clean) == 64:
        try:
            return bytes.fromhex(clean)
        except ValueError:
            pass
    return _DEV_KEY


def validate_aadhaar_number(value: str) -> str:
    cleaned = re.sub(r"[\s-]", "", value.strip())
    if not _AADHAAR_REGEX.match(cleaned):
        raise ValueError("Aadhaar number must be exactly 12 digits.")
    return cleaned


def validate_pan_number(value: str) -> str:
    cleaned = value.strip().upper()
    if not _PAN_REGEX.match(cleaned):
        raise ValueError("PAN number must be 10 characters (5 letters, 4 digits, 1 letter).")
    return cleaned


def mask_aadhaar(aadhaar: str) -> str:
    cleaned = validate_aadhaar_number(aadhaar)
    last_four = cleaned[-4:]
    return f"XXXX-XXXX-{last_four}"


def mask_pan(pan: str) -> str:
    cleaned = validate_pan_number(pan)
    return f"XXXXX{cleaned[5:]}"


def encrypt_identity_number(plain_text: str, key: Any | None = None) -> str:
    """Encrypt a sensitive identity number using AES-256-GCM.
    
    Returns base64-encoded string containing nonce (12 bytes) + ciphertext.
    """
    aes_key = get_encryption_key(key)
    aesgcm = AESGCM(aes_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plain_text.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_identity_number(cipher_text_b64: str, key: Any | None = None) -> str:
    """Decrypt an AES-256-GCM encrypted identity number."""
    aes_key = get_encryption_key(key)
    aesgcm = AESGCM(aes_key)
    raw = base64.b64decode(cipher_text_b64.encode("ascii"))
    if len(raw) < 12 + 16:  # 12-byte nonce + 16-byte tag
        raise ValueError("Invalid encrypted identity payload.")
    nonce = raw[:12]
    ciphertext = raw[12:]
    decrypted = aesgcm.decrypt(nonce, ciphertext, None)
    return decrypted.decode("utf-8")
