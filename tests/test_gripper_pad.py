"""Tests for GripperPad -- gripper location state tracking."""
from tests.mock import EXTERNAL_MOVER

import pytest

from orca.resource_models.gripper_pad import GripperPad
from tests.test_helpers import create_test_labware_instance


class TestGripperPadBasics:

    def test_starts_empty(self) -> None:
        pad = GripperPad("arm/gripper")
        assert pad.labware is None
        assert pad.loaded_labware == []

    # Constructor-name round-trip is covered by the parametrized
    # `test_labware_placeable_name_round_trips` in test_deck_site.py.

    def test_supports_deadlock_resolution_is_false(self) -> None:
        pad = GripperPad("arm/gripper")
        assert pad.supports_deadlock_resolution is False


class TestGripperPadLabwareTracking:

    @pytest.mark.asyncio
    async def test_notify_placed_sets_labware(self) -> None:
        pad = GripperPad("arm/gripper")
        lw = await create_test_labware_instance("plate_1")
        await pad.notify_placed(lw, EXTERNAL_MOVER)
        assert pad.labware is lw
        assert pad.loaded_labware == [lw]

    @pytest.mark.asyncio
    async def test_notify_picked_clears_labware(self) -> None:
        pad = GripperPad("arm/gripper")
        lw = await create_test_labware_instance("plate_1")
        await pad.notify_placed(lw, EXTERNAL_MOVER)
        await pad.notify_picked(lw, EXTERNAL_MOVER)
        assert pad.labware is None
        assert pad.loaded_labware == []

    @pytest.mark.asyncio
    async def test_prepare_for_pick_is_noop(self) -> None:
        pad = GripperPad("arm/gripper")
        lw = await create_test_labware_instance("plate_1")
        await pad.notify_placed(lw, EXTERNAL_MOVER)
        await pad.prepare_for_pick(lw, EXTERNAL_MOVER)
        assert pad.labware is lw

    @pytest.mark.asyncio
    async def test_prepare_for_place_is_noop(self) -> None:
        pad = GripperPad("arm/gripper")
        lw = await create_test_labware_instance("plate_1")
        await pad.prepare_for_place(lw, EXTERNAL_MOVER)
        assert pad.labware is None

    async def test_initialize_labware(self) -> None:
        pad = GripperPad("arm/gripper")
        lw = await create_test_labware_instance("plate_1")
        pad.initialize_labware(lw)
        assert pad.labware is lw
