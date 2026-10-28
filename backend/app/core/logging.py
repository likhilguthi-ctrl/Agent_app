from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.request_id import request_id_context


class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per log record without request payloads or secrets."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_context.get()
        if request_id:
            payload["request_id"] = request_id

        for key in (
            "method",
            "status_code",
            "environment",
            "exception_type",
            "provider_error_type",
            "reason",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # The request middleware emits structured, query-free access records.
    # Avoid Uvicorn's default access line, which can include sensitive URLs.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

