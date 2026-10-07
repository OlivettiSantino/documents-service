"""Wire DTOs.

Separate from ``app/models.py`` so the public contract and the stored shape can
evolve independently. The listing deliberately omits ``content``, which can be
megabytes per document.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DocumentSummary(BaseModel):
    """A document without its extracted text."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Identificador del documento")
    filename: str = Field(..., description="Nombre original del archivo PDF")
    checksum: str = Field(..., description="SHA-256 del contenido del PDF")
    page_count: int = Field(..., description="Cantidad de páginas")
    size_bytes: int = Field(..., description="Tamaño del PDF original en bytes")
    created_at: datetime = Field(..., description="Fecha de creación (UTC)")


class DocumentDetail(DocumentSummary):
    """A document including its extracted text."""

    content: str = Field(..., description="Texto extraído del PDF")
    updated_at: datetime = Field(..., description="Fecha de última modificación (UTC)")


class DocumentCreatedResponse(DocumentDetail):
    """Answer to ``POST /documents``.

    Same body for ``201`` and ``200`` so the client does not have to branch on
    the status code to read the document.
    """

    duplicate: bool = Field(..., description="True si el documento ya existía")


class DocumentListResponse(BaseModel):
    """Paginated listing, without ``content``."""

    items: list[DocumentSummary]
    total: int = Field(..., description="Total de documentos almacenados")
    limit: int
    offset: int


class HealthResponse(BaseModel):
    """Liveness."""

    status: str = "ok"


class ReadinessResponse(BaseModel):
    """Readiness: our own database, plus the state of the extract circuit."""

    status: str = Field(..., description="'ready' o 'not_ready'")
    database: str = Field(..., description="'up' o 'down'")
    extract_circuit: str = Field(..., description="closed | open | half_open")


class ErrorResponse(BaseModel):
    """Error body, matching FastAPI's default shape."""

    detail: str
