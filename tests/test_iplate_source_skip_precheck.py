"""The pre-submit start_location check refuses
submissions whose start_location is an `IPlateSource` (stacker, etc.)
because `loc.labware` reports the plate currently at the source's output
position. A stacker holds many plates physically -- the output slot
being occupied does NOT mean "blocked," it means "next plate is at the
output, transporter can pick it." The pre-check's PlatePad-shaped
"occupied = blocked" semantic is wrong for plate sources.

This test pins the contract: when the start_location's resource is an
`IPlateSource`, the pre-check must NOT refuse on `loc.labware is not None`.

The narrow fix introduces `IPlateSource` as a marker interface and skips
those locations in `_validate_start_locations._record_if_occupied`. The
full SpawnAction + dispense() wire-call work lands on a follow-up branch;
this commit is just the bug fix the SMC adaptive slow tests need.
"""

import pytest

from orca.devices.devices import Storage
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.runtime_interface import StartLocationsOccupiedError
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_system_runtime import _build_simple_system


class TestPreCheckSkipsIPlateSource:
    """The pre-submit check at `_validate_start_locations` must skip
    locations whose `_resource` is an `IPlateSource`."""

    async def test_storage_backed_start_location_with_occupant_does_not_refuse(
        self,
    ) -> None:
        """The canonical SMC adaptive failure shape: a Storage-backed
        start_location holds a plate (e.g., previous submission's plate
        mid-pick at the output position). A new submission targeting
        the same start_location MUST be accepted -- the source still
        has plates queued behind the one at output."""
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            # Replace pad1's PlatePad-backed location with a Storage-backed
            # location to exercise the IPlateSource path. Real systems wrap
            # device resources in LabwareStagingBridge (per `sdk/build.py`);
            # the bridge holds the device + the staged-labware slot.
            pad1 = system.system_map.get_location("pad1")
            storage = Storage("test_storage")
            bridge = LabwareStagingBridge("pad1", storage)
            pad1._resource = bridge

            # Drop a plate at the source's output so the slot is occupied.
            existing_plate = LabwareInstance("plate_96", "96_well")
            system.add_labware(existing_plate)
            pad1.initialize_labware(existing_plate)
            assert pad1.labware is existing_plate

            # Submit the workflow whose entry thread starts at pad1. With
            # the IPlateSource skip, the pre-check accepts. Without it
            # (pre-fix), this raises StartLocationsOccupiedError.
            sub = await runtime.submit_workflow("simple_workflow", mode=WorkflowRunMode.PURE_SIM)
            assert sub is not None
        finally:
            await runtime.shutdown()

    async def test_platepad_backed_start_location_with_occupant_still_refuses(
        self,
    ) -> None:
        """Regression guard: the pre-check MUST still refuse for PlatePad-
        backed start_locations. PlatePads are single-slot stages; an
        occupied PlatePad IS blocked. This is the start_location protection
        that prevents the silent stall on operator-left labware.
        """
        system, _ = await _build_simple_system()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        try:
            # pad1 is a PlatePad by default. Place a plate there.
            pad1 = system.system_map.get_location("pad1")
            existing_plate = LabwareInstance("plate_96", "96_well")
            system.add_labware(existing_plate)
            pad1.initialize_labware(existing_plate)
            assert pad1.labware is existing_plate

            # Submit MUST refuse -- PlatePad single-slot semantic is unchanged.
            with pytest.raises(StartLocationsOccupiedError):
                await runtime.submit_workflow("simple_workflow", mode=WorkflowRunMode.PURE_SIM)
        finally:
            await runtime.shutdown()
