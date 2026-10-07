"""Health Check API."""

from app.exceptions import CircuitOpenError
from tests.conftest import StubExtractor


async def test_liveness_is_always_ok(make_client):
    async with make_client() as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_liveness_does_not_touch_the_database(make_client, repository):
    """The Docker HEALTHCHECK watches this endpoint. Tying it to the database
    would let a brief Mongo blip restart a process that was perfectly fine."""
    repository.up = False
    async with make_client() as client:
        response = await client.get("/health")
    assert response.status_code == 200


async def test_readiness_is_ok_when_our_own_database_answers(make_client):
    async with make_client() as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "up", "extract_circuit": "closed"}


async def test_readiness_fails_when_our_own_database_is_down(make_client, repository):
    repository.up = False
    async with make_client() as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["database"] == "down"


async def test_readiness_reports_the_circuit_but_stays_ready(make_client):
    """A broken extract-service does not make us unready: we can still serve
    reads and shed writes correctly -- that is what the breaker is for."""
    extractor = StubExtractor(error=CircuitOpenError(retry_after=9))
    extractor.breaker_state = "open"

    async with make_client(extractor=extractor) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["extract_circuit"] == "open"
