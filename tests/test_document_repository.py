"""Mongo adapter, driven by a fake collection.

These cases pin the adapter logic (id parsing, sorting, duplicate mapping)
without needing a server. What a fake cannot prove -- that the unique index
really rejects a second insert, and that timestamps come back timezone-aware --
is covered by ``tests/integration/test_mongo_repository.py``.
"""

from datetime import datetime, timezone

import pytest
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError

from app.exceptions import DuplicateChecksumError
from app.models import DocumentCreate
from app.repositories.document_repository import (
    MongoDocumentRepository,
    _parse_object_id,
)

VALID_ID = "507f1f77bcf86cd799439011"


class FakeCursor:
    """Mimics the chainable, async-iterable cursor Motor returns."""

    def __init__(self, documents):
        self._documents = documents
        self.sorted_by = None
        self.skipped = 0
        self.limited = None

    def sort(self, key, direction):
        self.sorted_by = (key, direction)
        return self

    def skip(self, n):
        self.skipped = n
        return self

    def limit(self, n):
        self.limited = n
        return self

    async def __aiter__(self):
        for document in self._documents[self.skipped : self.skipped + (self.limited or len(self._documents))]:
            yield document


class FakeDatabaseHandle:
    def __init__(self, reachable=True):
        self.reachable = reachable

    async def command(self, name):
        if not self.reachable:
            raise ServerSelectionTimeoutError("sin servidor")
        return {"ok": 1}


class FakeCollection:
    def __init__(self, documents=None, *, reachable=True, duplicate=False):
        self.documents = documents or []
        self.database = FakeDatabaseHandle(reachable)
        self.duplicate = duplicate
        self.cursor = None
        self.deleted_filter = None
        self.inserted = None

    async def find_one(self, query):
        if "_id" in query:
            return next((d for d in self.documents if d["_id"] == query["_id"]), None)
        return next((d for d in self.documents if d["checksum"] == query["checksum"]), None)

    def find(self):
        self.cursor = FakeCursor(self.documents)
        return self.cursor

    async def count_documents(self, query):
        return len(self.documents)

    async def insert_one(self, payload):
        if self.duplicate:
            raise DuplicateKeyError("ux_documents_checksum")
        self.inserted = payload
        return type("Result", (), {"inserted_id": VALID_ID})()

    async def delete_one(self, query):
        self.deleted_filter = query
        found = any(d["_id"] == query["_id"] for d in self.documents)
        return type("Result", (), {"deleted_count": 1 if found else 0})()


class FakeDatabase(dict):
    pass


def make_repository(collection, clock):
    return MongoDocumentRepository(FakeDatabase(documents=collection), clock)


def raw_document(**overrides):
    from bson import ObjectId

    base = {
        "_id": ObjectId(VALID_ID),
        "filename": "informe.pdf",
        "checksum": "a" * 64,
        "content": "texto",
        "page_count": 3,
        "size_bytes": 1234,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    return {**base, **overrides}


class TestParseObjectId:
    def test_accepts_a_valid_id(self):
        assert _parse_object_id(VALID_ID) is not None

    @pytest.mark.parametrize("value", ["basura", "", "123", None])
    def test_a_malformed_id_is_none_not_an_exception(self, value):
        """So the endpoint answers 404 instead of blowing up with a 500."""
        assert _parse_object_id(value) is None


class TestFind:
    async def test_find_by_id(self, clock):
        repository = make_repository(FakeCollection([raw_document()]), clock)
        document = await repository.find_by_id(VALID_ID)
        assert document is not None
        assert document.id == VALID_ID
        assert document.filename == "informe.pdf"

    async def test_find_by_id_with_a_malformed_id_does_not_hit_the_database(self, clock):
        collection = FakeCollection([raw_document()])
        repository = make_repository(collection, clock)
        assert await repository.find_by_id("basura") is None

    async def test_find_by_checksum(self, clock):
        repository = make_repository(FakeCollection([raw_document()]), clock)
        assert await repository.find_by_checksum("a" * 64) is not None
        assert await repository.find_by_checksum("b" * 64) is None


class TestList:
    async def test_always_sorts_so_pagination_is_stable(self, clock):
        """Without an explicit sort, skip/limit has no defined order in MongoDB
        and offset=10 could return rows already seen at offset=0."""
        collection = FakeCollection([raw_document()])
        repository = make_repository(collection, clock)
        await repository.list(limit=10, offset=0)
        assert collection.cursor.sorted_by == ("_id", -1)

    async def test_applies_limit_and_offset(self, clock):
        collection = FakeCollection([raw_document()])
        repository = make_repository(collection, clock)
        await repository.list(limit=5, offset=20)
        assert (collection.cursor.skipped, collection.cursor.limited) == (20, 5)

    async def test_count(self, clock):
        repository = make_repository(FakeCollection([raw_document(), raw_document()]), clock)
        assert await repository.count() == 2


class TestCreate:
    async def test_stamps_both_timestamps_from_the_injected_clock(self, clock):
        collection = FakeCollection()
        repository = make_repository(collection, clock)

        document = await repository.create(
            DocumentCreate(
                filename="a.pdf", checksum="c" * 64, content="x", page_count=1, size_bytes=10
            )
        )

        assert document.created_at == clock.now()
        assert document.updated_at == clock.now()
        assert collection.inserted["checksum"] == "c" * 64

    async def test_a_unique_index_violation_becomes_a_domain_error(self, clock):
        """So the service can turn it into a 200 duplicate instead of a 500."""
        repository = make_repository(FakeCollection(duplicate=True), clock)
        with pytest.raises(DuplicateChecksumError):
            await repository.create(
                DocumentCreate(
                    filename="a.pdf", checksum="c" * 64, content="x", page_count=1, size_bytes=10
                )
            )


class TestDelete:
    async def test_reports_whether_something_was_deleted(self, clock):
        repository = make_repository(FakeCollection([raw_document()]), clock)
        assert await repository.delete(VALID_ID) is True

    async def test_a_malformed_id_is_simply_not_found(self, clock):
        collection = FakeCollection([raw_document()])
        repository = make_repository(collection, clock)
        assert await repository.delete("basura") is False
        assert collection.deleted_filter is None


class TestPing:
    async def test_reports_a_healthy_database(self, clock):
        repository = make_repository(FakeCollection(), clock)
        assert await repository.ping() is True

    async def test_reports_a_dead_database_without_raising(self, clock):
        """Readiness must answer, not explode."""
        repository = make_repository(FakeCollection(reachable=False), clock)
        assert await repository.ping() is False
