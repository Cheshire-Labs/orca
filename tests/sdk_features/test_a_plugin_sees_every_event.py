"""`OrcaPlugin`: `handle_runtime_event` is called for each runtime event."""

import pytest

from orca.events.runtime_event import RuntimeEvent
from orca.plugins.base import OrcaPlugin
from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import build_world, close_world, events, new_lab, run_to_end

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


class _Recorder(OrcaPlugin):
    def __init__(self) -> None:
        self.seen: list[RuntimeEvent] = []

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        self.seen.append(event)


async def test_the_plugin_hears_every_event() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, "pad_1", "pad_2"))
    plugin = _Recorder()
    world.runtime.register_plugin(plugin)
    try:
        execution_id, ended = await run_to_end(world)
        assert set(ended.values()) == {"COMPLETED"}, ended

        in_the_run = [e for e in events(world) if e.execution_id == execution_id]
        assert in_the_run and [e for e in plugin.seen if e.execution_id == execution_id] == in_the_run
        assert any(e.event_name.startswith("THREAD.") for e in in_the_run)
    finally:
        await close_world(world)
