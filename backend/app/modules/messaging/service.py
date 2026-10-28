from __future__ import annotations

import re
from collections.abc import Mapping

from app.modules.messaging.contracts import SendWhatsAppCommand
from app.modules.messaging.provider import MessagingProvider


_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
_PHONE_FORMATTING = re.compile(r"[ \t().-]")


def normalize_mobile_e164(value: str) -> str:
    """Normalize common punctuation; require a country-coded E.164 number."""
    if not isinstance(value, str) or not value or len(value) > 32:
        raise ValueError("Mobile number must be supplied in E.164 format.")
    candidate = _PHONE_FORMATTING.sub("", value)
    if not _E164.fullmatch(candidate):
        raise ValueError("Mobile number must include a country code in E.164 format.")
    return candidate


class MessagingService:
    """Stable application operations, independent of the external service contract."""

    def __init__(self, provider: MessagingProvider) -> None:
        self._provider = provider

    async def send_otp(self, mobile_number: str) -> bool:
        mobile = normalize_mobile_e164(mobile_number)
        return await self._provider.send_otp(mobile)

    async def verify_otp(self, mobile_number: str, otp: str) -> bool:
        mobile = normalize_mobile_e164(mobile_number)
        if not isinstance(otp, str) or not otp or len(otp) > 128 or any(ord(c) < 32 for c in otp):
            raise ValueError("OTP value is invalid.")
        return await self._provider.verify_otp(mobile, otp)

    async def send_whatsapp(
        self,
        recipient_number: str,
        payload: Mapping[str, object],
    ) -> bool:
        recipient = normalize_mobile_e164(recipient_number)
        if not isinstance(payload, Mapping):
            raise ValueError("WhatsApp payload must be an application-defined mapping.")
        return await self._provider.send_whatsapp(
            SendWhatsAppCommand(recipient_e164=recipient, payload=payload)
        )
