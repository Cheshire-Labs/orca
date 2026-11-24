from tests.mock import EXTERNAL_MOVER
"""ManualRemoveSpawn unit tests.

`ManualRemoveSpawn` is the end-side strategy for both bare-string
`end="loc"` and explicit `end=("loc", MANUAL_REMOVE)`. Behavior
branches on `thread.run_mode`:

- `CHECK_RUN` / `SIM`: return at once, with no operator wait. The
  thread then calls `LabwarePlacer.left`, which frees the slot.
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
from orca.resource_models.labware_location_service import InMemoryLabwareLocationService
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.spawn_actions import ManualRemoveSpawn
from tests.placements_for_tests import this_tests_placements
from tests.test_helpers import make_labware_placer


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


async def _end_the_thread(spawn: ManualRemoveSpawn, thread: LabwareThreadInstance, at: Location) -> None:
    """What `_handle_thread_completion` does at the end location: dispose, then the placer's `left`."""
    await asyncio.wait_for(spawn.dispose(thread), timeout=2.0)
    await make_labware_placer(InMemoryLabwareLocationService(this_tests_placements())).left(thread.labware, at)


class TestManualRemoveSpawnSimModes:
    """Sim modes free the slot with no operator wait."""

    async def test_check_run_frees_the_slot_without_an_operator_wait(self) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()
        location.initialize_labware(labware)
        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.CHECK_RUN)

        await _end_the_thread(ManualRemoveSpawn(location), thread, location)

        assert location.labware is None

    async def test_sim_frees_the_slot_without_an_operator_wait(self) -> None:
        location = _make_platepad_location()
        labware = _fresh_labware()
        location.initialize_labware(labware)
        thread = _make_thread(labware, location, run_mode=WorkflowRunMode.SIM)

        await _end_the_thread(ManualRemoveSpawn(location), thread, location)

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
