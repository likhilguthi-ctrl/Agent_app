from __future__ import annotations

from typing import Protocol

from app.modules.messaging.contracts import SendWhatsAppCommand


class MessagingProvider(Protocol):
    async def send_otp(self, mobile_e164: str) -> bool: ...

    async def verify_otp(self, mobile_e164: str, otp: str) -> bool: ...

    async def send_whatsapp(self, command: SendWhatsAppCommand) -> bool: ...

    async def aclose(self) -> None: ...
