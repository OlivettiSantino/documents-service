"""``/documents`` endpoints.

Domain errors are raised, not caught: a single handler registered in
``create_app`` maps them to status codes (and to ``Retry-After`` where it
applies).

``POST /documents`` reads the body itself instead of declaring an
``UploadFile``. With ``File(...)`` FastAPI parses the whole multipart, and
spools anything above 1 MB to disk, *before* the endpoint runs, so no size
check written in the endpoint could stop an oversized upload in time.
"""

import asyncio
import time

from fastapi import APIRouter, Depends, Query, Request, Response, status
from starlette.requests import ClientDisconnect

from app.api.deps import get_document_service, get_settings, get_upload_slots
from app.api.multipart import (
    MULTIPART_FILE_FIELD,
    MalformedMultipartError,
    UploadedFile,
    boundary_from,
    file_from_multipart,
)
from app.api.schemas import (
    DocumentCreatedResponse,
    DocumentDetail,
    DocumentListResponse,
    DocumentSummary,
    ErrorResponse,
)
from app.config import Settings
from app.exceptions import InvalidPdfError, PayloadTooLargeError, UploadSlotsFullError
from app.logging_config import get_logger, request_id_var
from app.services.document_service import DocumentService

logger = get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

#: Room for the multipart framing (boundaries and part headers) on top of
#: ``MAX_UPLOAD_SIZE``. The body is cut at the limit plus this margin; the file
#: itself is then held to ``MAX_UPLOAD_SIZE`` exactly.
MULTIPART_OVERHEAD = 64 * 1024

_ERRORS = {
    400: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
}

#: The body is read by hand, so FastAPI cannot infer it: document it here so
#: ``/docs`` keeps offering the upload.
_UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        MULTIPART_FILE_FIELD: {
                            "type": "string",
                            "format": "binary",
                            "description": "Archivo PDF",
                        }
                    },
                    "required": [MULTIPART_FILE_FIELD],
                }
            }
        },
    }
}


@router.post(
    "",
    response_model=DocumentCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": DocumentCreatedResponse, "description": "Documento duplicado"}, **_ERRORS},
    summary="Guardar un PDF ya procesado",
    openapi_extra=_UPLOAD_BODY,
)
async def create_document(
    request: Request,
    response: Response,
    service: DocumentService = Depends(get_document_service),
    settings: Settings = Depends(get_settings),
    upload_slots: asyncio.Semaphore = Depends(get_upload_slots),
) -> DocumentCreatedResponse:
    """Store a PDF: checksum, deduplicate, delegate extraction, persist."""
    started = time.monotonic()
    max_size = settings.MAX_UPLOAD_SIZE
    body_limit = max_size + MULTIPART_OVERHEAD

    # Cheap rejections first: they need neither a slot nor the body.
    _reject_declared_oversize(request, body_limit, max_size)
    boundary = boundary_from(request.headers.get("content-type", ""))
    if boundary is None:
        raise InvalidPdfError(
            f"El archivo debe enviarse como multipart/form-data, campo '{MULTIPART_FILE_FIELD}'"
        )

    await _acquire_upload_slot(upload_slots, settings.UPLOAD_SLOT_TIMEOUT)
    try:
        # The slot covers reading, extraction and persistence: the PDF stays
        # in memory until the very end.
        upload = await _receive_upload(request, boundary, body_limit, max_size)
        document, duplicate = await service.store(
            filename=upload.filename,
            pdf=upload.content,
            request_id=request_id_var.get(),
        )
    finally:
        upload_slots.release()

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


def _too_large(max_size: int) -> PayloadTooLargeError:
    return PayloadTooLargeError(f"El archivo supera el tamaño máximo de {max_size} bytes")


def _reject_declared_oversize(request: Request, body_limit: int, max_size: int) -> None:
    """Refuse an upload whose declared ``Content-Length`` is already too big.

    Runs before taking a slot and before reading a single byte of the body.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > body_limit:
        raise _too_large(max_size)


async def _acquire_upload_slot(upload_slots: asyncio.Semaphore, timeout: float) -> None:
    """Wait a bounded time for an admission slot, without touching the body."""
    try:
        async with asyncio.timeout(timeout):
            await upload_slots.acquire()
    except TimeoutError:
        logger.warning("Sin cupo para recibir uploads")
        raise UploadSlotsFullError(retry_after=timeout) from None


async def _receive_upload(
    request: Request, boundary: bytes, body_limit: int, max_size: int
) -> UploadedFile:
    """Read the body within ``body_limit`` and pull the PDF out of it."""
    body = await _read_body_within_limit(request, body_limit, max_size)
    try:
        upload = file_from_multipart(body, boundary)
    except MalformedMultipartError:
        raise InvalidPdfError("El cuerpo multipart está mal formado") from None

    if upload is None or not upload.content:
        raise InvalidPdfError("No se recibió ningún PDF")
    if len(upload.content) > max_size:
        raise _too_large(max_size)
    return upload


async def _read_body_within_limit(
    request: Request, body_limit: int, max_size: int
) -> bytearray:
    """Read the body, aborting as soon as it exceeds ``body_limit``.

    A client can lie about (or omit) ``Content-Length``, so the running total is
    what actually bounds memory here.
    """
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > body_limit:
                raise _too_large(max_size)
    except ClientDisconnect:
        # Nobody is left to read the answer; this only keeps the log clean
        # (an unhandled exception would be logged as a 500).
        logger.info("El cliente cortó la conexión durante el upload", extra={"read_bytes": len(body)})
        raise InvalidPdfError("El upload se interrumpió antes de terminar") from None
    # Returned as is: copying it to ``bytes`` would hold the upload twice.
    return body
