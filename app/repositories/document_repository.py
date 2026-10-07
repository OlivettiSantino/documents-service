"""Motor adapter implementing ``DocumentRepositoryPort``."""

from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.clock import Clock
from app.db.mongo import COLLECTION_NAME
from app.exceptions import DuplicateChecksumError
from app.logging_config import get_logger
from app.models import Document, DocumentCreate

logger = get_logger(__name__)


def _parse_object_id(document_id: str) -> ObjectId | None:
    """Return ``None`` for a malformed id instead of raising.

    Callers turn that into a ``404``, so ``GET /documents/basura`` answers
    "not found" rather than blowing up with a 500.
    """
    # `ObjectId(None)` does not fail -- it mints a brand-new random id, which
    # would silently turn a bad request into a lookup for a document nobody has.
    if not isinstance(document_id, str):
        return None
    try:
        return ObjectId(document_id)
    except InvalidId:
        return None


class MongoDocumentRepository:
    """Document persistence backed by a MongoDB collection."""

    def __init__(self, database: AsyncIOMotorDatabase, clock: Clock) -> None:
        self._collection = database[COLLECTION_NAME]
        self._clock = clock

    async def find_by_id(self, document_id: str) -> Document | None:
        object_id = _parse_object_id(document_id)
        if object_id is None:
            return None
        raw = await self._collection.find_one({"_id": object_id})
        return self._to_model(raw) if raw else None

    async def find_by_checksum(self, checksum: str) -> Document | None:
        raw = await self._collection.find_one({"checksum": checksum})
        return self._to_model(raw) if raw else None

    async def list(self, *, limit: int, offset: int) -> list[Document]:
        # The sort is not optional: `find().skip().limit()` without one has no
        # defined order in MongoDB, so offset=10 could return rows already seen
        # at offset=0. `_id` is monotonic enough for "newest first" and is
        # already indexed, so this costs nothing.
        cursor = self._collection.find().sort("_id", -1).skip(offset).limit(limit)
        return [self._to_model(raw) async for raw in cursor]

    async def count(self) -> int:
        return await self._collection.count_documents({})

    async def create(self, data: DocumentCreate) -> Document:
        now = self._clock.now()
        payload = data.model_dump()
        payload.update(created_at=now, updated_at=now)
        try:
            result = await self._collection.insert_one(payload)
        except DuplicateKeyError as exc:
            raise DuplicateChecksumError() from exc
        return self._to_model({**payload, "_id": result.inserted_id})

    async def delete(self, document_id: str) -> bool:
        object_id = _parse_object_id(document_id)
        if object_id is None:
            return False
        result = await self._collection.delete_one({"_id": object_id})
        return result.deleted_count == 1

    async def ping(self) -> bool:
        """Readiness probe: never raises, just reports."""
        try:
            await self._collection.database.command("ping")
            return True
        except PyMongoError as exc:
            logger.warning("Mongo no responde", extra={"error": str(exc)})
            return False

    @staticmethod
    def _to_model(raw: dict[str, Any]) -> Document:
        return Document(
            id=str(raw["_id"]),
            filename=raw["filename"],
            checksum=raw["checksum"],
            content=raw["content"],
            page_count=raw["page_count"],
            size_bytes=raw["size_bytes"],
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
        )
