from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any
import uuid

import razorpay
from razorpay.errors import SignatureVerificationError

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class RazorpayClientAdapter:
    """Encapsulates Razorpay Orders, Payments, and Webhook cryptographic verifications."""

    def __init__(
        self,
        key_id: str | None = None,
        key_secret: str | None = None,
        webhook_secret: str | None = None,
        is_mock: bool = False,
    ) -> None:
        settings = get_settings()
        self.key_id = key_id or settings.razorpay_key_id
        secret_obj = settings.razorpay_key_secret
        self.key_secret = key_secret or (secret_obj.get_secret_value() if secret_obj else None)
        wh_secret_obj = settings.razorpay_webhook_secret
        self.webhook_secret = webhook_secret or (wh_secret_obj.get_secret_value() if wh_secret_obj else None)
        self.is_mock = is_mock or not (self.key_id and self.key_secret)

        if not self.is_mock and self.key_id and self.key_secret:
            self._client = razorpay.Client(auth=(self.key_id, self.key_secret))
        else:
            self._client = None

    def create_order(
        self,
        amount_paise: int,
        currency: str = "INR",
        receipt: str | None = None,
        notes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a server-side order with Razorpay."""
        if self._client is not None:
            data = {
                "amount": amount_paise,
                "currency": currency,
                "receipt": receipt or f"rcpt_{uuid.uuid4().hex[:12]}",
                "notes": notes or {},
            }
            try:
                order = self._client.order.create(data=data)
                return order
            except Exception as exc:
                logger.error(f"Razorpay order creation failed: {exc}")
                raise RuntimeError(f"Payment gateway order creation failed: {exc}") from exc

        # Deterministic Mock Mode for local tests when credentials are not configured
        mock_order_id = f"order_mock_{uuid.uuid4().hex[:14]}"
        return {
            "id": mock_order_id,
            "entity": "order",
            "amount": amount_paise,
            "amount_paid": 0,
            "amount_due": amount_paise,
            "currency": currency,
            "receipt": receipt or f"rcpt_{uuid.uuid4().hex[:12]}",
            "status": "created",
            "attempts": 0,
            "notes": notes or {},
        }

    def verify_payment_signature(
        self,
        order_id: str,
        payment_id: str,
        signature: str,
    ) -> bool:
        """Verify checkout payment signature using HMAC-SHA256."""
        secret = self.key_secret or "mock_secret_key"
        expected_msg = f"{order_id}|{payment_id}"
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            expected_msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if hmac.compare_digest(expected_sig, signature):
            return True

        if self._client is not None:
            try:
                return bool(
                    self._client.utility.verify_payment_signature(
                        {
                            "razorpay_order_id": order_id,
                            "razorpay_payment_id": payment_id,
                            "razorpay_signature": signature,
                        }
                    )
                )
            except SignatureVerificationError:
                return False
        return False

    def verify_webhook_signature(
        self,
        body: bytes | str,
        signature: str,
    ) -> bool:
        """Verify Razorpay webhook signature header X-Razorpay-Signature."""
        secret = self.webhook_secret or "mock_webhook_secret"
        raw_bytes = body if isinstance(body, bytes) else body.encode("utf-8")
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            raw_bytes,
            hashlib.sha256,
        ).hexdigest()

        if hmac.compare_digest(expected_sig, signature):
            return True

        if self._client is not None and self.webhook_secret:
            try:
                payload_str = body.decode("utf-8") if isinstance(body, bytes) else body
                return bool(
                    self._client.utility.verify_webhook_signature(
                        payload_str,
                        signature,
                        self.webhook_secret,
                    )
                )
            except SignatureVerificationError:
                return False
        return False

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Fetch payment details from Razorpay to verify capture status."""
        if self._client is not None:
            try:
                return self._client.payment.fetch(payment_id)
            except Exception as exc:
                logger.error(f"Failed to fetch Razorpay payment {payment_id}: {exc}")
                raise RuntimeError(f"Payment gateway verification failed: {exc}") from exc

        # Mock payment verification return
        return {
            "id": payment_id,
            "entity": "payment",
            "amount": 10000,
            "currency": "INR",
            "status": "captured",
            "captured": True,
        }


# Global adapter instance
_default_adapter: RazorpayClientAdapter | None = None


def get_razorpay_adapter() -> RazorpayClientAdapter:
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = RazorpayClientAdapter()
    return _default_adapter

