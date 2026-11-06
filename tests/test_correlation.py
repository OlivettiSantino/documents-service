"""Request correlation (``X-Request-ID``)."""

import logging
import re
import uuid

import pytest

from app.logging_config import request_id_var
from app.middleware import RequestIdMiddleware, resolve_request_id
from tests.conftest import SAMPLE_PDF

GENERATED = re.compile(r"[0-9a-f]{32}")


def upload():
    return {"file": ("a.pdf", SAMPLE_PDF, "application/pdf")}


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


# --- Validacion del X-Request-ID entrante (issue #29) -----------------------


@pytest.mark.parametrize(
    "request_id",
    ["abc-123", "trace-xyz", "A" * 64, "0", "550e8400-e29b-41d4-a716-446655440000"],
)
async def test_a_valid_request_id_is_kept_and_forwarded_to_extract(
    make_client, extractor, request_id
):
    async with make_client() as client:
        response = await client.post(
            "/documents", files=upload(), headers={"X-Request-ID": request_id}
        )

    assert response.headers["X-Request-ID"] == request_id
    assert extractor.request_ids == [request_id]


INVALID_IDS = [
    pytest.param("hola mundo", id="espacio"),
    pytest.param("con\nsalto", id="salto-de-linea"),
    pytest.param("con\r\nX-Falso: 1", id="crlf"),
    pytest.param("A" * 65, id="65-caracteres"),
    pytest.param("acción".encode(), id="acentos"),
    pytest.param("", id="vacio"),
    pytest.param("a_b", id="guion-bajo"),
    pytest.param("a.b/c", id="puntuacion"),
    pytest.param(b"ctl\x00\x1b[31m", id="control"),
]


@pytest.mark.parametrize("request_id", INVALID_IDS)
async def test_an_invalid_request_id_is_replaced_everywhere_by_the_same_new_one(
    make_client, extractor, request_id
):
    async with make_client() as client:
        response = await client.post(
            "/documents", files=upload(), headers={"X-Request-ID": request_id}
        )

    generated = response.headers["X-Request-ID"]
    assert GENERATED.fullmatch(generated)
    assert extractor.request_ids == [generated]


@pytest.mark.parametrize("request_id", INVALID_IDS)
def test_resolve_request_id_rejects_what_does_not_match(request_id):
    raw = request_id if isinstance(request_id, bytes) else request_id.encode("latin-1")
    assert GENERATED.fullmatch(resolve_request_id(raw))


def test_a_trailing_newline_is_not_accepted():
    """Con ``re.match`` y ``$`` pasaria: ``$`` acepta un "\\n" final."""
    assert resolve_request_id(b"abc-123\n") != "abc-123"


async def test_the_replacement_is_logged_without_the_whole_value(make_client, caplog):
    received = "x" * 60 + "\n" + "secreto-que-no-va-al-log" * 10

    async with make_client() as client:
        with caplog.at_level(logging.INFO, logger="app.middleware"):
            response = await client.get("/health", headers={"X-Request-ID": received})

    [record] = [r for r in caplog.records if r.name == "app.middleware"]
    assert record.levelno == logging.INFO
    assert "X-Request-ID inválido" in record.getMessage()
    assert record.request_id_recibido == "x" * 60 + "\\n" + "sec"  # 64 caracteres, escapados
    assert "\n" not in record.request_id_recibido
    assert "secreto-que" not in record.request_id_recibido
    assert record.request_id_largo == len(received)
    assert GENERATED.fullmatch(response.headers["X-Request-ID"])


async def test_a_valid_or_missing_request_id_is_not_logged(make_client, caplog):
    async with make_client() as client:
        with caplog.at_level(logging.DEBUG, logger="app.middleware"):
            await client.get("/health", headers={"X-Request-ID": "abc-123"})
            await client.get("/health")

    assert not [r for r in caplog.records if r.name == "app.middleware"]


async def test_the_new_id_is_the_one_in_the_log_lines():
    """El ContextVar del pedido tiene el id generado, no el recibido."""
    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(request_id_var.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "headers": [(b"x-request-id", b"hola\r\nmundo")]}
    await RequestIdMiddleware(app)(scope, receive, send)

    echoed = dict(sent[0]["headers"])[b"x-request-id"].decode()
    assert GENERATED.fullmatch(echoed)
    assert seen == [echoed]
