from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import warnings
from typing import Protocol
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = MAX_IMAGE_DIMENSION * MAX_IMAGE_DIMENSION
_ALLOWED_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class InvalidImage(ValueError):
    """An uploaded payload is not an allowed, bounded raster image."""


class ObjectStorageUnavailable(RuntimeError):
    """No approved private object-storage implementation is configured."""


def check_magic_signature(content: bytes) -> str:
    """Validate file magic bytes to verify genuine raster image payloads.
    
    Prevents disguised executables, HTML polyglots, and corrupted binary streams.
    """
    if len(content) < 12:
        raise InvalidImage("The image payload is too short.")
    if content.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "WEBP"
    raise InvalidImage("The file signature does not match any supported image format (JPEG, PNG, WebP).")


@dataclass(frozen=True)
class ValidatedImage:
    content: bytes
    content_type: str
    image_format: str
    width: int
    height: int


class PrivateObjectStorage(Protocol):
    async def put_private(
        self, *, object_key: str, content: bytes, content_type: str
    ) -> None: ...

    async def get_private(self, *, object_key: str) -> bytes: ...

    async def delete_private(self, *, object_key: str) -> None: ...

    async def generate_presigned_url(
        self, *, object_key: str, expires_in_seconds: int = 300
    ) -> str: ...


class UnconfiguredPrivateObjectStorage:
    async def put_private(
        self, *, object_key: str, content: bytes, content_type: str
    ) -> None:
        raise ObjectStorageUnavailable("Cloudflare R2 private object storage is not configured.")

    async def get_private(self, *, object_key: str) -> bytes:
        raise ObjectStorageUnavailable("Cloudflare R2 private object storage is not configured.")

    async def delete_private(self, *, object_key: str) -> None:
        raise ObjectStorageUnavailable("Cloudflare R2 private object storage is not configured.")

    async def generate_presigned_url(
        self, *, object_key: str, expires_in_seconds: int = 300
    ) -> str:
        raise ObjectStorageUnavailable("Cloudflare R2 private object storage is not configured.")


class MockedR2Storage:
    """In-memory S3/R2 mock storage for tests and local development."""

    def __init__(self, bucket_name: str = "community-ott-private") -> None:
        self.bucket_name = bucket_name
        self._store: dict[str, tuple[bytes, str]] = {}

    async def put_private(
        self, *, object_key: str, content: bytes, content_type: str
    ) -> None:
        self._store[object_key] = (content, content_type)

    async def get_private(self, *, object_key: str) -> bytes:
        if object_key not in self._store:
            raise ValueError(f"Object {object_key} not found in mock storage.")
        return self._store[object_key][0]

    async def delete_private(self, *, object_key: str) -> None:
        self._store.pop(object_key, None)

    async def generate_presigned_url(
        self, *, object_key: str, expires_in_seconds: int = 300
    ) -> str:
        if object_key not in self._store:
            raise ValueError(f"Object {object_key} not found in mock storage.")
        return f"https://mock-r2.cloudflarestorage.com/{self.bucket_name}/{object_key}?expires={expires_in_seconds}"


class CloudflareR2Storage:
    """Production S3-compatible Cloudflare R2 storage adapter using boto3.
    
    All boto3 blocking I/O calls run via Starlette run_in_threadpool to keep
    the ASGI event loop completely non-blocking.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
        region_name: str = "auto",
    ) -> None:
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket_name = bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region_name,
            config=BotoConfig(signature_version="s3v4"),
        )

    async def put_private(
        self, *, object_key: str, content: bytes, content_type: str
    ) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        def _sync_put() -> None:
            self._client.put_object(
                Bucket=self.bucket_name,
                Key=object_key,
                Body=content,
                ContentType=content_type,
            )

        try:
            await run_in_threadpool(_sync_put)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageUnavailable(f"Cloudflare R2 put error: {exc}") from exc

    async def get_private(self, *, object_key: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError

        def _sync_get() -> bytes:
            response = self._client.get_object(
                Bucket=self.bucket_name,
                Key=object_key,
            )
            return response["Body"].read()

        try:
            return await run_in_threadpool(_sync_get)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageUnavailable(f"Cloudflare R2 get error: {exc}") from exc

    async def delete_private(self, *, object_key: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        def _sync_delete() -> None:
            self._client.delete_object(
                Bucket=self.bucket_name,
                Key=object_key,
            )

        try:
            await run_in_threadpool(_sync_delete)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageUnavailable(f"Cloudflare R2 delete error: {exc}") from exc

    async def generate_presigned_url(
        self, *, object_key: str, expires_in_seconds: int = 300
    ) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        def _sync_presign() -> str:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": object_key},
                ExpiresIn=expires_in_seconds,
            )

        try:
            return await run_in_threadpool(_sync_presign)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageUnavailable(f"Cloudflare R2 presign error: {exc}") from exc


_global_mock_storage = MockedR2Storage()


def get_storage_provider(settings: object | None = None) -> PrivateObjectStorage:
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()

    endpoint = getattr(settings, "r2_endpoint_url", None)
    bucket = getattr(settings, "r2_bucket_name", None)
    key_id = getattr(settings, "r2_access_key_id", None)
    secret_key = getattr(settings, "r2_secret_access_key", None)
    account_id = getattr(settings, "r2_account_id", None)
    region = getattr(settings, "r2_region", "auto")

    # If endpoint_url is not set but account_id is provided, derive Cloudflare R2 standard endpoint
    if not endpoint and account_id:
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"

    if endpoint and bucket and key_id and secret_key:
        access_id_val = key_id.get_secret_value() if hasattr(key_id, "get_secret_value") else str(key_id)
        secret_key_val = secret_key.get_secret_value() if hasattr(secret_key, "get_secret_value") else str(secret_key)
        return CloudflareR2Storage(
            endpoint_url=endpoint,
            access_key_id=access_id_val,
            secret_access_key=secret_key_val,
            bucket_name=bucket,
            region_name=region,
        )

    environment = getattr(settings, "environment", "development")
    if environment in ("test", "testing", "development"):
        return _global_mock_storage

    return UnconfiguredPrivateObjectStorage()


_CATEGORY_PREFIXES = {
    "agent-photo": "agent-photos",
    "aadhaar-document": "aadhaar-documents",
    "pan-document": "pan-documents",
    "user-photo": "user-photos",
}


def build_object_key(category: str, extension: str = "jpg") -> str:
    prefix = _CATEGORY_PREFIXES.get(category, "documents")
    return f"{prefix}/{uuid4().hex}.{extension}"


def new_private_object_key() -> str:
    return uuid4().hex


def validate_image(content: bytes) -> ValidatedImage:
    if not content:
        raise InvalidImage("The image is empty.")
    if len(content) > MAX_IMAGE_BYTES:
        raise InvalidImage("The image exceeds the maximum permitted size.")

    # Defense in depth: Verify magic bytes before any parser reads the payload
    detected_signature_format = check_magic_signature(content)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                image_format = image.format
                if image_format not in _ALLOWED_FORMATS:
                    raise InvalidImage("Only JPEG, PNG, and WebP images are allowed.")
                if image_format != detected_signature_format:
                    raise InvalidImage("The image content format does not match the file signature.")
                if getattr(image, "n_frames", 1) != 1:
                    raise InvalidImage("Animated images are not allowed.")
                width, height = image.size
                if (
                    width < 1
                    or height < 1
                    or width > MAX_IMAGE_DIMENSION
                    or height > MAX_IMAGE_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    raise InvalidImage("The image dimensions exceed the permitted limit.")
                image.verify()

            with Image.open(BytesIO(content)) as decoded:
                if decoded.format != image_format or decoded.size != (width, height):
                    raise InvalidImage("The image content is inconsistent.")
                decoded.load()
                normalized = decoded.convert(
                    "RGBA" if "A" in decoded.getbands() else "RGB"
                )
                output = BytesIO()
                if image_format == "JPEG":
                    normalized.convert("RGB").save(
                        output, format="JPEG", quality=90, optimize=True
                    )
                elif image_format == "PNG":
                    normalized.save(output, format="PNG", optimize=True)
                else:
                    normalized.save(output, format="WEBP", quality=90, method=4)
    except InvalidImage:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise InvalidImage("The image content is invalid or unsafe.") from exc

    sanitized_content = output.getvalue()
    if not sanitized_content or len(sanitized_content) > MAX_IMAGE_BYTES:
        raise InvalidImage("The sanitized image exceeds the maximum permitted size.")
    return ValidatedImage(
        content=sanitized_content,
        content_type=_ALLOWED_FORMATS[image_format],
        image_format=image_format,
        width=width,
        height=height,
    )
