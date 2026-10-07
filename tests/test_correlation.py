"""Request correlation (``X-Request-ID``)."""

import uuid

from app.logging_config import request_id_var


async def test_an_incoming_request_id_is_honoured(make_client):
    async with make_client() as client:
        response = await client.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert response.headers["X-Request-ID"] == "trace-abc"


async def test_a_request_id_is_generated_when_none_arrives(make_client):
    async with make_client() as client:
        response = await client.get("/health")

    generated = response.headers["X-Request-ID"]
    assert uuid.UUID(generated)


async def test_each_request_gets_its_own_id(make_client):
    async with make_client() as client:
        first = await client.get("/health")
        second = await client.get("/health")
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


async def test_the_id_is_visible_inside_the_endpoint(make_client):
    """This is the whole reason the middleware is pure ASGI.

    ``BaseHTTPMiddleware`` runs the downstream app in a separate task, so a
    ``ContextVar`` set before ``call_next`` is not reliably readable from the
    endpoint -- every service-layer log line would silently say ``-``.
    """
    seen: list[str] = []

    async with make_client() as client:
        app = client._transport.app  # type: ignore[attr-defined]

        @app.get("/__probe")
        async def probe() -> dict[str, str]:
            seen.append(request_id_var.get())
            return {"ok": "yes"}

        await client.get("/__probe", headers={"X-Request-ID": "visible-dentro"})

    assert seen == ["visible-dentro"]


async def test_the_id_reaches_extract_service(make_client, extractor):
    """Traceability across the hop: the same id the client sent is the one we
    forward to extract-service."""
    from tests.conftest import SAMPLE_PDF

    async with make_client() as client:
        await client.post(
            "/documents",
            files={"file": ("a.pdf", SAMPLE_PDF, "application/pdf")},
            headers={"X-Request-ID": "trace-xyz"},
        )

    assert extractor.request_ids == ["trace-xyz"]


async def test_the_context_var_is_reset_between_requests(make_client):
    async with make_client() as client:
        await client.get("/health", headers={"X-Request-ID": "uno"})
    assert request_id_var.get() == "-"
