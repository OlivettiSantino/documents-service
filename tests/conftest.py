"""Shared test doubles.

The point of these fakes is that the whole suite — including the resilience
tests — runs with no MongoDB and no extract-service.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx
import pytest

from app.config import Settings
from app.exceptions import DuplicateChecksumError
from app.main import create_app
from app.models import Document, DocumentCreate, ExtractionResult

#: A byte-for-byte valid one-page PDF, built by hand so the tests need no
#: PDF library (this service does not parse PDFs — it only checks the magic).
SAMPLE_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


class FakeClock:
    """A clock stopped at a known instant, so persisted timestamps are exact."""

    def __init__(self) -> None:
        self._now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now


class InMemoryDocumentRepository:
    """``DocumentRepositoryPort`` backed by a dict, including the unique index."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self._documents: dict[str, Document] = {}
        self._clock = clock or FakeClock()
        self._next_id = 1
        self.up = True
        self.create_calls = 0

    async def find_by_id(self, document_id: str) -> Document | None:
        return self._documents.get(document_id)

    async def find_by_checksum(self, checksum: str) -> Document | None:
        return next((d for d in self._documents.values() if d.checksum == checksum), None)

    async def list(self, *, limit: int, offset: int) -> list[Document]:
        ordered = sorted(self._documents.values(), key=lambda d: d.id, reverse=True)
        return ordered[offset : offset + limit]

    async def count(self) -> int:
        return len(self._documents)

    async def create(self, data: DocumentCreate) -> Document:
        self.create_calls += 1
        if await self.find_by_checksum(data.checksum):
            raise DuplicateChecksumError()
        now = self._clock.now()
        document = Document(
            id=f"{self._next_id:024d}", created_at=now, updated_at=now, **data.model_dump()
        )
        self._next_id += 1
        self._documents[document.id] = document
        return document

    async def delete(self, document_id: str) -> bool:
        return self._documents.pop(document_id, None) is not None

    async def ping(self) -> bool:
        return self.up


class StubExtractor:
    """``ExtractorClientPort`` that records calls and can be told to misbehave."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        page_count: int = 3,
        content: str | None = None,
    ) -> None:
        self.error = error
        self.page_count = page_count
        self.content = content
        self.calls = 0
        self.request_ids: list[str | None] = []

    async def extract(self, pdf: bytes, *, request_id: str | None = None) -> ExtractionResult:
        self.calls += 1
        self.request_ids.append(request_id)
        if self.error is not None:
            raise self.error
        content = self.content if self.content is not None else f"texto de {len(pdf)} bytes"
        return ExtractionResult(content=content, page_count=self.page_count)


@pytest.fixture
def sample_pdf() -> bytes:
    return SAMPLE_PDF


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repository(clock: FakeClock) -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository(clock)


@pytest.fixture
def extractor() -> StubExtractor:
    return StubExtractor()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        MONGO_DB_NAME="documents_test",
        MAX_UPLOAD_SIZE=1024 * 1024,
    )


@asynccontextmanager
async def running_app(app) -> AsyncIterator[httpx.AsyncClient]:
    """Drive the app over ASGI with its lifespan actually running.

    ``ASGITransport`` alone never runs startup, so the collaborators built in
    the lifespan would be missing. ``TestClient`` would run it but is
    synchronous and serializes requests, which would make the concurrency
    assertions in the upload admission tests pass for the wrong reason.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def make_client(settings, repository, extractor, clock):
    """Build an HTTP client over the app, with doubles wired in."""

    def _make(**overrides):
        app = create_app(
            overrides.pop("settings", settings),
            repository=overrides.pop("repository", repository),
            extractor=overrides.pop("extractor", extractor),
            clock=overrides.pop("clock", clock),
        )
        return running_app(app)

    return _make
