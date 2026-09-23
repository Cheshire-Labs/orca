"""EventChannel and EventChannelRegistry for cross-thread communication.

Counter-based latch pub/sub. Each channel stores the latest published
value and a monotonic counter. Waiters provide their last-seen counter
and block until the channel counter exceeds it. This prevents missed
publishes when emit() fires before on() starts waiting.

Used by V5.1 code-first execution model for:
- ctx.emit("event_name", value, data) -- publish from within a method
- orca.on("event_name") -- WaitStep in thread method list
- orca.branch("event_name", {val: methods}) -- BranchStep routing
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import JsonValue


@dataclass(frozen=True)
class PendingManualStep:
    """An emitted-but-unconfirmed operator manual step awaiting confirmation."""

    step_id: str
    instruction: str
    emitted_at: datetime


class EventChannel:
    """Single named pub/sub channel with counter-based latch."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._counter: int = 0
        self._latest_value: str | None = None
        self._latest_data: dict[str, JsonValue] = {}
        self._condition = asyncio.Condition()
        self._waiter_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def counter(self) -> int:
        return self._counter

    @property
    def waiter_count(self) -> int:
        """Coroutines currently parked in wait(); lets a producer await that a
        consumer has actually blocked (observability)."""
        return self._waiter_count

    async def publish(self, value: str | None = None, data: dict[str, JsonValue] | None = None) -> None:
        """Publish a value to this channel. Wakes all waiters."""
        async with self._condition:
            self._counter += 1
            self._latest_value = value
            self._latest_data = data if data is not None else {}
            self._condition.notify_all()

    async def wait(
        self,
        seen_counter: int = 0,
        timeout: float | None = None,
    ) -> tuple[int, str | None, dict[str, JsonValue]]:
        """Wait until a publish occurs after seen_counter.

        Returns (counter, value, data). Raises asyncio.TimeoutError
        if timeout expires before a publish.
        """
        async with self._condition:
            while self._counter <= seen_counter:
                self._waiter_count += 1
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                finally:
                    self._waiter_count -= 1
            return self._counter, self._latest_value, self._latest_data


class EventChannelRegistry:
    """Per-execution registry of named EventChannels."""

    def __init__(self) -> None:
        self._channels: dict[str, EventChannel] = {}
        self._pending_manual_steps: dict[str, PendingManualStep] = {}

    def get_or_create(self, name: str) -> EventChannel:
        if name not in self._channels:
            self._channels[name] = EventChannel(name)
        return self._channels[name]

    def record_manual_step(self, step_id: str, instruction: str) -> None:
        """Track a manual step as pending until it is confirmed or cleared."""
        self._pending_manual_steps[step_id] = PendingManualStep(
            step_id=step_id,
            instruction=instruction,
            emitted_at=datetime.now(timezone.utc),
        )

    def clear_manual_step(self, step_id: str) -> None:
        """Drop a manual step from the pending set. Idempotent."""
        self._pending_manual_steps.pop(step_id, None)

    def pop_manual_step(self, step_id: str) -> PendingManualStep | None:
        """Atomically remove and return a pending step, or None if absent.

        The synchronous claim is what closes the double-confirm window: the
        first caller takes the entry, a concurrent second caller sees None.
        """
        return self._pending_manual_steps.pop(step_id, None)

    def list_manual_steps(self) -> list[PendingManualStep]:
        """Pending manual steps sorted by emission time (oldest first)."""
        return sorted(
            self._pending_manual_steps.values(), key=lambda s: s.emitted_at,
        )

    def has_manual_step(self, step_id: str) -> bool:
        return step_id in self._pending_manual_steps
