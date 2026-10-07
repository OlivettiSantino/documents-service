"""Document orchestration: validate, deduplicate, extract, persist."""

import hashlib

from app.exceptions import (
    DuplicateChecksumError,
    InvalidPdfError,
    PayloadTooLargeError,
)
from app.logging_config import get_logger
from app.models import Document, DocumentCreate
from app.ports import DocumentRepositoryPort, ExtractorClientPort

logger = get_logger(__name__)

PDF_MAGIC = b"%PDF"


class DocumentService:
    """Use cases for stored documents.

    Does not extract text — that is delegated to extract-service through
    :class:`~app.ports.ExtractorClientPort`.
    """

    def __init__(
        self,
        *,
        repository: DocumentRepositoryPort,
        extractor: ExtractorClientPort,
        max_upload_size: int,
    ) -> None:
        self._repository = repository
        self._extractor = extractor
        self._max_upload_size = max_upload_size

    async def store(
        self, *, filename: str, pdf: bytes, request_id: str | None = None
    ) -> tuple[Document, bool]:
        """Store a PDF, extracting its text first unless we already have it.

        Returns:
            The document and whether it was already stored (a duplicate).
        """
        self._validate(filename, pdf)

        checksum = hashlib.sha256(pdf).hexdigest()
        short = checksum[:12]

        # Dedupe first, deliberately: a repeated PDF must not call
        # extract-service, must not consume a bulkhead slot and must not be
        # able to trip the circuit breaker.
        existing = await self._repository.find_by_checksum(checksum)
        if existing is not None:
            logger.info("Documento duplicado", extra={"checksum": short, "id": existing.id})
            return existing, True

        result = await self._extractor.extract(pdf, request_id=request_id)

        try:
            document = await self._repository.create(
                DocumentCreate(
                    filename=filename,
                    checksum=checksum,
                    content=result.content,
                    page_count=result.page_count,
                    size_bytes=len(pdf),
                )
            )
        except DuplicateChecksumError:
            # Two identical uploads raced past the check above. The unique
            # index is the real guarantee; re-read and report a duplicate.
            existing = await self._repository.find_by_checksum(checksum)
            if existing is None:  # pragma: no cover - index says it exists
                raise
            logger.info("Duplicado detectado por índice único", extra={"checksum": short})
            return existing, True

        logger.info(
            "Documento almacenado",
            extra={
                "checksum": short,
                "id": document.id,
                "page_count": document.page_count,
                "size_bytes": document.size_bytes,
            },
        )
        return document, False

    def _validate(self, filename: str, pdf: bytes) -> None:
        if len(pdf) > self._max_upload_size:
            raise PayloadTooLargeError(
                f"El archivo supera el tamaño máximo de {self._max_upload_size} bytes"
            )
        if not pdf:
            raise InvalidPdfError("No se recibió ningún PDF")
        if not filename or not filename.lower().endswith(".pdf"):
            raise InvalidPdfError("El archivo debe tener extensión .pdf")
        if not pdf.startswith(PDF_MAGIC):
            raise InvalidPdfError("El contenido del archivo no es un PDF válido")
