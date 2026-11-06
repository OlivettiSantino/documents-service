"""Injectable clock.

The repository stamps ``created_at`` and ``updated_at``. Reading the real clock
directly would make those fields impossible to assert, so time is a
collaborator: production uses ``SystemClock`` and tests use a fixed fake.
"""

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """What the service needs to know about time."""

    def now(self) -> datetime:
        """Current UTC timestamp, used for persisted fields."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)
