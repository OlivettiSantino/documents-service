"""Domain exceptions and their HTTP mapping.

Endpoints raise these; a single handler registered in ``create_app`` turns them
into responses. Keeping the mapping in one place avoids the repeated
``try/except ... raise HTTPException`` blocks that clutter the sibling repo.
"""


class DocumentsServiceError(Exception):
    """Base class for every expected failure of this service."""

    status_code = 500
    message = "Error interno del servicio"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or type(self).message
        super().__init__(self.message)


class InvalidPdfError(DocumentsServiceError):
    """The upload is empty, not a PDF, or not named ``.pdf``."""

    status_code = 400
    message = "El archivo debe ser un PDF válido"


class PayloadTooLargeError(DocumentsServiceError):
    """The upload exceeds ``MAX_UPLOAD_SIZE``."""

    status_code = 413
    message = "El archivo supera el tamaño máximo permitido"


class DocumentNotFoundError(DocumentsServiceError):
    """No document matches the requested id."""

    status_code = 404
    message = "Documento no encontrado"


class DuplicateChecksumError(DocumentsServiceError):
    """A document with the same checksum already exists.

    Raised by the repository when the unique index rejects an insert. The
    service turns it into a ``200`` with ``duplicate: true``, so it never
    reaches the HTTP handler as an error.
    """

    status_code = 409
    message = "El documento ya existe"


class ExtractProtocolError(DocumentsServiceError):
    """extract-service answered, but not with something we can use."""

    status_code = 502
    message = "Respuesta inválida de extract-service"


class RetryableError(DocumentsServiceError):
    """Base for the failures that carry a ``Retry-After`` hint."""

    status_code = 503
    message = "Servicio no disponible, reintentar más tarde"

    def __init__(self, message: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ExtractUnavailableError(RetryableError):
    """extract-service is down, refusing connections, or answering ``503``."""

    message = "extract-service no está disponible"


class CircuitOpenError(RetryableError):
    """The breaker is open: we refuse without calling extract-service.

    Deliberately NOT a subclass of :class:`ExtractUnavailableError`. The breaker
    must not count its own rejections as upstream failures, or it would never
    close again.
    """

    message = "Circuito abierto hacia extract-service"


class BulkheadFullError(RetryableError):
    """No concurrency slot became available in time.

    Also deliberately not an :class:`ExtractUnavailableError`: this is our own
    load shedding, not an upstream fault, and blaming extract-service for it
    would open the circuit for the wrong reason.
    """

    message = "Sin capacidad para procesar la extracción en este momento"


class ExtractTimeoutError(DocumentsServiceError):
    """extract-service accepted the request but did not answer in time."""

    status_code = 504
    message = "extract-service no respondió a tiempo"
