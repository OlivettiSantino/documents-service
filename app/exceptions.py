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
    """A malformed request, caught by documents itself before calling extract.

    Empty upload, no ``%PDF`` magic, no ``.pdf`` extension, or no
    ``multipart/form-data``. Compare :class:`UnprocessablePdfError`: that one
    looks like a PDF but extract-service could not process it.
    """

    status_code = 400
    message = "El archivo debe ser un PDF válido"


class UnprocessablePdfError(DocumentsServiceError):
    """The upload looks like a PDF, but extract-service could not process it.

    extract answered ``400``/``422``: the file is damaged, encrypted or not a
    real PDF. It is the client's file, not a fault of extract, so it is not a
    ``502``. Compare :class:`InvalidPdfError` (``400``): there documents
    rejected the request itself, before calling extract.

    The message is fixed; extract's ``detail`` only goes to the log.
    """

    status_code = 422
    message = "El PDF no se pudo procesar: está dañado, cifrado o no es válido"


class PayloadTooLargeError(DocumentsServiceError):
    """The upload exceeds ``MAX_UPLOAD_SIZE``."""

    status_code = 413
    message = "El archivo supera el tamaño máximo permitido"


class ContentTooLargeError(DocumentsServiceError):
    """The extracted text does not fit in a MongoDB document.

    ``422`` and not ``413``: the upload was within ``MAX_UPLOAD_SIZE``; what
    cannot be processed is the result of the extraction.
    """

    status_code = 422
    message = "El texto extraído supera el máximo que se puede almacenar"


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
    """extract-service is unreachable or saturated: connection refused, or the
    gateway answered ``502``/``503``/``404``/``429``. A ``503`` (extract's
    backpressure or Traefik's circuit breaker) and a ``429`` (Traefik's
    ``inFlightReq``) land here with the ``Retry-After`` they carried, if any."""

    message = "extract-service no está disponible"


class UploadSlotsFullError(RetryableError):
    """Too many uploads are being held in memory right now.

    Admission control at the HTTP edge, before a single byte of the body is
    read. It bounds memory and, since every admitted upload makes at most one
    call to extract-service, also how many extractions run at once. It is our
    own load shedding, not an upstream fault, so it is not an
    :class:`ExtractUnavailableError`.
    """

    message = "Servicio saturado: no hay cupo para recibir el archivo"


class ExtractTimeoutError(DocumentsServiceError):
    """extract-service accepted the request but did not answer in time."""

    status_code = 504
    message = "extract-service no respondió a tiempo"
