from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import sys
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.messaging.models import NotificationWebhookEvent


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    msg = timestamp.encode("utf-8") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def main() -> int:
    settings = get_settings()
    if not settings.notification_webhook_secret:
        print("[FAIL] notification_webhook_secret is not configured.")
        return 1

    secret = settings.notification_webhook_secret.get_secret_value()
    tunnel_url = settings.effective_notification_webhook_callback_url
    print(f"[INFO] Target Webhook URL: {tunnel_url}")

    results = []
    evt_id = f"evt_live_test_{uuid4().hex[:12]}"
    msg_id = f"msg_live_test_{uuid4().hex[:12]}"
    now = datetime.now(UTC)
    ts = str(int(now.timestamp()))

    payload = {
        "eventId": evt_id,
        "eventType": "message.status_changed",
        "serviceId": settings.notification_service_id or "community-ott",
        "messageId": msg_id,
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = compute_signature(secret, ts, raw_body)

    # 1. Valid signed event test
    print("\n--- Test 1: Valid Signed Webhook Event ---")
    with httpx.Client(timeout=15.0) as client:
        r1 = client.post(
            tunnel_url,
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig,
                "X-Event-ID": evt_id,
            },
        )
        print(f"Status Code: {r1.status_code}")
        print(f"Response: {r1.text}")
        if r1.status_code == 200 and r1.json().get("status") in {"processed", "received"}:
            print("[PASS] Valid event accepted via HTTPS Cloudflare tunnel.")
            results.append(("Valid Event Dispatch", "PASS", r1.status_code))
        else:
            print("[FAIL] Unexpected response for valid event.")
            results.append(("Valid Event Dispatch", "FAIL", r1.status_code))

        # 2. Duplicate event test
        print("\n--- Test 2: Duplicate Event ID (Idempotency) ---")
        r2 = client.post(
            tunnel_url,
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": sig,
                "X-Event-ID": evt_id,
            },
        )
        print(f"Status Code: {r2.status_code}")
        print(f"Response: {r2.text}")
        if r2.status_code == 200 and r2.json().get("status") == "duplicate":
            print("[PASS] Duplicate event idempotently handled (status=duplicate).")
            results.append(("Duplicate Event Idempotency", "PASS", r2.status_code))
        else:
            print("[FAIL] Unexpected response for duplicate event.")
            results.append(("Duplicate Event Idempotency", "FAIL", r2.status_code))

        # 3. Invalid signature test
        print("\n--- Test 3: Invalid HMAC-SHA256 Signature ---")
        bad_sig = compute_signature("wrong_signing_secret_for_probe_only", ts, raw_body)
        r3 = client.post(
            tunnel_url,
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": ts,
                "X-Notification-Signature": bad_sig,
                "X-Event-ID": evt_id,
            },
        )
        print(f"Status Code: {r3.status_code}")
        print(f"Response: {r3.text}")
        if r3.status_code == 401:
            print("[PASS] Invalid signature rejected with 401 Unauthorized.")
            results.append(("Invalid Signature Rejection", "PASS", r3.status_code))
        else:
            print("[FAIL] Expected 401 for invalid signature.")
            results.append(("Invalid Signature Rejection", "FAIL", r3.status_code))

        # 4. Expired timestamp test
        print("\n--- Test 4: Expired Timestamp (> 300s) ---")
        stale_time = now - timedelta(seconds=350)
        stale_ts = str(int(stale_time.timestamp()))
        stale_sig = compute_signature(secret, stale_ts, raw_body)
        r4 = client.post(
            tunnel_url,
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Notification-Timestamp": stale_ts,
                "X-Notification-Signature": stale_sig,
                "X-Event-ID": evt_id,
            },
        )
        print(f"Status Code: {r4.status_code}")
        print(f"Response: {r4.text}")
        if r4.status_code == 400:
            print("[PASS] Expired timestamp rejected with 400 Bad Request.")
            results.append(("Expired Timestamp Rejection", "PASS", r4.status_code))
        else:
            print("[FAIL] Expected 400 for expired timestamp.")
            results.append(("Expired Timestamp Rejection", "FAIL", r4.status_code))

    # 5. Database persistence verification
    print("\n--- Test 5: PostgreSQL Database Persistence Check ---")
    if settings.database_url:
        engine = create_engine(settings.database_url.get_secret_value())
        with Session(engine) as session:
            db_record = session.execute(
                select(NotificationWebhookEvent).where(NotificationWebhookEvent.event_id == evt_id)
            ).scalar_one_or_none()
            if db_record:
                print(f"[PASS] Webhook event found in PostgreSQL: id={db_record.id}, event_id={db_record.event_id}, status={db_record.status}, message_id={db_record.message_id}")
                results.append(("PostgreSQL Persistence Check", "PASS", 200))
            else:
                print(f"[FAIL] Webhook event {evt_id} NOT found in database.")
                results.append(("PostgreSQL Persistence Check", "FAIL", 0))

    print("\n" + "=" * 50)
    print("LIVE CLOUDFLARE HTTPS TUNNEL TEST SUMMARY:")
    all_passed = True
    for name, status, code in results:
        print(f"  - {name}: {status} (HTTP {code})")
        if status != "PASS":
            all_passed = False
    print("=" * 50)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

