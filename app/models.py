"""Internal domain models.

These are what the repository stores and returns. The API talks in terms of
``app/api/schemas.py`` instead, so changing the wire format never forces a
change to persistence (and the other way round).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DocumentCreate(BaseModel):
    """A document about to be persisted."""

    filename: str
    checksum: str = Field(..., description="SHA-256 del PDF original")
    content: str = Field(..., description="Texto extraído por extract-service")
    page_count: int = Field(..., ge=0)
    size_bytes: int = Field(..., ge=0)


class Document(DocumentCreate):
    """A persisted document."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime


class ExtractionResult(BaseModel):
    """What extract-service gives back for a PDF."""

    content: str
    page_count: int = Field(..., ge=0)
