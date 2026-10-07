"""HTTP client for extract-service, wrapped in the resilience layers.

Layer order, outermost first::

    breaker.call( retry(1, only on 503) -> bulkhead.slot() -> timeout -> httpx )

* The **breaker is outermost** so that, once open, a request is rejected in
  microseconds without consuming a bulkhead slot. That is literally the
  acceptance criterion ("503 in milliseconds with extract-service down").
* The **retry sits inside the breaker** so both attempts count as a single
  outcome: one 503 that was retried and failed is one failure, not two.
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
from app.services.bulkhead import Bulkhead
from app.services.circuit_breaker import CircuitBreaker

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
        base_url: str,
        breaker: CircuitBreaker,
        bulkhead: Bulkhead,
        clock: Clock,
        max_retry_wait: float = 1.0,
    ) -> None:
        self._http = http_client
        self._url = f"{base_url.rstrip('/')}/extract"
        self._breaker = breaker
        self._bulkhead = bulkhead
        self._clock = clock
        self._max_retry_wait = max_retry_wait

    @property
    def breaker_state(self) -> str:
        return self._breaker.state.value

    @property
    def retry_after(self) -> float:
        return self._breaker.retry_after()

    async def extract(self, pdf: bytes, *, request_id: str | None = None) -> ExtractionResult:
        """Extract text from ``pdf`` via extract-service."""
        return await self._breaker.call(lambda: self._with_retry(pdf, request_id))

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

        async with self._bulkhead.slot():
            try:
                response = await self._http.post(self._url, content=pdf, headers=headers)
            except httpx.TimeoutException as exc:
                raise ExtractTimeoutError() from exc
            except httpx.HTTPError as exc:
                raise ExtractUnavailableError(f"No se pudo contactar a extract-service: {exc}") from exc

        return self._parse(response)

    def _parse(self, response: httpx.Response) -> ExtractionResult:
        if response.status_code == 503:
            raise ExtractUnavailableError(
                retry_after=parse_retry_after(response.headers.get("Retry-After"))
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
