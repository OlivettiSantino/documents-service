"""MongoDB wiring — this service's own database, shared with nobody.

``tz_aware=True`` matters more than it looks: without it a document that was
just created serializes with ``+00:00`` while the same document read back from
Mongo serializes naive. Same resource, two representations, depending only on
whether the process had restarted.
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import Settings
from app.logging_config import get_logger

logger = get_logger(__name__)

COLLECTION_NAME = "documents"
CHECKSUM_INDEX = "ux_documents_checksum"


def create_client(settings: Settings) -> AsyncIOMotorClient:
    """Build the Motor client with a short server-selection timeout.

    The short timeout is what lets ``/health/ready`` answer ``503`` quickly
    instead of hanging for the driver's 30 s default.
    """
    return AsyncIOMotorClient(
        settings.MONGO_URI,
        tz_aware=True,
        serverSelectionTimeoutMS=settings.MONGO_TIMEOUT_MS,
    )


async def ensure_indexes(database: AsyncIOMotorDatabase) -> None:
    """Create the unique checksum index.

    ``create_index`` is idempotent, which is why this service needs no
    migration engine (KISS). The index — not the read-before-write check in the
    service — is the real idempotency guarantee for duplicate uploads.
    """
    await database[COLLECTION_NAME].create_index(
        "checksum", unique=True, name=CHECKSUM_INDEX
    )
    logger.info("Índices asegurados", extra={"collection": COLLECTION_NAME})
