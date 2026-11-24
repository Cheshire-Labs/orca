"""After a `ctx.manual_step` timeout, RETRY waits for the confirm again without re-running the action."""

from collections.abc import Awaitable, Callable

import pytest
from pydantic import JsonValue

from orca.events.event_channel import EventChannel
from orca.workflow_models.status_enums import RecoveryDecision
from tests.sdk_features.kit import (
    build_world,
    close_world,
    new_lab,
    recover,
    speeds,
    submit,
    wait_for_a_pause,
    wait_for_the_end,
)
from tests.sdk_features.test_a_manual_step_holds_the_action_until_confirmed import (
    _pending,
    _two_manual_steps,
)
from tests.test_helpers import wait_until

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

Wait = Callable[[EventChannel, int, float | None], Awaitable[tuple[int, str | None, dict[str, JsonValue]]]]
LONGER_THAN_THE_TEST = 600.0


class _ConfirmWaits:
    """Every wait for a manual step's confirm. The first keeps its short timeout; a wait after RETRY gets one load cannot beat."""

    def __init__(self, wait: Wait) -> None:
        self._wait = wait
        self.calls: dict[str, int] = {}
        self.channels: dict[str, EventChannel] = {}

    async def __call__(
        self, channel: EventChannel, seen_counter: int = 0, timeout: float | None = None,
    ) -> tuple[int, str | None, dict[str, JsonValue]]:
        if not channel.name.startswith("OPERATOR.CONFIRM."):
            return await self._wait(channel, seen_counter, timeout)
        self.calls[channel.name] = self.calls.get(channel.name, 0) + 1
        self.channels[channel.name] = channel
        if self.calls[channel.name] > 1 and timeout is not None:
            timeout = LONGER_THAN_THE_TEST
        return await self._wait(channel, seen_counter, timeout)

    def waiting_again(self, step_id: str) -> bool:
        name = f"OPERATOR.CONFIRM.{step_id}"
        return self.calls.get(name, 0) == 2 and self.channels[name].waiter_count == 1


async def test_a_timeout_pauses_and_retry_waits_for_the_confirm_again(monkeypatch: pytest.MonkeyPatch) -> None:
    waits = _ConfirmWaits(EventChannel.wait)

    async def wait(
        channel: EventChannel, seen_counter: int = 0, timeout: float | None = None,
    ) -> tuple[int, str | None, dict[str, JsonValue]]:
        return await waits(channel, seen_counter, timeout)

    monkeypatch.setattr(EventChannel, "wait", wait)
    lab = new_lab()
    world = await build_world(lab, _two_manual_steps(lab, timeout_hours=0.5 / 3600))
    try:
        execution_id = await submit(world)
        paused = await wait_for_a_pause(world, execution_id, template="first")
        assert paused.pause_reason == "error"
        assert paused.pause_site == "ACTION_WAIT"
        pending = await _pending(world, execution_id, 2)

        await recover(world, execution_id, paused.id, RecoveryDecision.RETRY)
        await wait_until(lambda: waits.waiting_again(pending["step A"].step_id), timeout=30)
        assert speeds(world, "shaker_1") == [100.0], "RETRY waits for the confirm again; it does not re-run the shake before it"

        await world.runtime.confirm_manual_step(execution_id, pending["step A"].step_id)
        await world.runtime.confirm_manual_step(execution_id, pending["step B"].step_id)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        assert speeds(world, "shaker_1") == [100.0, 200.0]
    finally:
        await close_world(world)
