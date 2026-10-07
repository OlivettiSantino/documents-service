"""Circuit Breaker.

Hand-rolled rather than imported: the course wants the pattern visible in the
code and driven by tests, and the available libraries either are synchronous
(``pybreaker``) or give no way to inject a clock (``aiobreaker``), which would
force every state test to ``sleep``.

States
------
``CLOSED``     calls go through; failures are counted inside a sliding window.
``OPEN``       calls are rejected immediately, without touching extract-service.
``HALF_OPEN``  exactly one probe is allowed through to see if it recovered.
"""

from collections import deque
from enum import Enum
from typing import Awaitable, Callable, TypeVar

from app.clock import Clock
from app.exceptions import CircuitOpenError
from app.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Fail fast while a dependency is known to be broken."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        open_seconds: float,
        window_seconds: float,
        clock: Clock,
        name: str = "extract",
    ) -> None:
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._window_seconds = window_seconds
        self._clock = clock
        self._name = name

        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        """Current state, re-evaluated against the clock on every read."""
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock.monotonic() - self._opened_at >= self._open_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    @property
    def failure_count(self) -> int:
        """Failures still inside the sliding window."""
        self._drop_stale_failures()
        return len(self._failures)

    def retry_after(self) -> float:
        """Seconds left before the next probe is allowed."""
        if self._opened_at is None:
            return 0.0
        remaining = self._open_seconds - (self._clock.monotonic() - self._opened_at)
        return max(0.0, remaining)

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run ``operation`` under the breaker.

        Raises:
            CircuitOpenError: if the circuit is open, or a probe is already in
                flight while half-open. The operation is not executed.
        """
        state = self.state

        if state is BreakerState.OPEN:
            raise CircuitOpenError(retry_after=self.retry_after())

        is_probe = state is BreakerState.HALF_OPEN
        if is_probe:
            if self._probe_in_flight:
                raise CircuitOpenError(retry_after=self.retry_after())
            self._probe_in_flight = True

        try:
            result = await operation()
        except Exception:
            self._record_failure(was_probe=is_probe)
            raise
        else:
            self._record_success()
            return result
        finally:
            if is_probe:
                self._probe_in_flight = False

    def _record_success(self) -> None:
        reopened = self._opened_at is not None
        self._failures.clear()
        self._opened_at = None
        if reopened:
            logger.info("Circuito cerrado tras sonda exitosa", extra={"breaker": self._name})

    def _record_failure(self, *, was_probe: bool) -> None:
        if was_probe:
            # A failed probe sends us straight back to open, with a fresh timer.
            self._opened_at = self._clock.monotonic()
            logger.warning("Sonda fallida, circuito abierto de nuevo", extra={"breaker": self._name})
            return

        self._failures.append(self._clock.monotonic())
        self._drop_stale_failures()
        if len(self._failures) >= self._failure_threshold:
            self._opened_at = self._clock.monotonic()
            self._failures.clear()
            logger.warning(
                "Circuito abierto",
                extra={"breaker": self._name, "open_seconds": self._open_seconds},
            )

    def _drop_stale_failures(self) -> None:
        """Forget failures older than the window, so old noise can't trip us."""
        cutoff = self._clock.monotonic() - self._window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
