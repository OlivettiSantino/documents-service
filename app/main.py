"""Application factory and composition root.

Every collaborator is built in one place — the lifespan — and can be replaced
through ``create_app``'s keyword arguments. That is what lets the resilience
tests run with in-memory doubles, no MongoDB and no network, so the acceptance
criteria are covered by the ordinary ``pytest`` run instead of by a suite that
gets skipped.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import documents, health
from app.clock import Clock, SystemClock
from app.config import Settings
from app.db.mongo import create_client, ensure_indexes
from app.exceptions import DocumentsServiceError, RetryableError
from app.logging_config import get_logger
from app.middleware import RequestIdMiddleware
from app.ports import DocumentRepositoryPort, ExtractorClientPort
from app.repositories.document_repository import MongoDocumentRepository
from app.services.bulkhead import Bulkhead
from app.services.circuit_breaker import CircuitBreaker
from app.services.document_service import DocumentService
from app.services.extractor_client import ExtractorClient

logger = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    repository: DocumentRepositoryPort | None = None,
    extractor: ExtractorClientPort | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the ASGI application.

    Args:
        settings: configuration; read from the environment when omitted.
        repository: replaces the MongoDB adapter (tests).
        extractor: replaces the extract-service client (tests).
        clock: replaces the system clock (tests).
    """
    settings = settings or Settings()
    clock = clock or SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mongo_client = None
        http_client = None

        app.state.repository = repository
        app.state.extractor = extractor

        if app.state.repository is None:
            mongo_client = create_client(settings)
            database = mongo_client[settings.MONGO_DB_NAME]
            await ensure_indexes(database)
            app.state.repository = MongoDocumentRepository(database, clock)

        if app.state.extractor is None:
            http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    settings.EXTRACT_TIMEOUT, connect=settings.EXTRACT_CONNECT_TIMEOUT
                ),
                limits=httpx.Limits(max_connections=settings.MAX_CONCURRENT_EXTRACTIONS * 2),
            )
            app.state.extractor = ExtractorClient(
                http_client=http_client,
                base_url=settings.EXTRACT_URL,
                breaker=CircuitBreaker(
                    failure_threshold=settings.BREAKER_FAILURE_THRESHOLD,
                    open_seconds=settings.BREAKER_OPEN_SECONDS,
                    window_seconds=settings.BREAKER_WINDOW_SECONDS,
                    clock=clock,
                ),
                bulkhead=Bulkhead(
                    max_concurrent=settings.MAX_CONCURRENT_EXTRACTIONS,
                    acquire_timeout=settings.BULKHEAD_ACQUIRE_TIMEOUT,
                ),
                clock=clock,
                max_retry_wait=settings.BULKHEAD_ACQUIRE_TIMEOUT,
            )

        app.state.document_service = DocumentService(
            repository=app.state.repository,
            extractor=app.state.extractor,
            max_upload_size=settings.MAX_UPLOAD_SIZE,
        )

        logger.info(
            "Servicio iniciado",
            extra={"extract_url": settings.EXTRACT_URL, "db": settings.MONGO_DB_NAME},
        )
        try:
            yield
        finally:
            if http_client is not None:
                await http_client.aclose()
            if mongo_client is not None:
                mongo_client.close()
            logger.info("Servicio detenido")

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(DocumentsServiceError)
    async def _domain_error_handler(_: Request, exc: DocumentsServiceError) -> JSONResponse:
        """Single place where domain failures become HTTP responses."""
        headers = {}
        if isinstance(exc, RetryableError) and exc.retry_after is not None:
            # Tell the client how long to back off instead of letting it
            # hammer a dependency we already know is struggling.
            headers["Retry-After"] = str(max(1, round(exc.retry_after)))
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.message}, headers=headers
        )

    app.include_router(documents.router)
    app.include_router(health.router)
    return app
