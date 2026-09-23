"""DispenseSpawn unit tests.

`DispenseSpawn` is the start-side strategy for `@orca.thread(start=("loc",
DISPENSE))` -- an IPlateSource-backed location (stacker / hotel) that
physically advances its queue via `device.dispense()` before the engine
writes the slot. Run-mode-agnostic: sim drivers handle PURE_SIM /
DEVICE_SIM internally; the strategy always calls dispense().

`acquire` takes the whole thread rather than its labware, which is what
lets every strategy read `run_mode` and the rest off one object.
"""
from orca.resource_models.labware_placeable_interface import IPlateMover

from unittest.mock import AsyncMock

import pytest

from orca.devices.devices import Storage
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import ILabwareLocationObserver, LabwareLocationEvent, Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.resource_models.labware_location_service import (
    InMemoryLabwareLocationService,
)
from orca.workflow_models.spawn_actions import DispenseSpawn


def _make_storage_location(name: str = "stacker_1") -> tuple[Location, Storage]:
    storage = Storage(name)
    bridge = LabwareStagingBridge(name, storage)
    location = Location(name)
    location._resource = bridge
    return location, storage


def _make_platepad_location(name: str = "pad1") -> Location:
    pad = PlatePad(name)
    return Location(name, resource=pad)


def _fresh_labware(template_name: str = "plate_96") -> LabwareInstance:
    return LabwareInstance(template_name, "96_well")


def _make_thread(
    labware: LabwareInstance,
    start_location: Location,
    *,
    run_mode: WorkflowRunMode = WorkflowRunMode.PURE_SIM,
) -> LabwareThreadInstance:
    return LabwareThreadInstance(
        labware=labware,
        start_location=start_location,
        end_locations=[start_location],
        run_mode=run_mode,
    )


class TestDispenseSpawn:
    """`DispenseSpawn.acquire` advances the IPlateSource queue then writes
    the orca-side slot. Behavior unchanged from FromSourceSpawn."""

    async def test_dispense_called_before_slot_write(self) -> None:
        location, storage = _make_storage_location()
        labware = _fresh_labware()

        call_order: list[str] = []

        async def tracking_dispense() -> None:
            call_order.append("dispense")

        storage.dispense = tracking_dispense  # type: ignore[method-assign]

        class _OrderObserver(ILabwareLocationObserver):
            async def notify_labware_location_change(
                self, event: LabwareLocationEvent, location: Location, labware: LabwareInstance,
            ) -> None:
                call_order.append(f"notify:{event.value}")

        location.add_observer(_OrderObserver())

        thread = _make_thread(labware, location)
        spawn = DispenseSpawn(location, InMemoryLabwareLocationService())
        await spawn.acquire(thread)

        assert call_order == ["dispense", "notify:initialized"]
        assert location.labware is labware

    async def test_dispense_called_once_per_acquire(self) -> None:
        location, storage = _make_storage_location()
        dispense_mock = AsyncMock(return_value=None)
        storage.dispense = dispense_mock  # type: ignore[method-assign]

        labware = _fresh_labware("a")
        thread = _make_thread(labware, location)
        spawn = DispenseSpawn(location, InMemoryLabwareLocationService())
        await spawn.acquire(thread)

        dispense_mock.assert_awaited_once_with()

    async def test_raises_if_location_is_not_plate_source(self) -> None:
        location = _make_platepad_location()
        with pytest.raises(TypeError, match="IPlateSource"):
            DispenseSpawn(location, InMemoryLabwareLocationService())

    async def test_direct_iplate_source_resource_accepted(self) -> None:
        """A Location whose `_resource` is itself an IPlateSource (no
        LabwareStagingBridge wrap) is wired as the spawn's source: the
        constructor resolves it directly (no unwrap) and `acquire` drives
        `dispense()` on it before writing the orca-side slot."""
        from orca.devices.device_interfaces import IPlateSource

        class _SourceShaped(IPlateSource, ILabwarePlaceable):
            def __init__(self) -> None:
                self._labware: LabwareInstance | None = None
                self.dispense_calls = 0

            @property
            def name(self) -> str:
                return "src"

            @property
            def labware(self) -> LabwareInstance | None:
                return self._labware

            @property
            def loaded_labware(self) -> list[LabwareInstance]:
                return []

            @property
            def supports_deadlock_resolution(self) -> bool:
                return False

            def initialize_labware(self, labware: LabwareInstance) -> None:
                self._labware = labware

            async def prepare_for_place(self, labware: LabwareInstance, mover: IPlateMover) -> None:
                pass

            async def prepare_for_pick(self, labware: LabwareInstance, mover: IPlateMover) -> None:
                pass

            async def notify_placed(self, labware: LabwareInstance, mover: IPlateMover) -> None:
                pass

            async def notify_picked(self, labware: LabwareInstance, mover: IPlateMover) -> None:
                self._labware = None

            async def dispose_labware(self, labware: LabwareInstance) -> None:
                self._labware = None

            async def dispense(self) -> None:
                self.dispense_calls += 1

        resource = _SourceShaped()
        location = Location("src")
        location._resource = resource

        spawn = DispenseSpawn(location, InMemoryLabwareLocationService())
        assert spawn._source is resource

        labware = _fresh_labware()
        thread = _make_thread(labware, location)
        await spawn.acquire(thread)

        assert resource.dispense_calls == 1
        assert location.labware is labware
