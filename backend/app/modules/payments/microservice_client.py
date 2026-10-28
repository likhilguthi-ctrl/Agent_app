from __future__ import annotations

import hmac
import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PaymentInitResult:
    success: bool
    payment_id: str
    order_id: str
    payment_page_url: str
    status: str
    amount: int
    currency: str
    tenant_id: str
    error: str | None = None
    raw_response: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PaymentStatusResult:
    success: bool
    payment_id: str
    status: str  # 'INITIATED', 'COMPLETED', 'FAILED', 'EXPIRED'
    order_id: str | None = None
    amount: int | None = None
    currency: str | None = None
    tenant_id: str | None = None
    is_authoritative: bool = False
    source_endpoint: str | None = None
    error: str | None = None
    raw_response: dict[str, Any] | None = None


class PaymentMicroserviceClient:
    """Client for Payment Microservice integration adhering to FRONTEND_INTEGRATION_PAYMENT.md."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._mock_orders: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return self._settings.payment_base_url

    @property
    def api_key(self) -> str | None:
        if self._settings.payment_service_api_key is not None:
            val = self._settings.payment_service_api_key.get_secret_value().strip()
            if val:
                return val
        if self._settings.notification_service_api_key is not None:
            val = self._settings.notification_service_api_key.get_secret_value().strip()
            if val:
                return val
        return None

    @property
    def init_endpoint_url(self) -> str:
        return f"{self.base_url}/api/v1/payments/init"

    async def initiate_payment(
        self,
        *,
        tenant_slug: str,
        amount_paise: int,
        description: str,
        callback_url: str,
        customer_name: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PaymentInitResult:
        """Initiate payment session (Hosted Checkout Flow A)."""
        payload: dict[str, Any] = {
            "amount": float(amount_paise) / 100.0,
            "amountInPaise": amount_paise,
            "tenant": tenant_slug,
            "currency": "INR",
            "description": description,
            "customer_details": {
                "name": customer_name or "",
                "phone": customer_phone or "",
                "email": customer_email or "",
            },
            "callback_url": callback_url,
            "idempotencyKey": idempotency_key or str(uuid4()),
            "metadate": metadata or {},
        }

        # Strict Production/Staging check: mock mode strictly forbidden in staging/production
        is_mock_allowed = (
            self._settings.environment in ("development", "test")
            and (self._settings.payment_delivery_mode == "mock" or self._settings.environment == "test")
        )
        if is_mock_allowed:
            mock_payment_id = f"pay_mock_{uuid4().hex[:16]}"
            mock_order_id = f"order_mock_{uuid4().hex[:14]}"
            mock_url = f"{self.base_url}/checkout/{mock_payment_id}"
            result = PaymentInitResult(
                success=True,
                payment_id=mock_payment_id,
                order_id=mock_order_id,
                payment_page_url=mock_url,
                status="INITIATED",
                amount=amount_paise,
                currency="INR",
                tenant_id=tenant_slug,
                raw_response={"mock": True},
            )
            self._mock_orders.append(payload)
            return result

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            should_close = False
            client = self._client
            if client is None:
                client = httpx.AsyncClient(timeout=10.0)
                should_close = True

            try:
                response = await client.post(
                    self.init_endpoint_url,
                    json=payload,
                    headers=headers,
                )
                if response.status_code in (200, 201):
                    data = response.json()
                    payment_id = str(data.get("payment_id") or data.get("paymentId") or "")
                    order_id = str(data.get("order_id") or data.get("orderId") or "")
                    payment_page_url = str(
                        data.get("payment_page_url")
                        or f"{self.base_url}/checkout/{payment_id}"
                    )
                    return PaymentInitResult(
                        success=True,
                        payment_id=payment_id,
                        order_id=order_id,
                        payment_page_url=payment_page_url,
                        status=data.get("status", "INITIATED"),
                        amount=data.get("amount", amount_paise),
                        currency=data.get("currency", "INR"),
                        tenant_id=data.get("tenant_id", tenant_slug),
                        raw_response=data,
                    )
                else:
                    logger.warning(
                        "payment_microservice_init_failed",
                        extra={
                            "status_code": response.status_code,
                            "body": response.text[:500],
                            "tenant": tenant_slug,
                        },
                    )
                    # If development / staging returns TENANT_NOT_FOUND or 400 because tenant isn't pre-configured in remote DB
                    if self._settings.environment == "development":
                        mock_payment_id = f"pay_dev_{uuid4().hex[:16]}"
                        mock_order_id = f"order_dev_{uuid4().hex[:14]}"
                        mock_url = f"{self.base_url}/checkout/{mock_payment_id}"
                        return PaymentInitResult(
                            success=True,
                            payment_id=mock_payment_id,
                            order_id=mock_order_id,
                            payment_page_url=mock_url,
                            status="INITIATED",
                            amount=amount_paise,
                            currency="INR",
                            tenant_id=tenant_slug,
                            raw_response={"dev_fallback": True, "original_status": response.status_code},
                        )

                    return PaymentInitResult(
                        success=False,
                        payment_id="",
                        order_id="",
                        payment_page_url="",
                        status="FAILED",
                        amount=amount_paise,
                        currency="INR",
                        tenant_id=tenant_slug,
                        error=f"Payment Microservice rejected request with status {response.status_code}: {response.text[:200]}",
                    )
            finally:
                if should_close:
                    await client.aclose()
        except Exception as exc:
            logger.error("payment_microservice_init_exception", extra={"error": str(exc)})
            if self._settings.environment in ("development", "test"):
                mock_payment_id = f"pay_dev_{uuid4().hex[:16]}"
                mock_order_id = f"order_dev_{uuid4().hex[:14]}"
                mock_url = f"{self.base_url}/checkout/{mock_payment_id}"
                return PaymentInitResult(
                    success=True,
                    payment_id=mock_payment_id,
                    order_id=mock_order_id,
                    payment_page_url=mock_url,
                    status="INITIATED",
                    amount=amount_paise,
                    currency="INR",
                    tenant_id=tenant_slug,
                    raw_response={"exception_fallback": True, "error": str(exc)},
                )
            return PaymentInitResult(
                success=False,
                payment_id="",
                order_id="",
                payment_page_url="",
                status="FAILED",
                amount=amount_paise,
                currency="INR",
                tenant_id=tenant_slug,
                error=f"Could not connect to Payment Microservice: {exc}",
            )

    async def get_payment_status(self, payment_id: str) -> PaymentStatusResult:
        """Fetch payment status from Payment Microservice.
        
        Attempts authoritative server-to-server endpoints (/api/v1/payments/{payment_id}
        or /api/v1/payments/{payment_id}/status) with X-API-Key authentication before falling
        back to public checkout details. Authoritative endpoints are marked is_authoritative=True,
        whereas public checkout details (/checkout/api/details/{payment_id}) are marked
        is_authoritative=False (non-authoritative for settlement).
        """
        is_mock_allowed = (
            self._settings.environment in ("development", "test")
            and (self._settings.payment_delivery_mode == "mock" or self._settings.environment == "test")
        )
        if payment_id.startswith("pay_mock_") or payment_id.startswith("pay_dev_"):
            if not is_mock_allowed:
                return PaymentStatusResult(
                    success=False,
                    payment_id=payment_id,
                    status="FAILED",
                    is_authoritative=False,
                    error="Mock payment IDs are strictly forbidden in production and staging.",
                )
            return PaymentStatusResult(
                success=True,
                payment_id=payment_id,
                status="COMPLETED",
                is_authoritative=True,
                source_endpoint="mock",
                raw_response={"mock": True},
            )

        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        authoritative_urls = [
            f"{self.base_url}/api/v1/payments/{payment_id}",
            f"{self.base_url}/api/v1/payments/{payment_id}/status",
        ]
        public_checkout_url = f"{self.base_url}/checkout/api/details/{payment_id}"

        should_close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=10.0)
            should_close = True

        try:
            last_error = "Unknown error"
            # 1. Query authoritative server-to-server endpoints first
            for url in authoritative_urls:
                try:
                    res = await client.get(url, headers=headers)
                    if res.status_code == 200:
                        data = res.json()
                        raw_status = str(data.get("status") or "").upper()
                        is_completed = (
                            data.get("isCompleted") is True
                            or raw_status in ("COMPLETED", "PAID", "SUCCESS", "CAPTURED")
                        )
                        if is_completed:
                            status = "COMPLETED"
                        elif raw_status in ("FAILED", "FAILURE", "REJECTED"):
                            status = "FAILED"
                        elif raw_status in ("EXPIRED", "CANCELLED"):
                            status = "EXPIRED"
                        else:
                            status = "INITIATED"

                        order_id = str(data.get("order_id") or data.get("orderId") or "")
                        amount_val = data.get("amountInPaise") or data.get("amount_paise") or data.get("amount")
                        amount_paise: int | None = None
                        if amount_val is not None:
                            try:
                                amount_paise = int(amount_val)
                            except (ValueError, TypeError):
                                pass

                        currency = data.get("currency", "INR")
                        tenant_id = data.get("tenant_id") or data.get("tenant") or data.get("tenantId")

                        return PaymentStatusResult(
                            success=True,
                            payment_id=payment_id,
                            status=status,
                            order_id=order_id or None,
                            amount=amount_paise,
                            currency=currency,
                            tenant_id=str(tenant_id) if tenant_id else None,
                            is_authoritative=True,
                            source_endpoint=url,
                            raw_response=data,
                        )
                    elif res.status_code != 404:
                        last_error = f"Authoritative payment query to {url} returned HTTP {res.status_code}"
                except Exception as endpoint_exc:
                    last_error = f"Error querying authoritative {url}: {endpoint_exc}"

            # 2. Query public checkout details endpoint (Non-authoritative for settlement)
            try:
                res = await client.get(public_checkout_url, headers={"Accept": "application/json"})
                if res.status_code == 200:
                    data = res.json()
                    raw_status = str(data.get("status") or "").upper()
                    is_completed = (
                        data.get("isCompleted") is True
                        or raw_status in ("COMPLETED", "PAID", "SUCCESS", "CAPTURED")
                    )
                    status = "COMPLETED" if is_completed else "INITIATED"
                    order_id = str(data.get("order_id") or data.get("orderId") or "")
                    amount_val = data.get("amountInPaise") or data.get("amount")
                    amount_paise = None
                    if amount_val is not None:
                        try:
                            amount_paise = int(amount_val)
                        except (ValueError, TypeError):
                            pass
                    currency = data.get("currency", "INR")
                    tenant_id = data.get("tenantId") or data.get("tenant_id") or data.get("tenant")

                    return PaymentStatusResult(
                        success=True,
                        payment_id=payment_id,
                        status=status,
                        order_id=order_id or None,
                        amount=amount_paise,
                        currency=currency,
                        tenant_id=str(tenant_id) if tenant_id else None,
                        is_authoritative=False,  # Explicitly NOT authoritative for settlement
                        source_endpoint=public_checkout_url,
                        raw_response=data,
                    )
                elif res.status_code != 404:
                    last_error = f"Public checkout query to {public_checkout_url} returned HTTP {res.status_code}"
            except Exception as endpoint_exc:
                last_error = f"Error querying public checkout {public_checkout_url}: {endpoint_exc}"

            return PaymentStatusResult(
                success=False,
                payment_id=payment_id,
                status="UNKNOWN",
                is_authoritative=False,
                error=last_error,
            )
        finally:
            if should_close:
                await client.aclose()

    @staticmethod
    def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
        """Verify HMAC-SHA256 signature for payment webhooks."""
        computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed.lower(), signature.strip().lower())

