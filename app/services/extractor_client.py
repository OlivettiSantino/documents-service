"""HTTP client for extract-service.

One ``POST`` per extraction, bounded by the httpx client's timeout
(``EXTRACT_TIMEOUT``), and nothing else: no retry, no circuit breaker and no
concurrency limit of its own. Traefik applies those to the traffic towards
extract-service, through the middlewares declared in extract's Consul tags
(issue #28). How many extractions run at once is bounded upstream, by the
upload admission slots (``MAX_CONCURRENT_UPLOADS``).

The destination is fixed, ``EXTRACT_URL``: in the compose, the gateway
(``http://traefik``), which finds extract's replicas in Consul and balances.

What stays here is turning every answer into a domain error, including the
gateway's own ``502``/``504``/``404``/``429`` and the ``Retry-After`` of a
``503`` or ``429``, which travels with the error up to the client. A ``400`` or
``422`` from extract is the client's PDF, not extract's fault: it becomes a
``422``, never a ``502``.

Every error carries the fixed ``message`` of its class: nothing that reaches
the client includes text from httpx, hosts, IPs, internal URLs or extract's
``detail``. That detail goes to a ``warning`` log line instead, which also
carries the request id.
"""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from app.exceptions import (
    ExtractProtocolError,
    ExtractTimeoutError,
    ExtractUnavailableError,
    PayloadTooLargeError,
    UnprocessablePdfError,
)
from app.logging_config import get_logger
from app.models import ExtractionResult

logger = get_logger(__name__)

PDF_CONTENT_TYPE = "application/pdf"

#: Cuanto del ``detail`` de extract se copia al log, como mucho.
MAX_LOGGED_DETAIL = 200


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header in either of its two legal forms.

    Returns the delay in seconds, or ``None`` if the header is absent or
    unparseable (RFC 9110 allows both delta-seconds and an HTTP-date).
    """
    if not value:
        return None

    value = value.strip()
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0.0, (when - reference).total_seconds())


class ExtractorClient:
    """Adapter over extract-service implementing ``ExtractorClientPort``."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        base_url: str,
    ) -> None:
        self._http = http_client
        # Base sin "/extract", p. ej. http://traefik.
        self._url = f"{base_url.rstrip('/')}/extract"

    async def extract(self, pdf: bytes, *, request_id: str | None = None) -> ExtractionResult:
        """Extract text from ``pdf`` via extract-service, in a single attempt.

        Nothing is retried here, not even a ``503``: retrying towards another
        replica is Traefik's job, and the client gets our ``503`` together
        with the gateway's ``Retry-After``.
        """
        headers = {"Content-Type": PDF_CONTENT_TYPE}
        if request_id:
            headers["X-Request-ID"] = request_id

        try:
            response = await self._http.post(self._url, content=pdf, headers=headers)
        except httpx.TimeoutException as exc:
            _log_transport_error("extract-service no respondió a tiempo", exc)
            raise ExtractTimeoutError() from exc
        except httpx.HTTPError as exc:
            # El texto de httpx trae host, puerto o IP del destino: solo al log.
            _log_transport_error("No se pudo contactar a extract-service", exc)
            raise ExtractUnavailableError() from exc

        return self._parse(response)

    def _parse(self, response: httpx.Response) -> ExtractionResult:
        status = response.status_code
        if status == 200:
            return self._parse_result(response)

        if status in (400, 422):
            # extract no pudo procesar el archivo: dañado, cifrado o no es un
            # PDF de verdad. Es culpa del archivo del cliente, no de extract.
            _log_error_response("extract-service rechazó el PDF", response)
            raise UnprocessablePdfError()
        if status == 413:
            # El MAX_UPLOAD_SIZE de extract es menor que el nuestro.
            _log_error_response("extract-service rechazó el PDF por tamaño", response)
            raise PayloadTooLargeError()
        if status in (429, 503):
            # 503: el backpressure de extract, o Traefik con el circuit breaker
            # abierto o sin servidores. 429: el inFlightReq de Traefik. Las dos
            # son saturación: reintentar más tarde.
            _log_error_response("extract-service no disponible o saturado", response)
            raise ExtractUnavailableError(
                retry_after=parse_retry_after(response.headers.get("Retry-After"))
            )
        # Respuestas propias del gateway (issue #27). Llamando a extract
        # directo no ocurrian; a traves de Traefik son la forma en que avisa
        # que no llego a una replica.
        if status == 504:
            # Traefik corto por responseHeaderTimeout.
            _log_error_response("El gateway cortó la espera a extract-service (504)", response)
            raise ExtractTimeoutError()
        if status == 502:
            # Traefik no pudo hablar con la replica (conexion rechazada o
            # cortada).
            _log_error_response("El gateway no pudo contactar a extract-service (502)", response)
            raise ExtractUnavailableError()
        if status == 404:
            # Traefik no tiene ruta para /extract: extract no esta registrado
            # en Consul o no tiene ninguna instancia passing.
            _log_error_response(
                "El gateway no tiene ruta hacia extract-service (404): "
                "extract no esta registrado en Consul",
                response,
            )
            raise ExtractUnavailableError()
        _log_error_response("extract-service respondió un código inesperado", response)
        raise ExtractProtocolError()

    @staticmethod
    def _parse_result(response: httpx.Response) -> ExtractionResult:
        try:
            payload = response.json()
        except ValueError as exc:
            raise _protocol_error("extract-service no devolvió JSON") from exc

        if not isinstance(payload, dict):
            raise _protocol_error("extract-service devolvió un JSON inesperado")

        content = payload.get("content")
        page_count = payload.get("page_count")
        if not isinstance(content, str) or not isinstance(page_count, int) or isinstance(page_count, bool):
            raise _protocol_error("Faltan 'content' o 'page_count' en la respuesta de extract-service")
        if page_count < 0:
            raise _protocol_error("'page_count' negativo en la respuesta de extract-service")

        return ExtractionResult(content=content, page_count=page_count)


def _log_transport_error(event: str, exc: httpx.HTTPError) -> None:
    logger.warning(event, extra={"error_type": type(exc).__name__, "error": str(exc)})


def _log_error_response(event: str, response: httpx.Response) -> None:
    logger.warning(
        event,
        extra={
            "extract_status": response.status_code,
            "extract_detail": _upstream_detail(response),
        },
    )


def _protocol_error(reason: str) -> ExtractProtocolError:
    """A ``200`` we cannot use: the reason goes to the log, not to the client."""
    logger.warning("Respuesta inválida de extract-service", extra={"reason": reason})
    return ExtractProtocolError()


def _upstream_detail(response: httpx.Response) -> str:
    """The ``detail`` of an error answer, for the log only, never for the client."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:MAX_LOGGED_DETAIL]
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])[:MAX_LOGGED_DETAIL]
    return str(payload)[:MAX_LOGGED_DETAIL]
