"""Request correlation (``X-Request-ID``)."""

import uuid

from app.config import Settings
from app.logging_config import request_id_var
from app.main import create_app
from tests.conftest import running_app


async def test_an_incoming_request_id_is_honoured(client):
    response = await client.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert response.headers["X-Request-ID"] == "trace-abc"


async def test_a_request_id_is_generated_when_none_arrives(client):
    response = await client.get("/health")
    assert uuid.UUID(response.headers["X-Request-ID"])


async def test_each_request_gets_its_own_id(client):
    first = await client.get("/health")
    second = await client.get("/health")
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


async def test_the_id_is_visible_inside_the_endpoint(settings):
    """This is the whole reason the middleware is pure ASGI.

    ``BaseHTTPMiddleware`` runs the downstream app in a separate task, so a
    ``ContextVar`` set before ``call_next`` is not reliably readable from the
    endpoint -- every service-layer log line would silently say ``-``.
    """
    seen: list[str] = []
    app = create_app(settings)

    @app.get("/__probe")
    async def probe() -> dict[str, str]:
        seen.append(request_id_var.get())
        return {"ok": "yes"}

    async with running_app(app) as client:
        await client.get("/__probe", headers={"X-Request-ID": "visible-dentro"})

    assert seen == ["visible-dentro"]


async def test_the_context_var_is_reset_between_requests(client):
    await client.get("/health", headers={"X-Request-ID": "uno"})
    assert request_id_var.get() == "-"
