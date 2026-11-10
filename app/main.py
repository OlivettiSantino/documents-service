"""Application factory and composition root.

Every collaborator is built in one place — the lifespan — and can be replaced
through ``create_app``'s keyword arguments. That is what lets the tests run
with in-memory doubles and no MongoDB, so the acceptance criteria are covered
by the ordinary ``pytest`` run instead of by a suite that gets skipped.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from app.api import documents, health
from app.clock import Clock, SystemClock
from app.config import Settings
from app.db.mongo import create_client, ensure_indexes
from app.exceptions import (
    DatabaseUnavailableError,
    DocumentsServiceError,
    RetryableError,
)
from app.logging_config import get_logger
from app.middleware import RequestIdMiddleware
from app.ports import DocumentRepositoryPort, ExtractorClientPort
from app.repositories.document_repository import MongoDocumentRepository
from app.services.consul import (
    ServiceRegistrarPort,
    build_registrar,
    keep_registered,
    traefik_tags,
)
from app.services.document_service import DocumentService
from app.services.extractor_client import ExtractorClient

logger = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    repository: DocumentRepositoryPort | None = None,
    extractor: ExtractorClientPort | None = None,
    clock: Clock | None = None,
    consul_registrar: ServiceRegistrarPort | None = None,
) -> FastAPI:
    """Build the ASGI application.

    Args:
        settings: configuration; read from the environment when omitted.
        repository: replaces the MongoDB adapter (tests).
        extractor: replaces the extract-service client (tests).
        clock: replaces the system clock (tests).
        consul_registrar: replaces the Consul registration (tests).
    """
    settings = settings or Settings()
    clock = clock or SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mongo_client = None
        http_client = None
        consul_client = None
        registrar = consul_registrar
        registration_task: asyncio.Task[None] | None = None

        app.state.repository = repository
        app.state.extractor = extractor

        if app.state.repository is None:
            mongo_client = create_client(settings)
            database = mongo_client[settings.MONGO_DB_NAME]
            await ensure_indexes(database)
            app.state.repository = MongoDocumentRepository(database, clock)

        if app.state.extractor is None:
            # Traefik (issue #27): documents ya no elige replicas. Siempre llama
            # a EXTRACT_URL, que en el compose es el gateway (http://traefik);
            # Traefik descubre las replicas de extract en Consul y balancea.
            # CONSUL_ENABLED solo controla el registro de documents, mas abajo.
            http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    settings.EXTRACT_TIMEOUT, connect=settings.EXTRACT_CONNECT_TIMEOUT
                ),
                # Cada upload admitido hace a lo sumo una llamada a extract, asi
                # que el cupo de uploads ya acota las conexiones abiertas.
                limits=httpx.Limits(max_connections=settings.MAX_CONCURRENT_UPLOADS),
            )
            app.state.extractor = ExtractorClient(
                http_client=http_client,
                base_url=settings.EXTRACT_URL,
            )

        app.state.document_service = DocumentService(
            repository=app.state.repository,
            extractor=app.state.extractor,
            max_content_bytes=settings.MAX_CONTENT_BYTES,
        )
        # Control de admision de uploads (issue #30). Se crea aca, dentro del
        # loop que va a atender los pedidos.
        app.state.upload_slots = asyncio.Semaphore(settings.MAX_CONCURRENT_UPLOADS)

        # Service discovery (issue #23): opcional y nunca fatal. Sin Consul,
        # o si el registro falla, el servicio funciona igual. Con los tags de
        # Traefik (issue #27), registrarse es tambien publicarse en el gateway.
        if settings.CONSUL_ENABLED:
            consul_client = httpx.AsyncClient(timeout=httpx.Timeout(settings.CONSUL_TIMEOUT))
            if registrar is None:
                registrar = build_registrar(
                    http_client=consul_client,
                    consul_url=settings.CONSUL_HTTP_ADDR,
                    token=settings.CONSUL_HTTP_TOKEN,
                    port=settings.PORT,
                    tags=traefik_tags(settings),
                )
            if registrar is not None:
                # En segundo plano: si Consul todavia no esta listo (p. ej. ACL
                # inicializandose), el servicio arranca igual y sigue
                # reintentando hasta publicarse en Traefik. Ya registrado,
                # vuelve a registrarse si Consul lo borra (issue #51).
                registration_task = asyncio.create_task(
                    keep_registered(registrar), name="consul-registro"
                )

        logger.info(
            "Servicio iniciado",
            extra={
                # Destino fijo de extract: el gateway (http://traefik) o, en
                # rollback, extract directo (http://extract:8001).
                "extract_destino": settings.EXTRACT_URL,
                "consul_registro": settings.CONSUL_ENABLED,
                # El registro corre en segundo plano: al lograrlo se loguea
                # consul_registrado=True (ver keep_registered).
                "consul_registrado": False,
                "db": settings.MONGO_DB_NAME,
            },
        )
        try:
            yield
        finally:
            if registration_task is not None:
                # Al cancelarse, keep_registered desregistra, y solo si se
                # llego a registrar. Se espera antes de cerrar consul_client,
                # que es el que usa para desregistrar.
                registration_task.cancel()
                await asyncio.gather(registration_task, return_exceptions=True)
            if consul_client is not None:
                await consul_client.aclose()
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
        return _error_response(exc)

    @app.exception_handler(PyMongoError)
    async def _database_error_handler(_: Request, exc: PyMongoError) -> JSONResponse:
        """Mongo down (or any driver error that got this far) -> 503.

        The repository still translates ``DuplicateKeyError`` and
        ``DocumentTooLarge`` itself, and readiness catches its own ``ping``
        failure: this only sees what nobody handled before. The driver's text
        names hosts and topology, so it only goes to the log.
        """
        logger.error(
            "MongoDB no disponible",
            extra={"error_type": type(exc).__name__, "error": str(exc)},
        )
        return _error_response(DatabaseUnavailableError(retry_after=settings.MONGO_RETRY_AFTER))

    app.include_router(documents.router)
    app.include_router(health.router)
    return app


def _error_response(exc: DocumentsServiceError) -> JSONResponse:
    """``{"detail": <fixed message>}``, plus ``Retry-After`` when it applies."""
    headers = {}
    if isinstance(exc, RetryableError) and exc.retry_after is not None:
        # Tell the client how long to back off instead of letting it
        # hammer a dependency we already know is struggling.
        headers["Retry-After"] = str(max(1, round(exc.retry_after)))
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.message}, headers=headers
    )
