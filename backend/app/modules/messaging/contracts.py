from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol


class MessagingOperation(StrEnum):
    SEND_OTP = "send_otp"
    VERIFY_OTP = "verify_otp"
    SEND_WHATSAPP = "send_whatsapp"


@dataclass(frozen=True, slots=True)
class SendOtpCommand:
    mobile_e164: str


@dataclass(frozen=True, slots=True)
class VerifyOtpCommand:
    mobile_e164: str
    otp: str


@dataclass(frozen=True, slots=True)
class SendWhatsAppCommand:
    recipient_e164: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EncodedMessagingRequest:
    body: bytes
    headers: Mapping[str, str]


class MessagingContractCodec(Protocol):
    """The single contract-specific seam for wire serialization and response parsing.

    Implement only after the messaging service publishes its API contract. The
    application and transport layers intentionally know no provider JSON fields.
    """

    def encode(
        self,
        operation: MessagingOperation,
        command: SendOtpCommand | VerifyOtpCommand | SendWhatsAppCommand,
    ) -> EncodedMessagingRequest: ...

    def decode(
        self,
        operation: MessagingOperation,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> bool: ...
