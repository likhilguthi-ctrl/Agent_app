from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[dict[str, Any]]], Callable[..., Awaitable[None]]], Awaitable[None]]


class OIDCCallbackQueryScrubbingMiddleware:
    """Remove one-time OIDC callback values from ASGI access-log scope after handling."""

    _CALLBACK_SUFFIX = "/platform/auth/oidc/callback"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope.get("type") != "http"
            or not isinstance(scope.get("path"), str)
            or not scope["path"].endswith(self._CALLBACK_SUFFIX)
        ):
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            # ASGI servers commonly format the access line after the app returns.
            # Keep the query available to FastAPI during dispatch, then erase it.
            scope["query_string"] = b""
