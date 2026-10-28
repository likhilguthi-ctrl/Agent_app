from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.commissions.service import (
    CommissionAttributionService,
    get_commission_attribution_service,
)
from app.modules.payments.client import RazorpayClientAdapter, get_razorpay_adapter
from app.modules.payments.models import (
    PaymentOrder,
    PaymentOrderStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
    PaymentWebhookEvent,
    PaymentWebhookEventStatus,
)
from app.modules.payments.schemas import (
    PaymentOrderCreateRequest,
    PaymentOrderSummary,
    PaymentVerifyRequest,
    PaymentVerifyResponse,
    PaymentWebhookResponse,
)
from app.modules.subscriptions.models import (
    MemberSubscription,
    SubscriptionPaymentStatus,
    SubscriptionStatus,
)

logger = logging.getLogger(__name__)


class PaymentService:
    """Orchestrates Razorpay Orders, Signature Verification, Webhooks, and Subscription Settlement."""

    def __init__(
        self,
        audit_service: AuditService | None = None,
        commission_service: CommissionAttributionService | None = None,
    ) -> None:
        self._audit = audit_service or AuditService()
        self._commissions = commission_service or get_commission_attribution_service()

    def create_payment_order(
        self,
        session: Session,
        tenant_id: UUID,
        caller_membership_id: UUID,
        request: PaymentOrderCreateRequest,
        adapter: RazorpayClientAdapter | None = None,
    ) -> PaymentOrderSummary:
        adapter = adapter or get_razorpay_adapter()

        # 1. Check idempotency if key supplied
        if request.idempotency_key:
            existing_order_stmt = select(PaymentOrder).where(
                PaymentOrder.tenant_id == tenant_id,
                PaymentOrder.idempotency_key == request.idempotency_key,
            )
            existing_order = session.execute(existing_order_stmt).scalar_one_or_none()
            if existing_order is not None:
                return PaymentOrderSummary(
                    id=existing_order.id,
                    tenant_id=existing_order.tenant_id,
                    subscription_id=existing_order.subscription_id,
                    subscriber_membership_id=existing_order.subscriber_membership_id,
                    gateway=existing_order.gateway,
                    gateway_order_id=existing_order.gateway_order_id,
                    amount_paise=existing_order.amount_paise,
                    amount=existing_order.amount,
                    currency=existing_order.currency,
                    status=existing_order.status,
                    key_id=adapter.key_id,
                    created_at=existing_order.created_at,
                )

        # 2. Fetch subscription within caller's tenant
        sub_stmt = select(MemberSubscription).where(
            MemberSubscription.id == request.subscription_id,
            MemberSubscription.tenant_id == tenant_id,
        )
        subscription = session.execute(sub_stmt).scalar_one_or_none()
        if subscription is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBSCRIPTION_NOT_FOUND", "message": "Subscription not found in current community."},
            )

        # 3. Verify subscription is eligible for payment
        if (
            subscription.status != SubscriptionStatus.PENDING_PAYMENT.value
            or subscription.payment_status != SubscriptionPaymentStatus.UNPAID.value
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "SUBSCRIPTION_NOT_PAYABLE",
                    "message": f"Subscription in status '{subscription.status}' with payment '{subscription.payment_status}' is not awaiting payment.",
                },
            )

        # 4. Check if an active/unexpired order already exists for this subscription
        active_order_stmt = (
            select(PaymentOrder)
            .where(
                PaymentOrder.subscription_id == subscription.id,
                PaymentOrder.status.in_([PaymentOrderStatus.CREATED.value, PaymentOrderStatus.ATTEMPTED.value]),
            )
            .order_by(PaymentOrder.created_at.desc())
        )
        active_order = session.execute(active_order_stmt).scalar_one_or_none()
        if active_order is not None:
            return PaymentOrderSummary(
                id=active_order.id,
                tenant_id=active_order.tenant_id,
                subscription_id=active_order.subscription_id,
                subscriber_membership_id=active_order.subscriber_membership_id,
                gateway=active_order.gateway,
                gateway_order_id=active_order.gateway_order_id,
                amount_paise=active_order.amount_paise,
                amount=active_order.amount,
                currency=active_order.currency,
                status=active_order.status,
                key_id=adapter.key_id,
                created_at=active_order.created_at,
            )

        # 5. Exact conversion of agreed decimal INR to integer paise (no floats allowed)
        paise_decimal = (subscription.agreed_amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        amount_paise = int(paise_decimal)
        if amount_paise <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "INVALID_AMOUNT", "message": "Subscription amount in paise must be positive."},
            )

        # 6. Create Razorpay order via server-side API
        receipt = f"sub_{subscription.id.hex[:10]}"
        order_notes = {
            "subscription_id": str(subscription.id),
            "tenant_id": str(tenant_id),
            "subscriber_membership_id": str(subscription.subscriber_membership_id),
        }
        if request.notes:
            order_notes.update(request.notes)

        rzp_order = adapter.create_order(
            amount_paise=amount_paise,
            currency=subscription.currency,
            receipt=receipt,
            notes=order_notes,
        )
        gateway_order_id = rzp_order["id"]

        now = datetime.now(UTC)
        # 7. Persist payment order record
        payment_order = PaymentOrder(
            tenant_id=tenant_id,
            subscription_id=subscription.id,
            subscriber_membership_id=subscription.subscriber_membership_id,
            gateway="razorpay",
            gateway_order_id=gateway_order_id,
            amount_paise=amount_paise,
            amount=subscription.agreed_amount,
            currency=subscription.currency,
            status=PaymentOrderStatus.CREATED.value,
            idempotency_key=request.idempotency_key,
            notes=order_notes,
            created_by_membership_id=caller_membership_id,
            created_at=now,
            updated_at=now,
        )
        session.add(payment_order)
        session.commit()
        session.refresh(payment_order)

        return PaymentOrderSummary(
            id=payment_order.id,
            tenant_id=payment_order.tenant_id,
            subscription_id=payment_order.subscription_id,
            subscriber_membership_id=payment_order.subscriber_membership_id,
            gateway=payment_order.gateway,
            gateway_order_id=payment_order.gateway_order_id,
            amount_paise=payment_order.amount_paise,
            amount=payment_order.amount,
            currency=payment_order.currency,
            status=payment_order.status,
            key_id=adapter.key_id,
            created_at=payment_order.created_at,
        )

    def verify_checkout_payment(
        self,
        session: Session,
        tenant_id: UUID,
        caller_membership_id: UUID,
        request: PaymentVerifyRequest,
        adapter: RazorpayClientAdapter | None = None,
    ) -> PaymentVerifyResponse:
        adapter = adapter or get_razorpay_adapter()

        # 1. Authoritative lookup of expected order from database in caller's tenant
        order_stmt = select(PaymentOrder).where(
            PaymentOrder.gateway_order_id == request.razorpay_order_id,
            PaymentOrder.tenant_id == tenant_id,
        )
        order = session.execute(order_stmt).scalar_one_or_none()
        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "ORDER_NOT_FOUND", "message": "Razorpay order not found for this community."},
            )

        # 2. Verify Razorpay cryptographic signature server-side
        sig_valid = adapter.verify_payment_signature(
            order_id=request.razorpay_order_id,
            payment_id=request.razorpay_payment_id,
            signature=request.razorpay_signature,
        )
        if not sig_valid:
            logger.warning(
                f"Invalid payment signature for order {request.razorpay_order_id}, payment {request.razorpay_payment_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_SIGNATURE", "message": "Cryptographic payment signature verification failed."},
            )

        # 3. If live/non-mock client, verify payment capture, amount, and currency via Razorpay API
        if not adapter.is_mock:
            payment_info = adapter.fetch_payment(request.razorpay_payment_id)
            if payment_info.get("status") != "captured" and not payment_info.get("captured"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "PAYMENT_NOT_CAPTURED", "message": "Payment is not in captured state."},
                )
            if payment_info.get("amount") != order.amount_paise:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "AMOUNT_MISMATCH", "message": "Captured amount does not match expected order amount."},
                )
            if payment_info.get("currency") != order.currency:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "CURRENCY_MISMATCH", "message": "Captured currency does not match order currency."},
                )

        # 4. Process authoritative transactional settlement
        return self.process_captured_payment(
            session=session,
            order=order,
            payment_id=request.razorpay_payment_id,
            signature=request.razorpay_signature,
            actor=f"membership:{caller_membership_id}",
        )

    def process_captured_payment(
        self,
        session: Session,
        order: PaymentOrder,
        payment_id: str,
        signature: str | None = None,
        captured_at: datetime | None = None,
        actor: str = "system:payment_service",
    ) -> PaymentVerifyResponse:
        """Atomic, idempotent processing of verified captured payment."""
        now = datetime.now(UTC)

        # 1. Check if this payment_id was already processed (idempotency guard)
        existing_tx_stmt = select(PaymentTransaction).where(
            PaymentTransaction.gateway == "razorpay",
            PaymentTransaction.gateway_payment_id == payment_id,
        )
        existing_tx = session.execute(existing_tx_stmt).scalar_one_or_none()
        if existing_tx is not None and existing_tx.processed_at is not None:
            # Already settled and activated - return idempotent response
            sub = session.get(MemberSubscription, existing_tx.subscription_id)
            return PaymentVerifyResponse(
                success=True,
                payment_id=existing_tx.gateway_payment_id,
                order_id=order.gateway_order_id,
                subscription_id=existing_tx.subscription_id,
                subscription_status=sub.status if sub else SubscriptionStatus.ACTIVE.value,
                payment_status=sub.payment_status if sub else SubscriptionPaymentStatus.SETTLED.value,
                settled_at=existing_tx.processed_at,
            )

        # 2. Lock subscription row for update to serialize state transitions
        sub_stmt = (
            select(MemberSubscription)
            .where(MemberSubscription.id == order.subscription_id)
            .with_for_update()
        )
        subscription = session.execute(sub_stmt).scalar_one_or_none()
        if subscription is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBSCRIPTION_NOT_FOUND", "message": "Associated subscription not found."},
            )

        # 3. Update payment order status to 'paid'
        order.status = PaymentOrderStatus.PAID.value
        order.updated_at = now

        # 4. Insert payment transaction record
        tx = PaymentTransaction(
            tenant_id=order.tenant_id,
            payment_order_id=order.id,
            subscription_id=subscription.id,
            gateway="razorpay",
            gateway_payment_id=payment_id,
            amount_paise=order.amount_paise,
            amount=order.amount,
            currency=order.currency,
            status=PaymentTransactionStatus.CAPTURED.value,
            gateway_signature=signature,
            captured_at=captured_at or now,
            processed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(tx)

        # 5. Activate subscription and record payment settled
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.payment_status = SubscriptionPaymentStatus.SETTLED.value
        subscription.starts_at = now
        subscription.expires_at = now + timedelta(days=subscription.snapshot_duration_days)
        subscription.updated_at = now

        # 6. Structured audit log
        actor_cat = "system" if "system" in actor else "tenant_member"
        actor_ref = actor if actor_cat == "system" else actor.replace("membership:", "")
        self._audit.record_mandatory(
            session=session,
            draft=AuditEventDraft(
                event_type="subscription.payment.settled",
                actor_category=actor_cat,
                actor_ref=actor_ref,
                action="subscription.payment.settled",
                outcome="success",
                tenant_id=order.tenant_id,
                target_entity_type="subscription",
                target_entity_ref=str(subscription.id),
            ),
        )

        # 7. Attribute multi-tier referral commissions to financial ledger
        session.flush()
        try:
            self._commissions.attribute_subscription_commissions(
                session=session,
                tenant_id=order.tenant_id,
                subscription=subscription,
                payment_transaction=tx,
            )
        except Exception as exc:
            logger.error(
                "Failed to attribute referral commissions for subscription %s: %s",
                subscription.id,
                exc,
                exc_info=True,
            )
            raise

        session.commit()
        session.refresh(subscription)

        return PaymentVerifyResponse(
            success=True,
            payment_id=payment_id,
            order_id=order.gateway_order_id,
            subscription_id=subscription.id,
            subscription_status=subscription.status,
            payment_status=subscription.payment_status,
            settled_at=now,
        )

    def process_webhook(
        self,
        session: Session,
        raw_body: bytes,
        signature: str,
        adapter: RazorpayClientAdapter | None = None,
    ) -> PaymentWebhookResponse:
        adapter = adapter or get_razorpay_adapter()

        # 1. Cryptographic raw body signature verification
        if not adapter.verify_webhook_signature(body=raw_body, signature=signature):
            logger.warning("Rejected webhook with invalid X-Razorpay-Signature.")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_SIGNATURE", "message": "Webhook signature verification failed."},
            )

        # 2. Parse payload safely
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_PAYLOAD", "message": "Malformed JSON payload."},
            ) from exc

        event_id = payload.get("id") or f"evt_{hash(raw_body)}"
        event_type = payload.get("event", "unknown")

        # 3. Durable database deduplication check
        existing_stmt = select(PaymentWebhookEvent).where(
            PaymentWebhookEvent.gateway == "razorpay",
            PaymentWebhookEvent.event_id == event_id,
        )
        existing_event = session.execute(existing_stmt).scalar_one_or_none()
        if existing_event is not None and existing_event.status == PaymentWebhookEventStatus.PROCESSED.value:
            return PaymentWebhookResponse(
                status="duplicate",
                event_id=event_id,
                event_type=event_type,
            )

        # Record incoming event
        now = datetime.now(UTC)
        webhook_event = existing_event or PaymentWebhookEvent(
            gateway="razorpay",
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            status=PaymentWebhookEventStatus.RECEIVED.value,
            created_at=now,
        )
        if existing_event is None:
            session.add(webhook_event)
            session.flush()

        # 4. Handle supported events
        if event_type == "payment.captured":
            payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
            payment_id = payment_entity.get("id")
            order_id = payment_entity.get("order_id")

            if order_id and payment_id:
                order_stmt = select(PaymentOrder).where(PaymentOrder.gateway_order_id == order_id)
                order = session.execute(order_stmt).scalar_one_or_none()

                if order is not None:
                    self.process_captured_payment(
                        session=session,
                        order=order,
                        payment_id=payment_id,
                        captured_at=now,
                        actor="system:razorpay_webhook",
                    )
            webhook_event.status = PaymentWebhookEventStatus.PROCESSED.value
            webhook_event.processed_at = now
        elif event_type == "payment.failed":
            payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
            order_id = payment_entity.get("order_id")
            payment_id = payment_entity.get("id")

            if order_id and payment_id:
                order_stmt = select(PaymentOrder).where(PaymentOrder.gateway_order_id == order_id)
                order = session.execute(order_stmt).scalar_one_or_none()

                if order is not None:
                    # Record failed transaction attempt
                    failed_tx = PaymentTransaction(
                        tenant_id=order.tenant_id,
                        payment_order_id=order.id,
                        subscription_id=order.subscription_id,
                        gateway="razorpay",
                        gateway_payment_id=payment_id,
                        amount_paise=payment_entity.get("amount", order.amount_paise),
                        amount=order.amount,
                        currency=order.currency,
                        status=PaymentTransactionStatus.FAILED.value,
                        error_code=payment_entity.get("error_code"),
                        error_description=payment_entity.get("error_description"),
                        processed_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(failed_tx)
            webhook_event.status = PaymentWebhookEventStatus.PROCESSED.value
            webhook_event.processed_at = now
        else:
            # Other informational webhook events
            webhook_event.status = PaymentWebhookEventStatus.IGNORED.value
            webhook_event.processed_at = now

        session.commit()

        return PaymentWebhookResponse(
            status="processed",
            event_id=event_id,
            event_type=event_type,
        )


_default_payment_service: PaymentService | None = None


def get_payment_service() -> PaymentService:
    global _default_payment_service
    if _default_payment_service is None:
        _default_payment_service = PaymentService()
    return _default_payment_service
