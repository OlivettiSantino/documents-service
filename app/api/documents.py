"""``/documents`` endpoints.

Domain errors are raised, not caught: a single handler registered in
``create_app`` maps them to status codes (and to ``Retry-After`` where it
applies).
"""

import time

from fastapi import (
    APIRouter,
    Depends,
    File,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from app.api.deps import get_document_service, get_settings
from app.api.schemas import (
    DocumentCreatedResponse,
    DocumentDetail,
    DocumentListResponse,
    DocumentSummary,
    ErrorResponse,
)
from app.config import Settings
from app.exceptions import InvalidPdfError, PayloadTooLargeError
from app.logging_config import get_logger, request_id_var
from app.services.document_service import DocumentService

logger = get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

_ERRORS = {
    400: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=DocumentCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": DocumentCreatedResponse, "description": "Documento duplicado"}, **_ERRORS},
    summary="Guardar un PDF ya procesado",
)
async def create_document(
    request: Request,
    response: Response,
    file: UploadFile = File(..., description="Archivo PDF"),
    service: DocumentService = Depends(get_document_service),
    settings: Settings = Depends(get_settings),
) -> DocumentCreatedResponse:
    """Store a PDF: checksum, deduplicate, delegate extraction, persist."""
    started = time.monotonic()
    _reject_oversized_upload(request, settings.MAX_UPLOAD_SIZE)

    pdf = await _read_within_limit(file, settings.MAX_UPLOAD_SIZE)

    document, duplicate = await service.store(
        filename=file.filename or "",
        pdf=pdf,
        request_id=request_id_var.get(),
    )

    if duplicate:
        response.status_code = status.HTTP_200_OK

    logger.info(
        "POST /documents",
        extra={
            "duplicate": duplicate,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        },
    )
    return DocumentCreatedResponse(**document.model_dump(), duplicate=duplicate)


@router.get("/{document_id}", response_model=DocumentDetail, summary="Documento completo")
async def get_document(
    document_id: str,
    service: DocumentService = Depends(get_document_service),
) -> DocumentDetail:
    document = await service.get(document_id)
    return DocumentDetail.model_validate(document)


@router.get("", response_model=DocumentListResponse, summary="Listado sin contenido")
async def list_documents(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: DocumentService = Depends(get_document_service),
) -> DocumentListResponse:
    documents, total = await service.list(limit=limit, offset=offset)
    return DocumentListResponse(
        items=[DocumentSummary.model_validate(d) for d in documents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Borrar")
async def delete_document(
    document_id: str,
    service: DocumentService = Depends(get_document_service),
) -> None:
    await service.delete(document_id)


def _reject_oversized_upload(request: Request, max_size: int) -> None:
    """Refuse an oversized upload before reading a single byte of the body."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_size:
        raise PayloadTooLargeError(f"El archivo supera el tamaño máximo de {max_size} bytes")


async def _read_within_limit(file: UploadFile, max_size: int) -> bytes:
    """Read the upload, aborting as soon as it exceeds the limit.

    A client can lie about (or omit) ``Content-Length``, so the running total is
    what actually bounds memory here.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > max_size:
            raise PayloadTooLargeError(f"El archivo supera el tamaño máximo de {max_size} bytes")
        chunks.append(chunk)

    if total == 0:
        raise InvalidPdfError("No se recibió ningún PDF")
    return b"".join(chunks)
