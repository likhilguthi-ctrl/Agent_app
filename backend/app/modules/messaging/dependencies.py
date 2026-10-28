from __future__ import annotations

from fastapi import Request

from app.modules.messaging.service import MessagingService


def get_messaging_service(request: Request) -> MessagingService:
    service = getattr(request.app.state, "messaging_service", None)
    if service is None:
        raise RuntimeError("Messaging service is available only while the application is running.")
    return service
