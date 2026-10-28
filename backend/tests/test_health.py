from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_health_endpoints_return_ok_and_request_id() -> None:
    app = create_app(Settings(frontend_origins=""))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/health", "/api/v1/health"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert response.headers["x-request-id"]


@pytest.mark.anyio
async def test_request_id_is_preserved_when_safe_and_replaced_when_invalid() -> None:
    app = create_app(Settings(frontend_origins=""))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        safe = await client.get("/health", headers={"X-Request-ID": "review-123"})
        assert safe.headers["x-request-id"] == "review-123"

        invalid = await client.get("/health", headers={"X-Request-ID": "!" * 70})
        assert invalid.status_code == 200
        assert invalid.headers["x-request-id"] != "!" * 70
        assert len(invalid.headers["x-request-id"]) == 36


@pytest.mark.anyio
async def test_cors_uses_only_configured_frontend_origins() -> None:
    app = create_app(Settings(frontend_origins="https://frontend.example"))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.options(
            "/api/v1/health",
            headers={
                "Origin": "https://frontend.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type,x-csrf-token",
            },
        )
        denied = await client.options(
            "/api/v1/health",
            headers={
                "Origin": "https://unlisted.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.headers["access-control-allow-origin"] == "https://frontend.example"
    assert "x-csrf-token" in allowed.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.anyio
async def test_errors_use_the_central_response_shape_without_echoing_input() -> None:
    app = create_app(Settings(frontend_origins=""))

    @app.get("/validation-check")
    async def validation_check(value: int) -> dict[str, int]:
        return {"value": value}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.get("/validation-check", params={"value": "not-an-int"})
        missing = await client.get("/route-that-does-not-exist")

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert invalid.json()["error"]["message"] == "Request validation failed."
    assert "not-an-int" not in invalid.text
    assert invalid.json()["error"]["requestId"] == invalid.headers["x-request-id"]
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "HTTP_ERROR"

