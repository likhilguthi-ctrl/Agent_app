from __future__ import annotations

from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.core.config import Settings
from app.main import create_app
from app.modules.registration_photos.storage import (
    MAX_IMAGE_BYTES,
    InvalidImage,
    ObjectStorageUnavailable,
    UnconfiguredPrivateObjectStorage,
    new_private_object_key,
    validate_image,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_registration_photo_upload_fails_closed_without_trusted_registration() -> None:
    app = create_app(Settings(frontend_origins=""))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/registration-photos")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "REGISTRATION_PHOTO_ASSOCIATION_UNAVAILABLE"
    assert response.json()["error"]["requestId"]


@pytest.mark.anyio
async def test_registration_photo_route_does_not_echo_or_accept_photo_payload() -> None:
    app = create_app(Settings(frontend_origins=""))
    transport = httpx.ASGITransport(app=app)
    boundary = "unit-test-boundary"
    payload = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="photo"; filename="private.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
        "SENSITIVE_PHOTO_BYTES\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/registration-photos",
            content=payload,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "REGISTRATION_PHOTO_ASSOCIATION_UNAVAILABLE"
    assert "SENSITIVE_PHOTO_BYTES" not in response.text
    assert "private.jpg" not in response.text


@pytest.mark.anyio
async def test_subscriber_registration_api_is_not_exposed_without_trusted_employee_contract() -> None:
    app = create_app(Settings(frontend_origins=""))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/subscribers",
            json={"tenant_id": "client-selected-tenant", "name": "Example"},
        )

    assert response.status_code == 404


def create_image(image_format: str, size: tuple[int, int] = (32, 24)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(12, 34, 56)).save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("image_format", "content_type"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")],
)
def test_image_validation_decodes_supported_formats_without_trusting_metadata(
    image_format: str, content_type: str
) -> None:
    original = create_image(image_format)
    validated = validate_image(original)

    assert validated.content != original
    assert validated.image_format == image_format
    assert validated.content_type == content_type
    assert (validated.width, validated.height) == (32, 24)
    with Image.open(BytesIO(validated.content)) as sanitized:
        assert sanitized.format == image_format
        assert sanitized.size == (32, 24)
        assert not sanitized.getexif()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not an image",
        b"\x89PNG\r\n\x1a\ntruncated",
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
    ],
)
def test_image_validation_rejects_empty_corrupt_and_non_raster_content(
    content: bytes,
) -> None:
    with pytest.raises(InvalidImage):
        validate_image(content)


def test_image_validation_rejects_oversized_payload_and_dimensions() -> None:
    with pytest.raises(InvalidImage, match="maximum permitted size"):
        validate_image(b"x" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(InvalidImage, match="dimensions"):
        validate_image(create_image("PNG", (4097, 1)))


def test_private_object_keys_are_random_and_do_not_include_client_paths() -> None:
    first = new_private_object_key()
    second = new_private_object_key()

    assert first != second
    assert "/" not in first and "\\" not in first
    assert len(first) == 32


@pytest.mark.anyio
async def test_unconfigured_private_storage_fails_explicitly() -> None:
    storage = UnconfiguredPrivateObjectStorage()

    with pytest.raises(ObjectStorageUnavailable):
        await storage.put_private(
            object_key=new_private_object_key(),
            content=create_image("PNG"),
            content_type="image/png",
        )
    with pytest.raises(ObjectStorageUnavailable):
        await storage.get_private(object_key=new_private_object_key())
    with pytest.raises(ObjectStorageUnavailable):
        await storage.delete_private(object_key=new_private_object_key())
