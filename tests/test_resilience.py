"""Acceptance criteria for resilience.

These run against the real :class:`ExtractorClient`, with only the network
mocked out, so the breaker, the bulkhead, the retry rule and the HTTP mapping
are all exercised together -- end to end minus the socket.
"""

import asyncio
import time

import httpx
import respx

from app.main import create_app
from app.services.bulkhead import Bulkhead
from app.services.circuit_breaker import CircuitBreaker
from app.services.extractor_client import ExtractorClient
from tests.conftest import SAMPLE_PDF, running_app

BASE_URL = "http://extract:8001"
ENDPOINT = f"{BASE_URL}/extract"


def build_app(settings, repository, clock, *, max_concurrent=2, acquire_timeout=0.05):
    """The production wiring, with only the socket replaced by respx."""
    extractor = ExtractorClient(
        http_client=httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=1.0)),
        base_url=BASE_URL,
        breaker=CircuitBreaker(
            failure_threshold=settings.BREAKER_FAILURE_THRESHOLD,
            open_seconds=settings.BREAKER_OPEN_SECONDS,
            window_seconds=settings.BREAKER_WINDOW_SECONDS,
            clock=clock,
        ),
        bulkhead=Bulkhead(max_concurrent=max_concurrent, acquire_timeout=acquire_timeout),
        clock=clock,
        max_retry_wait=1.0,
    )
    app = create_app(settings, repository=repository, extractor=extractor, clock=clock)
    return running_app(app), extractor


def upload(pdf: bytes = SAMPLE_PDF, name: str = "informe.pdf"):
    return {"file": (name, pdf, "application/pdf")}


@respx.mock
async def test_with_extract_down_the_circuit_opens_and_reads_keep_working(
    settings, repository, clock
):
    """The headline acceptance criterion.

    With extract-service down, ``POST /documents`` answers 503 in milliseconds
    once the circuit is open, and ``GET /documents`` keeps serving.
    """
    route = respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("conexion rechazada"))
    context, extractor = build_app(settings, repository, clock)

    async with context as client:
        # Trip the breaker: threshold failures, each one a real attempt.
        for n in range(settings.BREAKER_FAILURE_THRESHOLD):
            response = await client.post("/documents", files=upload(SAMPLE_PDF + bytes([n])))
            assert response.status_code == 503

        assert extractor.breaker_state == "open"
        attempts_before = route.call_count

        # From here on we must fail fast, without touching the network at all.
        started = time.perf_counter()
        rejected = await client.post("/documents", files=upload(b"%PDF-nuevo"))
        elapsed_ms = (time.perf_counter() - started) * 1000

        assert rejected.status_code == 503
        assert int(rejected.headers["Retry-After"]) >= 1
        assert elapsed_ms < 50
        assert route.call_count == attempts_before  # no new call reached extract

        # Reads are unaffected by the writer-side outage.
        listing = await client.get("/documents")
        assert listing.status_code == 200
        assert listing.json()["total"] == 0

        health = await client.get("/health")
        ready = await client.get("/health/ready")
        assert health.status_code == 200
        assert ready.status_code == 200
        assert ready.json()["extract_circuit"] == "open"


@respx.mock
async def test_the_service_recovers_once_extract_comes_back(settings, repository, clock):
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("caido"))
    context, extractor = build_app(settings, repository, clock)

    async with context as client:
        for n in range(settings.BREAKER_FAILURE_THRESHOLD):
            await client.post("/documents", files=upload(SAMPLE_PDF + bytes([n])))
        assert extractor.breaker_state == "open"

        # extract-service comes back, and the open window elapses.
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"content": "recuperado", "page_count": 2})
        )
        clock.advance(settings.BREAKER_OPEN_SECONDS)

        response = await client.post("/documents", files=upload(b"%PDF-recuperado"))

    assert response.status_code == 201
    assert response.json()["content"] == "recuperado"
    assert extractor.breaker_state == "closed"


@respx.mock
async def test_a_saturated_extract_sheds_writes_while_reads_stay_fast(
    settings, repository, clock
):
    """Bulkhead: slow extractions cannot starve the rest of the service."""
    context, _ = build_app(settings, repository, clock, max_concurrent=1, acquire_timeout=0.01)

    # The handler announces that it holds the only slot, so the test never has
    # to guess how long scheduling takes.
    holding_the_slot = asyncio.Event()
    release = asyncio.Event()

    async def slow(request):
        holding_the_slot.set()
        await release.wait()
        return httpx.Response(200, json={"content": "lento", "page_count": 1})

    respx.post(ENDPOINT).mock(side_effect=slow)

    async with context as client:
        in_flight = asyncio.create_task(client.post("/documents", files=upload(b"%PDF-lento")))
        await holding_the_slot.wait()

        shed = await client.post("/documents", files=upload(b"%PDF-otro"))
        assert shed.status_code == 503
        assert "Retry-After" in shed.headers

        started = time.perf_counter()
        listing = await client.get("/documents")
        read_ms = (time.perf_counter() - started) * 1000

        assert listing.status_code == 200
        assert read_ms < 50

        release.set()
        assert (await in_flight).status_code == 201


@respx.mock
async def test_a_known_pdf_is_served_even_with_extract_down(settings, repository, clock):
    """Idempotency by checksum keeps working during an outage: the duplicate
    path never calls extract, so it cannot be blocked by the circuit."""
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"content": "texto", "page_count": 1})
    )
    context, extractor = build_app(settings, repository, clock)

    async with context as client:
        created = await client.post("/documents", files=upload())
        assert created.status_code == 201

        respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("caido"))
        for n in range(settings.BREAKER_FAILURE_THRESHOLD):
            await client.post("/documents", files=upload(SAMPLE_PDF + bytes([n + 1])))
        assert extractor.breaker_state == "open"

        started = time.perf_counter()
        duplicate = await client.post("/documents", files=upload())
        elapsed_ms = (time.perf_counter() - started) * 1000

    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["id"] == created.json()["id"]
    assert elapsed_ms < 50


@respx.mock
async def test_a_slow_extract_becomes_a_504_and_is_not_retried(settings, repository, clock):
    route = respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout("tardo demasiado"))
    context, _ = build_app(settings, repository, clock)

    async with context as client:
        response = await client.post("/documents", files=upload())

    assert response.status_code == 504
    assert route.call_count == 1
