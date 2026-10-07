"""Bulkhead: bounded concurrency with a bounded wait."""

import asyncio

import pytest

from app.exceptions import BulkheadFullError
from app.services.bulkhead import Bulkhead


def make_bulkhead(max_concurrent: int = 2, acquire_timeout: float = 0.05) -> Bulkhead:
    return Bulkhead(max_concurrent=max_concurrent, acquire_timeout=acquire_timeout)


async def test_grants_slots_up_to_the_limit():
    bulkhead = make_bulkhead(max_concurrent=2)
    async with bulkhead.slot():
        async with bulkhead.slot():
            assert bulkhead.available == 0
            assert bulkhead.in_use == 2


async def test_releases_the_slot_on_the_way_out():
    bulkhead = make_bulkhead(max_concurrent=1)
    async with bulkhead.slot():
        pass
    assert bulkhead.available == 1


async def test_releases_the_slot_even_when_the_work_blows_up():
    bulkhead = make_bulkhead(max_concurrent=1)
    with pytest.raises(RuntimeError):
        async with bulkhead.slot():
            raise RuntimeError("falló la extracción")
    assert bulkhead.available == 1


async def test_sheds_load_instead_of_queueing_forever():
    bulkhead = make_bulkhead(max_concurrent=1, acquire_timeout=0.01)
    async with bulkhead.slot():
        with pytest.raises(BulkheadFullError):
            async with bulkhead.slot():
                pass


async def test_the_rejection_tells_the_client_when_to_come_back():
    bulkhead = make_bulkhead(max_concurrent=1, acquire_timeout=0.01)
    async with bulkhead.slot():
        with pytest.raises(BulkheadFullError) as info:
            async with bulkhead.slot():
                pass
    assert info.value.retry_after == pytest.approx(0.01)


async def test_a_waiter_gets_in_as_soon_as_a_slot_frees_up():
    bulkhead = make_bulkhead(max_concurrent=1, acquire_timeout=1.0)
    order: list[str] = []

    async def worker(name: str, hold: float) -> None:
        async with bulkhead.slot():
            order.append(f"{name}:in")
            await asyncio.sleep(hold)
            order.append(f"{name}:out")

    await asyncio.gather(worker("a", 0.01), worker("b", 0.0))
    assert order == ["a:in", "a:out", "b:in", "b:out"]


async def test_timed_out_waiters_do_not_leak_permits():
    """A cancelled ``acquire()`` used to be able to leak a permit in CPython.

    If that ever regresses the service would starve slowly and silently, so we
    assert the pool is intact rather than trusting the interpreter.
    """
    bulkhead = make_bulkhead(max_concurrent=2, acquire_timeout=0.01)
    async with bulkhead.slot():
        async with bulkhead.slot():
            for _ in range(5):
                with pytest.raises(BulkheadFullError):
                    async with bulkhead.slot():
                        pass
    assert bulkhead.available == bulkhead.max_concurrent == 2
