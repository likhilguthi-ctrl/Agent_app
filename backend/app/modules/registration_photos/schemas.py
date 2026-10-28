from __future__ import annotations

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field


class StorageUploadResponse(BaseModel):
    object_key: str
    category: str
    content_type: str
    file_size: int
    width: int | None = None
    height: int | None = None
    created_at: datetime


class StoragePresignedUrlResponse(BaseModel):
    object_key: str
    presigned_url: str
    expires_in_seconds: int

