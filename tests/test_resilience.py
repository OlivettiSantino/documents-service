"""Resiliencia que sigue viviendo en documents (issue #28).

El circuit breaker, el retry y el limite de concurrencia hacia extract los
aplica Traefik. Lo que queda aca es lo propio del servicio: el timeout del
cliente HTTP, la deduplicacion por checksum y el mapeo de errores a codigos
HTTP (con el ``Retry-After`` de un ``503``).

La app se arma con el wiring de produccion (``create_app`` sin ``extractor``),
asi que el cliente de extract es el real. Solo cambia el destino: respx para
las respuestas del gateway, o un socket de verdad cuando lo que se prueba es
el timeout o la conexion rechazada (respx no aplica los timeouts de httpx).
"""

import asyncio
import socket
import time
from contextlib import asynccontextmanager

import httpx
import respx

from app.config import Settings
from app.main import create_app
from tests.conftest import SAMPLE_PDF, running_app

GATEWAY = "http://traefik"
ENDPOINT = f"{GATEWAY}/extract"


def build_settings(**overrides) -> Settings:
    values = {
        "MONGO_DB_NAME": "documents_test",
        "EXTRACT_URL": GATEWAY,
        "LOG_LEVEL": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)


def build_app(repository, **overrides):
    """Wiring de produccion: el ExtractorClient lo arma el lifespan."""
    app = create_app(build_settings(**overrides), repository=repository)
    return running_app(app)


def upload(pdf: bytes = SAMPLE_PDF, name: str = "informe.pdf"):
    return {"file": (name, pdf, "application/pdf")}


@asynccontextmanager
async def silent_gateway():
    """Un gateway que acepta la conexion, lee el pedido y nunca contesta."""
    connections = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(writer)
        try:
            while await reader.read(65536):
                pass
        except ConnectionError:
            pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", connections
    finally:
        for writer in connections:
            writer.close()
        server.close()
        await server.wait_closed()


def closed_port_url() -> str:
    """URL a un puerto local donde no escucha nadie: conexion rechazada."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@respx.mock
async def test_a_gateway_503_is_propagated_with_retry_after_and_reads_keep_working(
    repository,
):
    """Con el circuito de Traefik abierto (o sin replicas), el POST responde
    503 con el Retry-After del gateway y los GET no se enteran."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(503, headers={"Retry-After": "10"})
    )

    async with build_app(repository) as client:
        rejected = await client.post("/documents", files=upload())
        listing = await client.get("/documents")
        ready = await client.get("/health/ready")

    assert rejected.status_code == 503
    assert rejected.headers["Retry-After"] == "10"
    assert route.call_count == 1  # documents no reintenta: eso es de Traefik
    assert listing.status_code == 200
    assert listing.json()["total"] == 0
    assert ready.status_code == 200


@respx.mock
async def test_a_gateway_503_without_retry_after_carries_no_hint(repository):
    """El 503 del circuit breaker de Traefik no trae Retry-After: no se inventa."""
    respx.post(ENDPOINT).mock(return_value=httpx.Response(503))

    async with build_app(repository) as client:
        rejected = await client.post("/documents", files=upload())

    assert rejected.status_code == 503
    assert "Retry-After" not in rejected.headers


@respx.mock
async def test_a_known_pdf_is_served_as_duplicate_while_the_gateway_fails(repository):
    """El dedupe por checksum va antes de llamar a extract: un PDF ya guardado
    se sirve aunque el gateway este rechazando todo."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"content": "texto", "page_count": 1})
    )

    async with build_app(repository) as client:
        created = await client.post("/documents", files=upload())
        assert created.status_code == 201

        route.mock(return_value=httpx.Response(503))
        failing = await client.post("/documents", files=upload(SAMPLE_PDF + b"nuevo"))
        duplicate = await client.post("/documents", files=upload())

    assert failing.status_code == 503
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["id"] == created.json()["id"]
    assert route.call_count == 2  # el duplicado no llego al gateway


async def test_a_slow_gateway_becomes_a_504_after_extract_timeout_and_is_not_retried(
    repository,
):
    timeout = 0.3
    async with (
        silent_gateway() as (url, connections),
        build_app(repository, EXTRACT_URL=url, EXTRACT_TIMEOUT=timeout) as client,
    ):
        started = time.perf_counter()
        response = await client.post("/documents", files=upload())
        elapsed = time.perf_counter() - started

    assert response.status_code == 504
    assert timeout <= elapsed < timeout + 1.0
    assert len(connections) == 1  # un solo intento


async def test_a_connection_error_becomes_a_fast_503(repository):
    async with build_app(repository, EXTRACT_URL=closed_port_url()) as client:
        started = time.perf_counter()
        response = await client.post("/documents", files=upload())
        elapsed = time.perf_counter() - started

    assert response.status_code == 503
    assert elapsed < 0.5
