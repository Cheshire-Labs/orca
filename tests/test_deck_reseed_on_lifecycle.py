"""A liquid handler's deck re-seeds from the world model after lifecycle verbs.

The gap this pins (session-rebuild reconcile handoff): initialize / connect can
rebuild the driver's session, which silently empties its deck while the world
model still knows every occupant. Orca's memory of "this world is configured"
went stale with it, so nothing re-declared the labware and the next command
failed with resource-not-found. After a lifecycle verb runs, the facade invalidates
the deck world and re-runs the occupancy reconcile (a state push, no motion), and an operator
can force the same pass with ``devices.reconcile_deck`` when the rebuild
happened outside orca (robot touchscreen, driver-internal recovery).
"""

import pytest

from cheshire_drivers import RecordingLiquidHandlerDriver
from orca.resource_models.location import Location
from tests.test_labware_state_reconciliation import RESERVOIR_SITE, _build
from tests.memory_activity import memory_runtime
from tests.recording_handler import RecordingHandler


def _count(recorder: RecordingLiquidHandlerDriver, method: str) -> int:
    return sum(1 for call in recorder.calls if call.method == method)


async def _runtime_with_resident():
    recorder = RecordingHandler()
    build, lh = await _build(recorder, wf_name="deck_reseed")
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    await runtime.labware.register(
        "reservoir", location=RESERVOIR_SITE, confirm=True,
    )
    return build, lh, runtime, recorder


async def test_initialize_re_seeds_the_deck() -> None:
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        configures = _count(recorder, "configure_deck")
        reconciles = _count(recorder, "reconcile_deck_occupancy")

        await runtime.devices.after_lifecycle(lh.name, "initialize")

        assert _count(recorder, "configure_deck") == configures + 1
        assert _count(recorder, "reconcile_deck_occupancy") == reconciles + 1
    finally:
        await runtime.shutdown()


async def test_reconcile_deck_of_a_marked_world_pushes_once_and_leaves_no_rebuild_owed() -> None:
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        worlds = runtime.system.device_worlds
        worlds.mark_every_world(lh.name)
        reconciles = _count(recorder, "reconcile_deck_occupancy")

        await runtime.devices.reconcile_deck(lh.name, confirm=True)

        assert _count(recorder, "reconcile_deck_occupancy") == reconciles + 1
        assert not await worlds.owes_a_rebuild(lh), "the next command would push the same deck again"
    finally:
        await runtime.shutdown()


async def test_a_mark_set_while_reconcile_deck_pushes_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        worlds = runtime.system.device_worlds
        placer = runtime.system.labware_placer
        push = placer.deck_rebuilt

        async def push_then_the_record_changes(location: Location) -> None:
            await push(location)
            worlds.mark_every_world(lh.name)

        monkeypatch.setattr(placer, "deck_rebuilt", push_then_the_record_changes)

        await runtime.devices.reconcile_deck(lh.name, confirm=True)

        assert await worlds.owes_a_rebuild(lh), "the push read the record before it changed"
    finally:
        await runtime.shutdown()


async def test_a_reconcile_deck_push_that_fails_leaves_the_world_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        worlds = runtime.system.device_worlds
        await worlds.bring_up_to_date(lh.name)
        assert not await worlds.owes_a_rebuild(lh)

        async def refused(location: Location) -> None:
            raise RuntimeError("the driver refused the deck")

        monkeypatch.setattr(runtime.system.labware_placer, "deck_rebuilt", refused)

        with pytest.raises(RuntimeError, match="refused the deck"):
            await runtime.devices.reconcile_deck(lh.name, confirm=True)

        assert await worlds.owes_a_rebuild(lh)
    finally:
        await runtime.shutdown()


async def test_connect_re_seeds_the_deck() -> None:
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        configures = _count(recorder, "configure_deck")

        await runtime.devices.after_lifecycle(lh.name, "connect")

        assert _count(recorder, "configure_deck") == configures + 1
    finally:
        await runtime.shutdown()


async def test_reconcile_deck_verb_re_seeds_on_demand() -> None:
    """The operator's answer to a rebuild orca never saw (touchscreen
    cancel_run + initialize): force the pass, then RETRY the paused thread."""
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        configures = _count(recorder, "configure_deck")
        reconciles = _count(recorder, "reconcile_deck_occupancy")

        await runtime.devices.reconcile_deck(lh.name, confirm=True)

        assert _count(recorder, "configure_deck") == configures + 1
        assert _count(recorder, "reconcile_deck_occupancy") == reconciles + 1
    finally:
        await runtime.shutdown()


async def test_reconcile_deck_projects_the_resident() -> None:
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        await runtime.devices.reconcile_deck(lh.name, confirm=True)
        last = [c for c in recorder.calls if c.method == "reconcile_deck_occupancy"][-1]
        projected = [r["name"] for r in last.args["resources"]]
        assert any("reservoir" in name for name in projected)
    finally:
        await runtime.shutdown()


async def test_disconnect_invalidates_without_reconnecting(
) -> None:
    """Disconnect must not push state at a device it just tore down; the
    invalidation shows up as a fresh configure on the NEXT reconcile."""
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        await runtime.devices.after_lifecycle(lh.name, "disconnect")
        configures = _count(recorder, "configure_deck")

        lh_location = build.system.system_map.get_resource_location(lh.name)
        await build.system.reconcile_lh_deck_occupancy(lh_location)

        assert _count(recorder, "configure_deck") == configures + 1
    finally:
        await runtime.shutdown()


async def test_reconcile_deck_refuses_a_non_lh_device() -> None:
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        with pytest.raises(ValueError, match="liquid handler"):
            await runtime.devices.reconcile_deck("stacker", confirm=True)
    finally:
        await runtime.shutdown()
