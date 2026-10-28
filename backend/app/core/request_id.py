from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from uuid import uuid4

from app.core.error_responses import internal_server_error_response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_logger = logging.getLogger("app.http")


class RequestIdMiddleware:
    """Attach a safe request ID to the context and every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        supplied_id = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = supplied_id if _VALID_REQUEST_ID.fullmatch(supplied_id) else str(uuid4())
        token = request_id_context.set(request_id)
        status_code = 500
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                response_headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": response_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            if response_started:
                raise
            _logger.error(
                "unhandled_request_error",
                exc_info=True,
                extra={"exception_type": type(exc).__name__},
            )
            response = internal_server_error_response(request_id)
            await response(scope, receive, send_with_request_id)
        finally:
            _logger.info(
                "http_request_complete",
                extra={"method": scope.get("method", ""), "status_code": status_code},
            )
            request_id_context.reset(token)

