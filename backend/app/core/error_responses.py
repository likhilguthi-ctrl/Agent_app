from __future__ import annotations

from fastapi.responses import JSONResponse


def error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str | None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "requestId": request_id,
            }
        },
    )


def internal_server_error_response(request_id: str | None) -> JSONResponse:
    return error_response(
        500,
        "INTERNAL_ERROR",
        "An unexpected error occurred.",
        request_id,
    )
