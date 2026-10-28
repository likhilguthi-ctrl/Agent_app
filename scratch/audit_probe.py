from __future__ import annotations

import hashlib
import hmac
import json
import sys
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from app.core.config import get_settings


def compute_sig(secret: str, ts: str, body: bytes) -> str:
    msg = ts.encode("utf-8") + b"." + body
    d = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={d}"


def probe():
    settings = get_settings()
    secret = settings.notification_webhook_secret.get_secret_value() if settings.notification_webhook_secret else ""
    print(f"Secret configured: {bool(secret)}")

    now = datetime.now(UTC)
    ts = str(int(now.timestamp()))
    evt_id = f"evt_probe_{uuid4().hex[:10]}"
    payload = {
        "eventId": evt_id,
        "eventType": "message.status_changed",
        "serviceId": "community-ott",
        "messageId": f"msg_probe_{uuid4().hex[:10]}",
        "status": "DELIVERED",
        "channel": "WHATSAPP",
        "occurredAt": now.isoformat(),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = compute_sig(secret, ts, raw_body)

    # 1. Test GET on 8000
    r_get = httpx.get("http://127.0.0.1:8000/api/v1/webhooks/notifications/status", timeout=5)
    print(f"GET 8000: {r_get.status_code}")

    # 2. Test GET on 5000
    r_get_5000 = httpx.get("http://127.0.0.1:5000/backend-api/api/v1/webhooks/notifications/status", timeout=5)
    print(f"GET 5000: {r_get_5000.status_code}")

    # 3. Test POST on 8000 (direct)
    r_post_8000 = httpx.post(
        "http://127.0.0.1:8000/api/v1/webhooks/notifications/status",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": ts,
            "X-Notification-Signature": sig,
            "X-Event-ID": evt_id,
        },
        timeout=5,
    )
    print(f"POST 8000: {r_post_8000.status_code} {r_post_8000.text}")

    # 4. Test POST on 5000 (proxy)
    evt_id_5000 = f"evt_probe_5000_{uuid4().hex[:10]}"
    payload["eventId"] = evt_id_5000
    raw_body_5000 = json.dumps(payload).encode("utf-8")
    sig_5000 = compute_sig(secret, ts, raw_body_5000)

    r_post_5000 = httpx.post(
        "http://127.0.0.1:5000/backend-api/api/v1/webhooks/notifications/status",
        content=raw_body_5000,
        headers={
            "Content-Type": "application/json",
            "X-Notification-Timestamp": ts,
            "X-Notification-Signature": sig_5000,
            "X-Event-ID": evt_id_5000,
        },
        timeout=5,
    )
    print(f"POST 5000: {r_post_5000.status_code} {r_post_5000.text}")


if __name__ == "__main__":
    probe()

