from tests.mock import EXTERNAL_MOVER
"""ManualRemoveSpawn unit tests.

`ManualRemoveSpawn` is the end-side strategy for both bare-string
`end="loc"` and explicit `end=("loc", MANUAL_REMOVE)`. Behavior
branches on `thread.run_mode`:

- `PURE_SIM` / `DEVICE_SIM`: auto-dispose -- call
  `end_location.dispose_labware(labware)` exactly as today's terminal
  branch of `_handle_thread_completion` does. No operator wait.
- `LIVE`: poll `end_location.labware` every 0.5s. When the operator
  calls `labware_discharge(labware_id)`, the slot clears (discharge
  already walks slot + registry), the wait returns, and
  `_handle_thread_completion` falls through to `status = COMPLETED`.

These tests exercise the strategy directly. The engine's
`AWAITING_MANUAL_REMOVE` status emission belongs to the engine
(`_handle_thread_completion` three-way dispatch).
"""

import asyncio

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.spawn_actions import ManualRemoveSpawn


def _make_platepad_location(name: str = "pad1") -> Location:
    pad = PlatePad(name)
    return Location(name, resource=pad)


def _fresh_labware(template_name: str = "plate_96") -> LabwareInstance:
    return LabwareInstance(template_name, "96_well")


def _make_thread(
    labware: LabwareInstance,
    end_location: Location,
    *,
    run_mode: WorkflowRunMode,
) -> LabwareThreadInstance:
    return LabwareThreadInstance(
        labware=labware,
        start_location=end_location,
        end_locations=[end_location],
        run_mode=run_mode,
    )


class TestManualRemoveSpawnSimModes:
    """Sim modes auto-dispose via the same `dispose_labware` call the
    legacy `_handle_thread_completion` branch ran. No operator wait."""

    async def test_pure_sim_calls_dispose_labware(self) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()
        location.initialize_labware(labware)
        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.PURE_SIM)

        spawn = ManualRemoveSpawn(location)
        await spawn.dispose(thread)

        assert location.labware is None

    async def test_device_sim_calls_dispose_labware(self) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()
        location.initialize_labware(labware)
        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.DEVICE_SIM)

        spawn = ManualRemoveSpawn(location)
        await spawn.dispose(thread)

        assert location.labware is None


class TestManualRemoveSpawnLive:
    """LIVE mode polls `end_location.labware`, waiting for
    `labware_discharge` to clear the slot. Returns when the slot is
    cleared so the caller (`_handle_thread_completion`) can
    transition to `COMPLETED`."""

    async def test_live_waits_for_operator_discharge(self) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()
        location.initialize_labware(labware)
        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.LIVE)

        spawn = ManualRemoveSpawn(location)
        dispose_task = asyncio.create_task(spawn.dispose(thread))
        await asyncio.sleep(0)
        assert not dispose_task.done()
        assert location.labware is labware

        # Simulate operator discharge clearing the slot.
        await location.notify_picked(labware, EXTERNAL_MOVER)

        await dispose_task
        assert location.labware is None

    async def test_live_returns_immediately_if_slot_already_clear(self) -> None:
        """If the operator discharged before `_handle_thread_completion`
        called `dispose`, the polling loop short-circuits. Defensive
        case; in practice the engine sets status before calling dispose
        so the typical sequence is occupied -> discharge -> clear."""
        location = _make_platepad_location()
        labware = _fresh_labware()
        # Slot starts clear (discharged before dispose was called).
        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.LIVE)

        spawn = ManualRemoveSpawn(location)
        await asyncio.wait_for(spawn.dispose(thread), timeout=2.0)

        assert location.labware is None
