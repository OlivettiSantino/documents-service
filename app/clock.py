"""Injectable clock.

The circuit breaker measures elapsed time. Reading the real clock directly
would force its tests to ``sleep``, so time is a collaborator: production uses
``SystemClock`` and tests use a fake that advances a counter by hand.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """Everything the resilience layers need to know about time."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, immune to wall-clock jumps."""
        ...

    def now(self) -> datetime:
        """Current UTC timestamp, used for persisted fields."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the current task."""
        ...


class SystemClock:
    """The real clock."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
