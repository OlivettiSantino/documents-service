"""The extract-service client and how the resilience layers compose."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.exceptions import (
    BulkheadFullError,
    CircuitOpenError,
    ExtractProtocolError,
    ExtractTimeoutError,
    ExtractUnavailableError,
)
from app.services.bulkhead import Bulkhead
from app.services.circuit_breaker import BreakerState, CircuitBreaker
from app.services.extractor_client import ExtractorClient, parse_retry_after

BASE_URL = "http://extract:8001"
ENDPOINT = f"{BASE_URL}/extract"


def make_client(clock, *, threshold: int = 3, max_concurrent: int = 2,
                acquire_timeout: float = 0.05) -> ExtractorClient:
    return ExtractorClient(
        http_client=httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=1.0)),
        base_url=BASE_URL,
        breaker=CircuitBreaker(
            failure_threshold=threshold, open_seconds=10.0, window_seconds=30.0, clock=clock
        ),
        bulkhead=Bulkhead(max_concurrent=max_concurrent, acquire_timeout=acquire_timeout),
        clock=clock,
        max_retry_wait=1.0,
    )


@pytest.fixture
def client(clock):
    return make_client(clock)


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
async def test_propagates_the_request_id(client, sample_pdf):
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"content": "", "page_count": 0})
    )
    await client.extract(sample_pdf, request_id="abc123")
    assert route.calls.last.request.headers["X-Request-ID"] == "abc123"


@respx.mock
@pytest.mark.parametrize("status_code", [400, 413, 404, 500])
async def test_unexpected_status_is_a_protocol_error(client, sample_pdf, status_code):
    respx.post(ENDPOINT).mock(return_value=httpx.Response(status_code, json={"detail": "no"}))
    with pytest.raises(ExtractProtocolError):
        await client.extract(sample_pdf)


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
async def test_a_timeout_is_never_retried(client, sample_pdf):
    """Resending a multi-megabyte PDF would double the load on a service that is
    already saturated -- the exact bottleneck the TP asks us not to worsen."""
    route = respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout("tardo demasiado"))
    with pytest.raises(ExtractTimeoutError):
        await client.extract(sample_pdf)
    assert route.call_count == 1


@respx.mock
async def test_a_connection_error_means_unavailable(client, sample_pdf):
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("conexion rechazada"))
    with pytest.raises(ExtractUnavailableError):
        await client.extract(sample_pdf)


@respx.mock
async def test_a_503_is_retried_exactly_once(client, sample_pdf, clock):
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"content": "al segundo intento", "page_count": 1}),
        ]
    )
    result = await client.extract(sample_pdf)

    assert result.content == "al segundo intento"
    assert route.call_count == 2
    assert clock.slept == [1.0]


@respx.mock
async def test_two_consecutive_503s_give_up(client, sample_pdf):
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(503, headers={"Retry-After": "1"})
    )
    with pytest.raises(ExtractUnavailableError):
        await client.extract(sample_pdf)
    assert route.call_count == 2


@respx.mock
async def test_a_long_retry_after_is_handed_to_the_caller_instead_of_waited_out(
    client, sample_pdf, clock
):
    respx.post(ENDPOINT).mock(return_value=httpx.Response(503, headers={"Retry-After": "120"}))
    with pytest.raises(ExtractUnavailableError) as info:
        await client.extract(sample_pdf)

    assert info.value.retry_after == 120.0
    assert clock.slept == []


@respx.mock
async def test_a_503_without_retry_after_is_not_retried(client, sample_pdf):
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(503))
    with pytest.raises(ExtractUnavailableError):
        await client.extract(sample_pdf)
    assert route.call_count == 1


@respx.mock
async def test_the_breaker_opens_after_repeated_failures(client, sample_pdf):
    route = respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("caido"))

    for _ in range(3):
        with pytest.raises(ExtractUnavailableError):
            await client.extract(sample_pdf)
    assert client.breaker_state == BreakerState.OPEN.value

    with pytest.raises(CircuitOpenError):
        await client.extract(sample_pdf)
    assert route.call_count == 3  # the open circuit never reached the network


@respx.mock
async def test_an_open_circuit_does_not_consume_a_bulkhead_slot(client, sample_pdf):
    """This is why the breaker wraps the bulkhead and not the other way round."""
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("caido"))
    for _ in range(3):
        with pytest.raises(ExtractUnavailableError):
            await client.extract(sample_pdf)

    with pytest.raises(CircuitOpenError):
        await client.extract(sample_pdf)
    assert client._bulkhead.available == client._bulkhead.max_concurrent


@respx.mock
async def test_the_bulkhead_sheds_load_when_extract_is_slow(sample_pdf, clock):
    """Reads and health keep working because writes are capped and shed fast."""
    client = make_client(clock, threshold=10, max_concurrent=1, acquire_timeout=0.01)

    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"content": "lento", "page_count": 1})

    respx.post(ENDPOINT).mock(side_effect=slow)

    first = asyncio.create_task(client.extract(sample_pdf))
    await asyncio.sleep(0.02)

    with pytest.raises(BulkheadFullError):
        await client.extract(sample_pdf)

    assert (await first).content == "lento"


class TestParseRetryAfter:
    """Retry-After is legal in two forms (RFC 9110)."""

    def test_delta_seconds(self):
        assert parse_retry_after("30") == 30.0

    def test_http_date(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        header = (now + timedelta(seconds=45)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(header, now=now) == pytest.approx(45.0, abs=1)

    def test_a_date_in_the_past_means_no_wait(self):
        assert parse_retry_after("Sat, 01 Jan 2000 00:00:00 GMT") == 0.0

    @pytest.mark.parametrize("value", [None, "", "manana", "-"])
    def test_absent_or_unparseable(self, value):
        assert parse_retry_after(value) is None
