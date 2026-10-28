from __future__ import annotations

import hmac
from collections.abc import Mapping

from app.modules.messaging.contracts import SendWhatsAppCommand


class MockMessagingProvider:
    """Automated-test fake. It verifies only explicitly configured test codes."""

    def __init__(
        self,
        accepted_otps: Mapping[str, str] | None = None,
        *,
        environment: str,
    ) -> None:
        if environment != "test":
            raise RuntimeError("MockMessagingProvider may be constructed only in test configuration.")
        self._accepted_otps = dict(accepted_otps or {})
        self.sent_otp_requests: list[str] = []
        self.sent_whatsapp_requests: list[SendWhatsAppCommand] = []

    async def send_otp(self, mobile_e164: str) -> bool:
        self.sent_otp_requests.append(mobile_e164)
        return True

    async def verify_otp(self, mobile_e164: str, otp: str) -> bool:
        expected = self._accepted_otps.get(mobile_e164)
        return expected is not None and hmac.compare_digest(expected, otp)

    async def send_whatsapp(self, command: SendWhatsAppCommand) -> bool:
        self.sent_whatsapp_requests.append(command)
        return True

    async def aclose(self) -> None:
        return None
