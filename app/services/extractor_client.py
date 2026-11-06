"""HTTP client for extract-service.

The destination comes from an ``ExtractEndpointPort`` and is resolved on
every attempt.

Layer order, outermost first::

    retry(1, only on 503) -> bulkhead.slot() -> timeout -> httpx

The circuit breaker is not here: Traefik applies it to the traffic towards
extract-service (issue #28).

* The **bulkhead sits inside the retry** so a retry asks for a slot again and a
  burst of retries cannot bypass the concurrency limit.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.clock import Clock
from app.exceptions import (
    ExtractProtocolError,
    ExtractTimeoutError,
    ExtractUnavailableError,
)
from app.logging_config import get_logger
from app.models import ExtractionResult
from app.ports import ExtractEndpointPort
from app.services.bulkhead import Bulkhead

logger = get_logger(__name__)

PDF_CONTENT_TYPE = "application/pdf"


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
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (when - reference).total_seconds())


class ExtractorClient:
    """Adapter over extract-service implementing ``ExtractorClientPort``."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        endpoints: ExtractEndpointPort,
        bulkhead: Bulkhead,
        clock: Clock,
        max_retry_wait: float = 1.0,
    ) -> None:
        self._http = http_client
        # La URL se resuelve por pedido: con Consul puede cambiar de instancia.
        self._endpoints = endpoints
        self._bulkhead = bulkhead
        self._clock = clock
        self._max_retry_wait = max_retry_wait

    async def extract(self, pdf: bytes, *, request_id: str | None = None) -> ExtractionResult:
        """Extract text from ``pdf`` via extract-service."""
        return await self._with_retry(pdf, request_id)

    async def _with_retry(self, pdf: bytes, request_id: str | None) -> ExtractionResult:
        """At most one retry, and only when extract-service answered ``503``.

        A timeout is never retried: extract-service is already saturated, and
        resending the same multi-megabyte PDF would double the load on the very
        bottleneck we are trying to protect.
        """
        try:
            return await self._attempt(pdf, request_id)
        except ExtractUnavailableError as first:
            wait = first.retry_after
            if wait is None or wait > self._max_retry_wait:
                # Honour the server's own pacing instead of hammering it: hand
                # the hint to our caller and let them decide.
                raise
            logger.info("Reintentando extracción", extra={"retry_after": wait})
            await self._clock.sleep(wait)
            return await self._attempt(pdf, request_id)

    async def _attempt(self, pdf: bytes, request_id: str | None) -> ExtractionResult:
        headers = {"Content-Type": PDF_CONTENT_TYPE}
        if request_id:
            headers["X-Request-ID"] = request_id

        # Fuera del cupo: resolver el destino es barato (va cacheado) y no
        # tiene por que ocupar un lugar del bulkhead.
        url = f"{await self._endpoints.base_url()}/extract"

        async with self._bulkhead.slot():
            try:
                response = await self._http.post(url, content=pdf, headers=headers)
            except httpx.TimeoutException as exc:
                raise ExtractTimeoutError() from exc
            except httpx.HTTPError as exc:
                raise ExtractUnavailableError(f"No se pudo contactar a extract-service: {exc}") from exc

        return self._parse(response)

    def _parse(self, response: httpx.Response) -> ExtractionResult:
        if response.status_code == 503:
            # El de extract (backpressure) y los de Traefik: circuit breaker
            # abierto o "no available server".
            raise ExtractUnavailableError(
                retry_after=parse_retry_after(response.headers.get("Retry-After"))
            )
        # Respuestas propias del gateway (issue #27). Llamando a extract
        # directo no ocurrian; a traves de Traefik son la forma en que avisa
        # que no llego a una replica.
        if response.status_code == 504:
            # Traefik corto por responseHeaderTimeout.
            raise ExtractTimeoutError()
        if response.status_code == 502:
            # Traefik no pudo hablar con la replica (conexion rechazada o
            # cortada).
            raise ExtractUnavailableError(
                "El gateway no pudo contactar a extract-service (502)"
            )
        if response.status_code == 404:
            # Traefik no tiene ruta para /extract: extract no esta registrado
            # en Consul o no tiene ninguna instancia passing.
            logger.warning(
                "El gateway no tiene ruta hacia extract-service (404): "
                "extract no esta registrado en Consul"
            )
            raise ExtractUnavailableError(
                "El gateway no tiene ruta hacia extract-service (404)"
            )
        if response.status_code != 200:
            raise ExtractProtocolError(
                f"extract-service respondió {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ExtractProtocolError("extract-service no devolvió JSON") from exc

        if not isinstance(payload, dict):
            raise ExtractProtocolError("extract-service devolvió un JSON inesperado")

        content = payload.get("content")
        page_count = payload.get("page_count")
        if not isinstance(content, str) or not isinstance(page_count, int) or isinstance(page_count, bool):
            raise ExtractProtocolError("Faltan 'content' o 'page_count' en la respuesta de extract-service")
        if page_count < 0:
            raise ExtractProtocolError("'page_count' negativo en la respuesta de extract-service")

        return ExtractionResult(content=content, page_count=page_count)
