"""Repository against a real MongoDB.

Excluded from the default run (``addopts``) because it needs a server. It
covers what a fake cannot honestly prove: that the unique index really
rejects a duplicate checksum, that timestamps come back timezone-aware, and
where the 16 MiB BSON limit really bites.

Run it with::

    docker run -d --rm --name mongo-test -p 27017:27017 mongo:7.0
    MONGO_URI=mongodb://localhost:27017 uv run pytest tests/integration -m integration --override-ini addopts=
"""

import os
import uuid

import pytest

from app.config import Settings
from app.db.mongo import ensure_indexes
from app.exceptions import ContentTooLargeError, DuplicateChecksumError
from app.models import DocumentCreate
from app.repositories.document_repository import MongoDocumentRepository
from tests.conftest import FakeClock

pytestmark = pytest.mark.integration

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")


@pytest.fixture
async def repository():
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
    db_name = f"documents_test_{uuid.uuid4().hex[:8]}"
    database = client[db_name]
    await ensure_indexes(database)
    try:
        yield MongoDocumentRepository(database, FakeClock())
    finally:
        await client.drop_database(db_name)
        client.close()


def make_document(checksum: str, **overrides) -> DocumentCreate:
    base = {
        "filename": "informe.pdf",
        "checksum": checksum,
        "content": "texto extraído",
        "page_count": 3,
        "size_bytes": 2048,
    }
    return DocumentCreate(**{**base, **overrides})


async def test_round_trip(repository):
    created = await repository.create(make_document("a" * 64))
    found = await repository.find_by_id(created.id)

    assert found is not None
    assert found.model_dump() == created.model_dump()


async def test_timestamps_come_back_timezone_aware(repository):
    """Without ``tz_aware=True`` the same document serializes with an offset
    right after creation and naive after a restart."""
    created = await repository.create(make_document("b" * 64))
    reread = await repository.find_by_id(created.id)

    assert reread.created_at.tzinfo is not None
    assert reread.updated_at.tzinfo is not None


async def test_the_unique_index_rejects_a_duplicate_checksum(repository):
    """The real idempotency guarantee, not the read-before-write check."""
    await repository.create(make_document("c" * 64))

    with pytest.raises(DuplicateChecksumError):
        await repository.create(make_document("c" * 64, filename="otro-nombre.pdf"))


async def test_find_by_checksum(repository):
    created = await repository.create(make_document("d" * 64))
    assert (await repository.find_by_checksum("d" * 64)).id == created.id
    assert await repository.find_by_checksum("e" * 64) is None


async def test_pagination_is_stable_across_pages(repository):
    for n in range(10):
        await repository.create(make_document(f"{n:064d}"))

    first = await repository.list(limit=5, offset=0)
    second = await repository.list(limit=5, offset=5)

    assert len({d.id for d in first} & {d.id for d in second}) == 0
    assert await repository.count() == 10


async def test_newest_documents_come_first(repository):
    older = await repository.create(make_document("1" * 64))
    newer = await repository.create(make_document("2" * 64))

    assert [d.id for d in await repository.list(limit=10, offset=0)] == [newer.id, older.id]


async def test_delete(repository):
    created = await repository.create(make_document("f" * 64))

    assert await repository.delete(created.id) is True
    assert await repository.delete(created.id) is False
    assert await repository.find_by_id(created.id) is None


async def test_ping_reports_a_live_database(repository):
    assert await repository.ping() is True


async def test_content_at_max_content_bytes_fits_in_a_mongo_document(repository):
    """The default 15 MiB leaves enough headroom for the other fields."""
    content = "a" * Settings().MAX_CONTENT_BYTES
    created = await repository.create(make_document("9" * 64, content=content))

    found = await repository.find_by_id(created.id)
    assert len(found.content) == len(content)


async def test_content_over_the_bson_limit_becomes_a_domain_error(repository):
    """Safety net: if something gets past the service check, Mongo's
    DocumentTooLarge must not escape as a 500."""
    content = "a" * (17 * 1024 * 1024)

    with pytest.raises(ContentTooLargeError):
        await repository.create(make_document("8" * 64, content=content))
    assert await repository.find_by_checksum("8" * 64) is None
