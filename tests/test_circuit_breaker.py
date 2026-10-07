"""Circuit breaker state machine.

Every case runs against the injected ``FakeClock``: the whole file finishes in
milliseconds and never sleeps, which is exactly why the breaker is hand-rolled
instead of imported.
"""

import pytest

from app.exceptions import CircuitOpenError
from app.services.circuit_breaker import BreakerState, CircuitBreaker
from tests.conftest import FakeClock


class Boom(Exception):
    """A counted upstream failure."""


def make_breaker(clock: FakeClock, *, threshold: int = 3, open_seconds: float = 10.0,
                 window_seconds: float = 30.0) -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=threshold,
        open_seconds=open_seconds,
        window_seconds=window_seconds,
        clock=clock,
    )


async def ok():
    return "listo"


async def boom():
    raise Boom()


async def test_starts_closed(clock):
    assert make_breaker(clock).state is BreakerState.CLOSED


async def test_closed_breaker_returns_the_operation_result(clock):
    assert await make_breaker(clock).call(ok) == "listo"


async def test_failures_propagate_while_closed(clock):
    breaker = make_breaker(clock)
    with pytest.raises(Boom):
        await breaker.call(boom)
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failure_count == 1


async def test_opens_once_the_threshold_is_reached(clock):
    breaker = make_breaker(clock, threshold=3)
    for _ in range(3):
        with pytest.raises(Boom):
            await breaker.call(boom)
    assert breaker.state is BreakerState.OPEN


async def test_open_breaker_rejects_without_running_the_operation(clock):
    breaker = make_breaker(clock, threshold=1)
    with pytest.raises(Boom):
        await breaker.call(boom)

    calls = 0

    async def counted():
        nonlocal calls
        calls += 1
        return "no debería ejecutarse"

    with pytest.raises(CircuitOpenError):
        await breaker.call(counted)
    assert calls == 0


async def test_failures_older_than_the_window_do_not_count(clock):
    """Three failures spread over an hour are not an outage."""
    breaker = make_breaker(clock, threshold=3, window_seconds=30.0)
    for _ in range(2):
        with pytest.raises(Boom):
            await breaker.call(boom)
        clock.advance(20.0)

    with pytest.raises(Boom):
        await breaker.call(boom)
    assert breaker.state is BreakerState.CLOSED


async def test_a_success_clears_the_failure_window(clock):
    breaker = make_breaker(clock, threshold=3)
    for _ in range(2):
        with pytest.raises(Boom):
            await breaker.call(boom)
    await breaker.call(ok)
    assert breaker.failure_count == 0

    with pytest.raises(Boom):
        await breaker.call(boom)
    assert breaker.state is BreakerState.CLOSED


async def test_open_becomes_half_open_once_the_timer_expires(clock):
    breaker = make_breaker(clock, threshold=1, open_seconds=10.0)
    with pytest.raises(Boom):
        await breaker.call(boom)
    assert breaker.state is BreakerState.OPEN

    clock.advance(9.9)
    assert breaker.state is BreakerState.OPEN

    clock.advance(0.1)
    assert breaker.state is BreakerState.HALF_OPEN


async def test_half_open_lets_a_probe_through_and_closes_on_success(clock):
    breaker = make_breaker(clock, threshold=1, open_seconds=10.0)
    with pytest.raises(Boom):
        await breaker.call(boom)
    clock.advance(10.0)

    assert await breaker.call(ok) == "listo"
    assert breaker.state is BreakerState.CLOSED


async def test_a_failed_probe_reopens_the_circuit_with_a_fresh_timer(clock):
    breaker = make_breaker(clock, threshold=1, open_seconds=10.0)
    with pytest.raises(Boom):
        await breaker.call(boom)
    clock.advance(10.0)

    with pytest.raises(Boom):
        await breaker.call(boom)
    assert breaker.state is BreakerState.OPEN
    assert breaker.retry_after() == pytest.approx(10.0)


async def test_half_open_admits_only_one_probe_at_a_time(clock):
    """A flood of requests must not all be sent at a dependency that may still
    be down."""
    import asyncio

    breaker = make_breaker(clock, threshold=1, open_seconds=10.0)
    with pytest.raises(Boom):
        await breaker.call(boom)
    clock.advance(10.0)

    gate = asyncio.Event()
    started = 0

    async def slow_probe():
        nonlocal started
        started += 1
        await gate.wait()
        return "listo"

    first = asyncio.create_task(breaker.call(slow_probe))
    await asyncio.sleep(0)

    with pytest.raises(CircuitOpenError):
        await breaker.call(slow_probe)

    gate.set()
    assert await first == "listo"
    assert started == 1


async def test_retry_after_counts_down(clock):
    breaker = make_breaker(clock, threshold=1, open_seconds=10.0)
    with pytest.raises(Boom):
        await breaker.call(boom)

    assert breaker.retry_after() == pytest.approx(10.0)
    clock.advance(4.0)
    assert breaker.retry_after() == pytest.approx(6.0)
    clock.advance(100.0)
    assert breaker.retry_after() == 0.0


async def test_retry_after_is_zero_while_closed(clock):
    assert make_breaker(clock).retry_after() == 0.0


async def test_circuit_open_error_carries_the_remaining_wait(clock):
    breaker = make_breaker(clock, threshold=1, open_seconds=10.0)
    with pytest.raises(Boom):
        await breaker.call(boom)
    clock.advance(3.0)

    with pytest.raises(CircuitOpenError) as info:
        await breaker.call(ok)
    assert info.value.retry_after == pytest.approx(7.0)


async def test_recovers_fully_after_an_outage(clock):
    """Open, wait, probe, close, and keep serving."""
    breaker = make_breaker(clock, threshold=2, open_seconds=10.0)
    for _ in range(2):
        with pytest.raises(Boom):
            await breaker.call(boom)
    assert breaker.state is BreakerState.OPEN

    clock.advance(10.0)
    await breaker.call(ok)

    assert breaker.state is BreakerState.CLOSED
    assert await breaker.call(ok) == "listo"
