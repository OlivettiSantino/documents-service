"""Bulkhead.

A semaphore caps how many extractions can be in flight at once. When the pool
is exhausted the request is shed quickly instead of queueing behind work that
will outlive its own usefulness — which is what keeps ``GET``, ``DELETE`` and
``/health`` responsive while extract-service is saturated.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.exceptions import BulkheadFullError
from app.logging_config import get_logger

logger = get_logger(__name__)


class Bulkhead:
    """Bounded concurrency with a bounded wait."""

    def __init__(self, *, max_concurrent: int, acquire_timeout: float, name: str = "extract") -> None:
        self._max_concurrent = max_concurrent
        self._acquire_timeout = acquire_timeout
        self._name = name
        self._semaphore = asyncio.Semaphore(max_concurrent)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def available(self) -> int:
        """Free slots right now."""
        # asyncio exposes no public accessor for the remaining permits.
        return self._semaphore._value

    @property
    def in_use(self) -> int:
        return self._max_concurrent - self.available

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold a slot for the duration of the block.

        Raises:
            BulkheadFullError: if no slot frees up within ``acquire_timeout``.
        """
        try:
            async with asyncio.timeout(self._acquire_timeout):
                await self._semaphore.acquire()
        except TimeoutError:
            logger.warning(
                "Bulkhead saturado",
                extra={"bulkhead": self._name, "max_concurrent": self._max_concurrent},
            )
            raise BulkheadFullError(retry_after=self._acquire_timeout) from None

        try:
            yield
        finally:
            self._semaphore.release()
