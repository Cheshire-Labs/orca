"""Tests for LabwareStagingBridge -- the physical position where labware sits on a device."""

from typing import List
from unittest.mock import AsyncMock

import pytest

from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.device_error import DeviceBusyError, SlotOccupiedError
from tests.test_helpers import create_test_plate_template
from tests.mock import EXTERNAL_MOVER, UniversalMockDevice


class TestLabwareStagingBridgeStagePipeline:

    async def test_notify_placed_stages_then_loads(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        await (nest.notify_placed(labware, EXTERNAL_MOVER))
        # Single occupancy: the loaded plate still occupies the site; only
        # the approach point reads clear.
        assert nest.labware is labware
        assert nest.accessible_labware is None
        assert labware in nest.loaded_labware

    async def test_sequential_placement_requires_departure(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        lw1 = await create_test_plate_template("a").create_instance()
        lw2 = await create_test_plate_template("b").create_instance()
        await (nest.notify_placed(lw1, EXTERNAL_MOVER))
        with pytest.raises(SlotOccupiedError, match="already holds"):
            await nest.prepare_for_place(lw2, EXTERNAL_MOVER)
        await nest.prepare_for_pick(lw1, EXTERNAL_MOVER)
        await nest.notify_picked(lw1, EXTERNAL_MOVER)
        await (nest.notify_placed(lw2, EXTERNAL_MOVER))
        assert nest.labware is lw2

    async def test_prepare_for_pick_unloads_to_stage(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        await (nest.notify_placed(labware, EXTERNAL_MOVER))
        await (nest.prepare_for_pick(labware, EXTERNAL_MOVER))
        assert nest.labware == labware
        assert labware not in nest.loaded_labware

    async def test_prepare_for_pick_skips_if_staged(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        nest.initialize_labware(labware)
        await (nest.prepare_for_pick(labware, EXTERNAL_MOVER))
        assert nest.labware == labware

    async def test_notify_picked_clears_stage(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        await (nest.notify_placed(labware, EXTERNAL_MOVER))
        await (nest.prepare_for_pick(labware, EXTERNAL_MOVER))
        await (nest.notify_picked(labware, EXTERNAL_MOVER))
        assert nest.labware is None
        assert len(nest.loaded_labware) == 0

    async def test_notify_placed_raises_if_stage_occupied(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        lw1 = await create_test_plate_template("a").create_instance()
        lw2 = await create_test_plate_template("b").create_instance()
        nest.initialize_labware(lw1)
        with pytest.raises(ValueError):
            await (nest.notify_placed(lw2, EXTERNAL_MOVER))

    async def test_notify_picked_of_a_plate_never_here_leaves_the_one_that_is(self) -> None:
        """The bridge does not police which plate left. One record cannot hold
        a plate at two positions, so a pick of something this site never held
        has nothing to undo and must not clear the real occupant."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        lw1 = await create_test_plate_template("a").create_instance()
        lw2 = await create_test_plate_template("b").create_instance()
        await (nest.notify_placed(lw1, EXTERNAL_MOVER))
        await (nest.prepare_for_pick(lw1, EXTERNAL_MOVER))

        await (nest.notify_picked(lw2, EXTERNAL_MOVER))

        assert nest.labware is lw1


class TestLabwareStagingBridgeProperties:

    def test_name(self) -> None:
        nest = LabwareStagingBridge("nest1", UniversalMockDevice("dev1"))
        assert nest.name == "nest1"

    def test_supports_deadlock_resolution_false(self) -> None:
        nest = LabwareStagingBridge("nest1", UniversalMockDevice("dev1"))
        assert nest.supports_deadlock_resolution is False

    def test_labware_none_when_empty(self) -> None:
        nest = LabwareStagingBridge("nest1", UniversalMockDevice("dev1"))
        assert nest.labware is None

    def test_loaded_labware_empty_when_empty(self) -> None:
        nest = LabwareStagingBridge("nest1", UniversalMockDevice("dev1"))
        assert nest.loaded_labware == []


class TestLabwareStagingBridgeInitializeLabware:

    async def test_initialize_stages(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        nest.initialize_labware(labware)
        assert nest.labware == labware
        assert labware not in nest.loaded_labware

    async def test_initialize_idempotent_if_loaded(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        await (nest.notify_placed(labware, EXTERNAL_MOVER))
        nest.initialize_labware(labware)
        assert len(nest.loaded_labware) == 1

    async def test_initialize_raises_if_stage_occupied(self) -> None:
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        lw1 = await create_test_plate_template("a").create_instance()
        lw2 = await create_test_plate_template("b").create_instance()
        nest.initialize_labware(lw1)
        with pytest.raises(DeviceBusyError):
            nest.initialize_labware(lw2)


class TestLabwareStagingBridgeHookDelegation:

    async def test_delegates_notify_placed(self) -> None:
        device = UniversalMockDevice("dev1")
        device._do_notify_placed = AsyncMock()  # type: ignore[method-assign]
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        await (nest.notify_placed(labware, EXTERNAL_MOVER))
        device._do_notify_placed.assert_called_once_with(labware, EXTERNAL_MOVER)

    async def test_delegates_prepare_for_place(self) -> None:
        device = UniversalMockDevice("dev1")
        device._do_prepare_for_place = AsyncMock()  # type: ignore[method-assign]
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        await (nest.prepare_for_place(labware, EXTERNAL_MOVER))
        device._do_prepare_for_place.assert_called_once_with(labware, EXTERNAL_MOVER)

    async def test_delegates_prepare_for_pick(self) -> None:
        device = UniversalMockDevice("dev1")
        device._do_prepare_for_pick = AsyncMock()  # type: ignore[method-assign]
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        await (nest.notify_placed(labware, EXTERNAL_MOVER))
        await (nest.prepare_for_pick(labware, EXTERNAL_MOVER))
        device._do_prepare_for_pick.assert_called_once_with(labware, EXTERNAL_MOVER)

    async def test_delegates_notify_picked(self) -> None:
        device = UniversalMockDevice("dev1")
        device._do_notify_picked = AsyncMock()  # type: ignore[method-assign]
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()
        await (nest.notify_placed(labware, EXTERNAL_MOVER))
        await (nest.prepare_for_pick(labware, EXTERNAL_MOVER))
        await (nest.notify_picked(labware, EXTERNAL_MOVER))
        device._do_notify_picked.assert_called_once_with(labware, EXTERNAL_MOVER)
