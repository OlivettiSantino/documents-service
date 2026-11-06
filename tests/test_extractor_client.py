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
    PayloadTooLargeError,
    UnprocessablePdfError,
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


# --- Mapeo de cada respuesta (issue #29) -----------------------------------


@respx.mock
@pytest.mark.parametrize(
    ("status_code", "error"),
    [
        (400, UnprocessablePdfError),
        (422, UnprocessablePdfError),
        (413, PayloadTooLargeError),
        (429, ExtractUnavailableError),
        (503, ExtractUnavailableError),
        (502, ExtractUnavailableError),
        (404, ExtractUnavailableError),
        (504, ExtractTimeoutError),
        (500, ExtractProtocolError),
        (501, ExtractProtocolError),
        (401, ExtractProtocolError),
        (409, ExtractProtocolError),
        (302, ExtractProtocolError),
        (201, ExtractProtocolError),
    ],
)
async def test_each_status_maps_to_its_domain_error(client, sample_pdf, status_code, error):
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(status_code, json={"detail": "no"})
    )
    with pytest.raises(error) as info:
        await client.extract(sample_pdf)
    assert type(info.value) is error
    assert route.call_count == 1  # nunca se reintenta


@respx.mock
@pytest.mark.parametrize("status_code", [400, 422])
async def test_a_pdf_extract_cannot_process_is_unprocessable_and_logs_the_detail(
    client, sample_pdf, caplog, status_code
):
    """El 400/422 de extract es culpa del archivo: 422 al cliente, no 502. El
    detail de extract va al log, no al mensaje."""
    detail = "El contenido del archivo no es un PDF válido"
    respx.post(ENDPOINT).mock(return_value=httpx.Response(status_code, json={"detail": detail}))

    with caplog.at_level(logging.WARNING), pytest.raises(UnprocessablePdfError) as info:
        await client.extract(sample_pdf)

    assert info.value.message == UnprocessablePdfError.message
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.extract_status == status_code
    assert record.extract_detail == detail


@respx.mock
async def test_a_413_from_extract_is_payload_too_large(client, sample_pdf):
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(413, json={"detail": "supera 1000 bytes"})
    )
    with pytest.raises(PayloadTooLargeError) as info:
        await client.extract(sample_pdf)
    assert info.value.message == PayloadTooLargeError.message


@respx.mock
async def test_a_429_from_the_gateway_is_unavailable_with_its_retry_after(client, sample_pdf):
    """El inFlightReq de Traefik responde 429: es saturacion, no un 502."""
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            429, text="Too Many Requests", headers={"Retry-After": "3"}
        )
    )
    with pytest.raises(ExtractUnavailableError) as info:
        await client.extract(sample_pdf)
    assert info.value.retry_after == 3.0


@respx.mock
async def test_a_429_without_retry_after_has_no_hint(client, sample_pdf):
    respx.post(ENDPOINT).mock(return_value=httpx.Response(429, text="Too Many Requests"))
    with pytest.raises(ExtractUnavailableError) as info:
        await client.extract(sample_pdf)
    assert info.value.retry_after is None


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


# --- Mensajes fijos, detalle solo en el log (issue #29) ---------------------

INTERNAL_URL = "http://gateway-interno.svc:8123"


@respx.mock
async def test_a_connection_error_does_not_leak_the_destination(sample_pdf, caplog):
    """El texto de httpx trae host, puerto e IP: al log sí, al mensaje no."""
    respx.post(f"{INTERNAL_URL}/extract").mock(
        side_effect=httpx.ConnectError(
            "[Errno 111] Connect call failed ('10.0.3.7', 8123) gateway-interno.svc"
        )
    )
    with caplog.at_level(logging.WARNING), pytest.raises(ExtractUnavailableError) as info:
        await make_client(INTERNAL_URL).extract(sample_pdf)

    message = info.value.message
    assert message == ExtractUnavailableError.message
    for internal in ("gateway-interno", "8123", "10.0.3.7", "Errno"):
        assert internal not in message
        assert internal not in str(info.value)

    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.error_type == "ConnectError"
    assert "10.0.3.7" in record.error


@respx.mock
async def test_a_timeout_logs_its_type_and_keeps_the_fixed_message(client, sample_pdf, caplog):
    respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout("read timeout on extract:8001"))
    with caplog.at_level(logging.WARNING), pytest.raises(ExtractTimeoutError) as info:
        await client.extract(sample_pdf)

    assert info.value.message == ExtractTimeoutError.message
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.error_type == "ReadTimeout"


@respx.mock
@pytest.mark.parametrize(
    "status_code", [400, 404, 413, 422, 429, 500, 501, 502, 503, 504, 418]
)
async def test_every_error_answer_keeps_its_class_message_and_logs_the_origin(
    client, sample_pdf, caplog, status_code
):
    leak = "Traceback ... File /app/extractor/services/pdf_extraction.py 10.0.3.7"
    respx.post(ENDPOINT).mock(return_value=httpx.Response(status_code, json={"detail": leak}))

    with caplog.at_level(logging.WARNING), pytest.raises(Exception) as info:
        await client.extract(sample_pdf)

    error = info.value
    assert error.message == type(error).message
    assert "10.0.3.7" not in error.message
    assert str(status_code) not in error.message
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.extract_status == status_code
    assert record.extract_detail == leak


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>oops</html>"),
        httpx.Response(200, json=["no", "es", "un", "objeto"]),
        httpx.Response(200, json={"content": "hola"}),
        httpx.Response(200, json={"content": "hola", "page_count": -1}),
    ],
)
async def test_an_unusable_200_is_a_fixed_protocol_error_with_the_reason_in_the_log(
    client, sample_pdf, caplog, response
):
    respx.post(ENDPOINT).mock(return_value=response)
    with caplog.at_level(logging.WARNING), pytest.raises(ExtractProtocolError) as info:
        await client.extract(sample_pdf)

    assert info.value.message == ExtractProtocolError.message
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.reason


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
