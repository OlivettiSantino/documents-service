"""Ports (hexagonal architecture).

The service layer depends on these ``Protocol`` definitions, never on Motor or
httpx. That is what lets the resilience tests run with in-memory doubles and no
network at all.
"""

from typing import Protocol

from app.models import Document, DocumentCreate, ExtractionResult


class DocumentRepositoryPort(Protocol):
    """Persistence of documents.

    Intentionally narrow: there is no ``update`` because ``PUT`` is out of
    contract for this service (ISP).
    """

    async def find_by_id(self, document_id: str) -> Document | None: ...

    async def find_by_checksum(self, checksum: str) -> Document | None: ...

    async def list(self, *, limit: int, offset: int) -> list[Document]: ...

    async def count(self) -> int: ...

    async def create(self, data: DocumentCreate) -> Document:
        """Persist a document.

        Raises:
            DuplicateChecksumError: if the unique checksum index rejects it.
        """
        ...

    async def delete(self, document_id: str) -> bool: ...

    async def ping(self) -> bool:
        """Report whether the backing store answers. Never raises."""
        ...


class ExtractorClientPort(Protocol):
    """Text extraction delegated to extract-service."""

    async def extract(self, pdf: bytes, *, request_id: str | None = None) -> ExtractionResult:
        """Extract text from ``pdf``.

        Raises:
            CircuitOpenError, BulkheadFullError, ExtractUnavailableError,
            ExtractTimeoutError, ExtractProtocolError
        """
        ...

    @property
    def breaker_state(self) -> str:
        """Current circuit state, surfaced by ``/health/ready``."""
        ...
