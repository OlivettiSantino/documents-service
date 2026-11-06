"""The extract-service client: one POST and the mapping of its answers.

Retry, circuit breaker and concurrency limit are Traefik's (issue #28), so the
tests here check the opposite: that the client never repeats a call.
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.exceptions import (
    ExtractProtocolError,
    ExtractTimeoutError,
    ExtractUnavailableError,
)
from app.services.extractor_client import ExtractorClient, parse_retry_after

BASE_URL = "http://extract:8001"
ENDPOINT = f"{BASE_URL}/extract"


def make_client(base_url: str = BASE_URL) -> ExtractorClient:
    return ExtractorClient(
        http_client=httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=1.0)),
        base_url=base_url,
    )


@pytest.fixture
def client():
    return make_client()


@respx.mock
async def test_happy_path(client, sample_pdf):
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"content": "hola", "page_count": 7})
    )
    result = await client.extract(sample_pdf)
    assert (result.content, result.page_count) == ("hola", 7)


@respx.mock
async def test_sends_the_pdf_as_a_binary_body(client, sample_pdf):
    """The binary path is the one the cathedra client uses."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"content": "", "page_count": 0})
    )
    await client.extract(sample_pdf)

    request = route.calls.last.request
    assert request.headers["content-type"] == "application/pdf"
    assert request.content == sample_pdf


@respx.mock
async def test_a_trailing_slash_in_the_base_url_is_ignored(sample_pdf):
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"content": "", "page_count": 0})
    )
    await make_client(f"{BASE_URL}/").extract(sample_pdf)
    assert route.call_count == 1


@respx.mock
async def test_propagates_the_request_id(client, sample_pdf):
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"content": "", "page_count": 0})
    )
    await client.extract(sample_pdf, request_id="abc123")
    assert route.calls.last.request.headers["X-Request-ID"] == "abc123"


@respx.mock
@pytest.mark.parametrize("status_code", [400, 413, 500])
async def test_unexpected_status_is_a_protocol_error(client, sample_pdf, status_code):
    respx.post(ENDPOINT).mock(return_value=httpx.Response(status_code, json={"detail": "no"}))
    with pytest.raises(ExtractProtocolError):
        await client.extract(sample_pdf)


# --- Respuestas propias del gateway (issue #27) ----------------------------


@respx.mock
async def test_a_gateway_504_is_a_timeout(client, sample_pdf):
    """Traefik corto por responseHeaderTimeout: es un timeout, no se reintenta."""
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(504, text="Gateway Timeout"))
    with pytest.raises(ExtractTimeoutError):
        await client.extract(sample_pdf)
    assert route.call_count == 1


@respx.mock
async def test_a_gateway_502_means_unavailable(client, sample_pdf):
    """Traefik no pudo hablar con la replica."""
    respx.post(ENDPOINT).mock(return_value=httpx.Response(502, text="Bad Gateway"))
    with pytest.raises(ExtractUnavailableError):
        await client.extract(sample_pdf)


@respx.mock
async def test_a_gateway_404_means_unavailable_and_warns(client, sample_pdf, caplog):
    """Traefik no tiene ruta: extract no esta registrado en Consul."""
    respx.post(ENDPOINT).mock(return_value=httpx.Response(404, text="404 page not found"))
    with caplog.at_level(logging.WARNING), pytest.raises(ExtractUnavailableError):
        await client.extract(sample_pdf)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no esta registrado" in r.getMessage() for r in warnings)


@pytest.mark.parametrize(
    ("status_code", "error"),
    [(504, ExtractTimeoutError), (502, ExtractUnavailableError), (404, ExtractUnavailableError)],
)
def test_parse_maps_the_gateway_responses(status_code, error):
    """El mapeo vive en _parse, independiente del transporte."""
    with pytest.raises(error):
        make_client()._parse(httpx.Response(status_code))


@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        {"content": "hola"},
        {"page_count": 3},
        {"content": 5, "page_count": 3},
        {"content": "hola", "page_count": "3"},
        {"content": "hola", "page_count": -1},
        ["no", "es", "un", "objeto"],
    ],
)
async def test_a_malformed_body_is_a_protocol_error(client, sample_pdf, payload):
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(ExtractProtocolError):
        await client.extract(sample_pdf)


@respx.mock
async def test_non_json_body_is_a_protocol_error(client, sample_pdf):
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ExtractProtocolError):
        await client.extract(sample_pdf)


@respx.mock
async def test_a_timeout_is_a_timeout_error_and_is_not_retried(client, sample_pdf):
    """Resending a multi-megabyte PDF would double the load on a service that is
    already saturated."""
    route = respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout("tardo demasiado"))
    with pytest.raises(ExtractTimeoutError):
        await client.extract(sample_pdf)
    assert route.call_count == 1


@respx.mock
async def test_a_connection_error_means_unavailable(client, sample_pdf):
    route = respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("conexion rechazada"))
    with pytest.raises(ExtractUnavailableError):
        await client.extract(sample_pdf)
    assert route.call_count == 1


@respx.mock
async def test_a_503_carries_the_retry_after_and_is_not_retried(client, sample_pdf):
    """The hint is handed to our caller, who turns it into our own Retry-After."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(503, headers={"Retry-After": "1"})
    )
    with pytest.raises(ExtractUnavailableError) as info:
        await client.extract(sample_pdf)

    assert info.value.retry_after == 1.0
    assert route.call_count == 1


@respx.mock
async def test_a_503_without_retry_after_has_no_hint(client, sample_pdf):
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(503))
    with pytest.raises(ExtractUnavailableError) as info:
        await client.extract(sample_pdf)

    assert info.value.retry_after is None
    assert route.call_count == 1


class TestParseRetryAfter:
    """Retry-After is legal in two forms (RFC 9110)."""

    def test_delta_seconds(self):
        assert parse_retry_after("30") == 30.0

    def test_http_date(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        header = (now + timedelta(seconds=45)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(header, now=now) == pytest.approx(45.0, abs=1)

    def test_a_date_in_the_past_means_no_wait(self):
        assert parse_retry_after("Sat, 01 Jan 2000 00:00:00 GMT") == 0.0

    @pytest.mark.parametrize("value", [None, "", "manana", "-"])
    def test_absent_or_unparseable(self, value):
        assert parse_retry_after(value) is None
