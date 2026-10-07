"""Shared test doubles.

The point of these fakes is that the suite runs with no MongoDB, no
extract-service and no real time.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx
import pytest

from app.config import Settings
from app.main import create_app

#: A byte-for-byte valid one-page PDF, built by hand so the tests need no PDF
#: library (this service does not parse PDFs -- it only checks the magic).
SAMPLE_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


class FakeClock:
    """A clock the test moves by hand, so nothing ever sleeps for real."""

    def __init__(self, start: float = 1000.0) -> None:
        self._monotonic = start
        self._now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self._monotonic

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)  # yield to the loop without burning wall time

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)


@asynccontextmanager
async def running_app(app) -> AsyncIterator[httpx.AsyncClient]:
    """Drive the app over ASGI with its lifespan actually running.

    ``TestClient`` would also run the lifespan, but it is synchronous and
    serializes requests, which would later make concurrency assertions pass for
    the wrong reason.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def sample_pdf() -> bytes:
    return SAMPLE_PDF


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(MONGO_DB_NAME="documents_test", MAX_UPLOAD_SIZE=1024 * 1024)


@pytest.fixture
async def client(settings) -> AsyncIterator[httpx.AsyncClient]:
    async with running_app(create_app(settings)) as http_client:
        yield http_client
