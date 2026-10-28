from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.modules.agents.models import SubscriberProfile
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.identity.models import TenantMembership
from app.modules.messaging.notification_service import NotificationServiceClient
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
from app.modules.payments.service import PaymentService, get_payment_service
from app.modules.subscriptions.models import (
    MemberSubscription,
    SubscriptionPaymentStatus,
    SubscriptionStatus,
)
from app.modules.tenants.models import Tenant

logger = logging.getLogger(__name__)

payments_router = APIRouter(prefix="/payments", tags=["payments"])
payment_webhooks_router = APIRouter(prefix="/webhooks/payments", tags=["payment_webhooks"])


@payments_router.post(
    "/orders",
    response_model=PaymentOrderSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Razorpay payment order for an eligible subscription",
)
def create_payment_order(
    request: PaymentOrderCreateRequest,
    caller: Annotated[
        ResolvedSession,
        Depends(require_session_capability(PermissionCode.PAYMENT_MANAGE)),
    ],
    db: Session = Depends(get_db),
    service: PaymentService = Depends(get_payment_service),
) -> PaymentOrderSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "TENANT_REQUIRED", "message": "Tenant context required for payment orders."},
        )

    return service.create_payment_order(
        session=db,
        tenant_id=caller.tenant_id,
        caller_membership_id=caller.membership_id,
        request=request,
    )


@payments_router.post(
    "/verify",
    response_model=PaymentVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify Razorpay Checkout payment response and activate subscription",
)
def verify_payment(
    request: PaymentVerifyRequest,
    caller: Annotated[
        ResolvedSession,
        Depends(require_session_capability(PermissionCode.PAYMENT_MANAGE)),
    ],
    db: Session = Depends(get_db),
    service: PaymentService = Depends(get_payment_service),
) -> PaymentVerifyResponse:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "TENANT_REQUIRED", "message": "Tenant context required for payment verification."},
        )

    return service.verify_checkout_payment(
        session=db,
        tenant_id=caller.tenant_id,
        caller_membership_id=caller.membership_id,
        request=request,
    )


@payments_router.post(
    "/webhook",
    response_model=PaymentWebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Razorpay server-to-server webhook callback",
)
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: Annotated[str | None, Header(alias="X-Razorpay-Signature")] = None,
    db: Session = Depends(get_db),
    service: PaymentService = Depends(get_payment_service),
) -> PaymentWebhookResponse:
    if not x_razorpay_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "MISSING_SIGNATURE", "message": "X-Razorpay-Signature header is required."},
        )

    raw_body = await request.body()
    return service.process_webhook(
        session=db,
        raw_body=raw_body,
        signature=x_razorpay_signature,
    )


@payment_webhooks_router.post(
    "/status",
    response_model=PaymentWebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Payment Microservice server-to-server status webhook callback",
)
async def receive_payment_status_webhook(
    request: Request,
    x_payment_signature: Annotated[str | None, Header(alias="X-Payment-Signature")] = None,
    x_signature: Annotated[str | None, Header(alias="X-Signature")] = None,
    x_payment_timestamp: Annotated[str | None, Header(alias="X-Payment-Timestamp")] = None,
    x_event_id: Annotated[str | None, Header(alias="X-Event-ID")] = None,
    db: Session = Depends(get_db),
    service: PaymentService = Depends(get_payment_service),
) -> PaymentWebhookResponse:
    """Receive, cryptographically verify, and process status callbacks from Payment Microservice."""
    signature = x_payment_signature or x_signature
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "MISSING_SIGNATURE", "message": "X-Payment-Signature header is required."},
        )

    raw_body = await request.body()
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()

    # Resolve webhook signing secret
    secret: str | None = None
    if settings.payment_service_webhook_secret is not None:
        val = settings.payment_service_webhook_secret.get_secret_value().strip()
        if val:
            secret = val
    if not secret and settings.razorpay_webhook_secret is not None:
        val = settings.razorpay_webhook_secret.get_secret_value().strip()
        if val:
            secret = val

    if not secret:
        if settings.environment in ("development", "test"):
            secret = "dev_payment_webhook_secret_default"
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "WEBHOOK_SECRET_NOT_CONFIGURED", "message": "Payment webhook secret is not configured."},
            )

    # 1. Cryptographic raw body signature verification (HMAC-SHA256)
    computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed.lower(), signature.strip().lower()):
        logger.warning("Rejected payment webhook with invalid signature.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_SIGNATURE", "message": "Payment webhook signature verification failed."},
        )

    # 2. Parse payload safely
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_PAYLOAD", "message": "Malformed JSON payload."},
        ) from exc

    event_id = (
        x_event_id
        or payload.get("event_id")
        or payload.get("id")
        or f"evt_{hashlib.sha256(raw_body).hexdigest()[:16]}"
    )
    event_type = payload.get("event_type") or payload.get("event") or "payment.status_update"

    # 3. Durable database deduplication check
    existing_stmt = select(PaymentWebhookEvent).where(
        PaymentWebhookEvent.gateway == "payment_microservice",
        PaymentWebhookEvent.event_id == event_id,
    )
    existing_event = db.execute(existing_stmt).scalar_one_or_none()
    if existing_event is not None and existing_event.status == PaymentWebhookEventStatus.PROCESSED.value:
        return PaymentWebhookResponse(
            status="duplicate",
            event_id=event_id,
            event_type=event_type,
        )

    now = datetime.now(UTC)
    webhook_event = existing_event or PaymentWebhookEvent(
        gateway="payment_microservice",
        event_id=event_id,
        event_type=event_type,
        payload=payload,
        status=PaymentWebhookEventStatus.RECEIVED.value,
        created_at=now,
    )
    if existing_event is None:
        db.add(webhook_event)
        db.flush()

    # 4. Entity resolution (Order)
    payment_id = payload.get("payment_id") or payload.get("paymentId")
    order_id = payload.get("order_id") or payload.get("orderId")

    query = select(PaymentOrder)
    if payment_id:
        query = query.where(PaymentOrder.external_payment_id == payment_id)
    elif order_id:
        query = query.where(PaymentOrder.gateway_order_id == order_id)
    else:
        webhook_event.status = PaymentWebhookEventStatus.FAILED.value
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "MISSING_IDENTIFIER", "message": "Neither payment_id nor order_id was provided in webhook."},
        )

    order = db.execute(query).scalar_one_or_none()
    if not order:
        webhook_event.status = PaymentWebhookEventStatus.FAILED.value
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "PAYMENT_ORDER_NOT_FOUND", "message": "Payment order could not be found."},
        )

    # 5. Cross-tenant isolation verification
    payload_tenant = payload.get("tenant_id") or payload.get("tenant") or payload.get("tenantId")
    if payload_tenant:
        tenant_matches = False
        if str(order.tenant_id) == str(payload_tenant):
            tenant_matches = True
        else:
            t_slug = db.execute(select(Tenant.slug).where(Tenant.id == order.tenant_id)).scalar()
            if t_slug and t_slug == str(payload_tenant):
                tenant_matches = True
        if not tenant_matches:
            webhook_event.status = PaymentWebhookEventStatus.FAILED.value
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "CROSS_TENANT_PAYMENT_REJECTED", "message": "Cross-tenant payment rejected."},
            )

    # 6. Altered amount and currency validation
    payload_currency = payload.get("currency")
    if payload_currency and order.currency:
        if payload_currency.strip().upper() != order.currency.strip().upper():
            webhook_event.status = PaymentWebhookEventStatus.FAILED.value
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "CURRENCY_MISMATCH", "message": f"Currency mismatch: expected {order.currency}, got {payload_currency}."},
            )

    payload_amount = payload.get("amountInPaise") or payload.get("amount_paise") or payload.get("amount")
    if payload_amount is not None:
        try:
            amt_num = float(payload_amount)
            if abs(amt_num - order.amount_paise) <= 10 or abs((amt_num * 100) - order.amount_paise) <= 10:
                pass
            else:
                webhook_event.status = PaymentWebhookEventStatus.FAILED.value
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "AMOUNT_MISMATCH", "message": "Payment amount does not match order record."},
                )
        except (ValueError, TypeError):
            pass

    # 7. Check if already settled (Idempotency)
    if order.status == PaymentOrderStatus.PAID.value:
        webhook_event.status = PaymentWebhookEventStatus.PROCESSED.value
        webhook_event.processed_at = now
        db.commit()
        return PaymentWebhookResponse(
            status="already_settled",
            event_id=event_id,
            event_type=event_type,
        )

    # 8. Event status transitions
    event_status = str(payload.get("status") or "").upper()
    is_completed = (
        event_status in ("COMPLETED", "PAID", "SUCCESS", "CAPTURED")
        or event_type in ("payment.completed", "payment.captured", "order.paid")
    )

    if is_completed:
        order.status = PaymentOrderStatus.PAID.value
        order.updated_at = now

        sub = db.execute(
            select(MemberSubscription).where(MemberSubscription.id == order.subscription_id)
        ).scalar_one_or_none()
        if sub:
            sub.status = SubscriptionStatus.ACTIVE.value
            sub.payment_status = SubscriptionPaymentStatus.SETTLED.value
            sub.starts_at = now
            sub.expires_at = now + timedelta(days=sub.snapshot_duration_days or 30)
            sub.updated_at = now

        tx = PaymentTransaction(
            tenant_id=order.tenant_id,
            payment_order_id=order.id,
            subscription_id=order.subscription_id,
            gateway="payment_microservice",
            gateway_payment_id=payment_id or order.external_payment_id or order.gateway_order_id,
            amount_paise=order.amount_paise,
            amount=order.amount,
            currency=order.currency,
            status=PaymentTransactionStatus.CAPTURED.value,
            captured_at=now,
        )
        db.add(tx)
        db.flush()

        try:
            service._commissions.attribute_subscription_commissions(
                session=db,
                tenant_id=order.tenant_id,
                subscription=sub,
                payment_transaction=tx,
            )
        except Exception as exc:
            logger.warning("Commission attribution failed during payment webhook: %s", exc)

        # Multi-channel welcome notification (Strict WhatsApp consent)
        order_notes = dict(order.notes or {})
        if not order_notes.get("welcome_notified"):
            sub_prof = db.execute(
                select(
                    SubscriberProfile,
                    TenantMembership.mobile_e164,
                    Tenant.slug.label("tenant_slug"),
                )
                .join(TenantMembership, TenantMembership.id == SubscriberProfile.membership_id)
                .join(Tenant, Tenant.id == SubscriberProfile.tenant_id)
                .where(SubscriberProfile.membership_id == order.subscriber_membership_id)
            ).first()

            if sub_prof:
                subscriber, mobile, t_slug = sub_prof
                notif_client = NotificationServiceClient(settings=settings)
                try:
                    await notif_client.send_subscriber_welcome(
                        subscriber_name=subscriber.full_name,
                        mobile_e164=mobile,
                        whatsapp_number=subscriber.whatsapp_number,
                        whatsapp_consent=subscriber.whatsapp_consent,
                        email=subscriber.email,
                        plan_name=sub.snapshot_plan_name if sub else "Standard",
                        amount_paid=order.amount,
                        community_name=t_slug or "Community OTT",
                    )
                    order_notes["welcome_notified"] = True
                    order_notes["welcome_notified_at"] = now.isoformat()
                    order.notes = order_notes
                except Exception as notif_exc:
                    logger.warning("Welcome notification failed during payment webhook: %s", notif_exc)

        webhook_event.status = PaymentWebhookEventStatus.PROCESSED.value
        webhook_event.processed_at = now
        db.commit()

        return PaymentWebhookResponse(
            status="processed",
            event_id=event_id,
            event_type=event_type,
        )
    elif event_status in ("FAILED", "FAILURE", "REJECTED") or event_type in ("payment.failed",):
        order.status = PaymentOrderStatus.FAILED.value
        order.updated_at = now
        tx = PaymentTransaction(
            tenant_id=order.tenant_id,
            payment_order_id=order.id,
            subscription_id=order.subscription_id,
            gateway="payment_microservice",
            gateway_payment_id=payment_id or order.external_payment_id or order.gateway_order_id,
            amount_paise=order.amount_paise,
            amount=order.amount,
            currency=order.currency,
            status=PaymentTransactionStatus.FAILED.value,
        )
        db.add(tx)
        webhook_event.status = PaymentWebhookEventStatus.PROCESSED.value
        webhook_event.processed_at = now
        db.commit()

        return PaymentWebhookResponse(
            status="failed_recorded",
            event_id=event_id,
            event_type=event_type,
        )
    else:
        webhook_event.status = PaymentWebhookEventStatus.IGNORED.value
        webhook_event.processed_at = now
        db.commit()
        return PaymentWebhookResponse(
            status="ignored",
            event_id=event_id,
            event_type=event_type,
        )

