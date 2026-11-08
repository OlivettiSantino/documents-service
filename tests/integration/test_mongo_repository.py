"""Repository against a real MongoDB.

Excluded from the default run (``addopts``) because it needs a server. It
covers what a fake cannot honestly prove: that the unique index really
rejects a duplicate checksum (also under concurrent inserts from several
replicas), that timestamps come back timezone-aware, and where the 16 MiB
BSON limit really bites.

It needs a MongoDB published on the host: the one in the compose file does not
publish its port. Same recipe as the README ("Tests")::

    docker run -d --name documents-mongo -p 127.0.0.1:27017:27017 mongo:7.0
    MONGO_URI=mongodb://127.0.0.1:27017 uv run pytest tests/integration -m integration --override-ini addopts=
    docker stop documents-mongo
"""

import asyncio
import os
import uuid

import pytest

from app.config import Settings
from app.db.mongo import ensure_indexes
from app.exceptions import ContentTooLargeError, DuplicateChecksumError
from app.models import DocumentCreate, ExtractionResult
from app.repositories.document_repository import MongoDocumentRepository
from app.services.document_service import DocumentService
from tests.conftest import SAMPLE_PDF, FakeClock

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


@pytest.fixture
async def replicas():
    """Two repositories on the same database, each with its own client and
    connection pool: what two documents containers look like to Mongo."""
    from motor.motor_asyncio import AsyncIOMotorClient

    clients = [
        AsyncIOMotorClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
        for _ in range(2)
    ]
    db_name = f"documents_test_{uuid.uuid4().hex[:8]}"
    await ensure_indexes(clients[0][db_name])
    try:
        yield [MongoDocumentRepository(client[db_name], FakeClock()) for client in clients]
    finally:
        await clients[0].drop_database(db_name)
        for client in clients:
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


async def test_concurrent_inserts_of_the_same_checksum_store_exactly_one(replicas):
    """Escalado horizontal (issue #35): the race between replicas is decided
    by the unique index, not by the read-before-write check. 20 concurrent
    inserts of the same checksum, split across two clients, leave one
    document and 19 ``DuplicateChecksumError``."""
    checksum = "7" * 64
    results = await asyncio.gather(
        *(
            replicas[n % 2].create(make_document(checksum, filename=f"copia-{n}.pdf"))
            for n in range(20)
        ),
        return_exceptions=True,
    )

    created = [r for r in results if not isinstance(r, BaseException)]
    duplicates = [r for r in results if isinstance(r, DuplicateChecksumError)]
    assert len(created) == 1
    assert len(duplicates) == 19
    assert await replicas[0].count() == 1
    assert (await replicas[1].find_by_checksum(checksum)).id == created[0].id


class RendezvousExtractor:
    """Extractor that only answers once every replica has called it.

    That forces the worst case: all of them passed ``find_by_checksum`` before
    anyone inserted, so only the unique index can tell them apart.
    """

    def __init__(self, parties: int) -> None:
        self.calls = 0
        self._parties = parties
        self._everyone_in = asyncio.Event()

    async def extract(self, pdf: bytes, *, request_id: str | None = None) -> ExtractionResult:
        self.calls += 1
        if self.calls == self._parties:
            self._everyone_in.set()
        await asyncio.wait_for(self._everyone_in.wait(), timeout=5)
        return ExtractionResult(content="texto", page_count=1)


async def test_two_replicas_racing_the_same_upload_end_with_one_document(replicas):
    """Same PDF on two replicas at once: both extract (the known cost of the
    race), one stores it and the other answers duplicate with the same id."""
    extractor = RendezvousExtractor(parties=2)
    services = [
        DocumentService(repository=repo, extractor=extractor, max_content_bytes=1024)
        for repo in replicas
    ]

    (first, first_dup), (second, second_dup) = await asyncio.gather(
        *(service.store(filename="informe.pdf", pdf=SAMPLE_PDF) for service in services)
    )

    assert sorted([first_dup, second_dup]) == [False, True]
    assert first.id == second.id
    assert extractor.calls == 2
    assert await replicas[0].count() == 1


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
