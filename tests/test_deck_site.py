"""Tests for DeckSite -- deck position state tracking."""
from tests.mock import EXTERNAL_MOVER

from typing import Callable

import pytest

from orca.resource_models.deck_site import DeckSite
from orca.resource_models.gripper_pad import GripperPad
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable
from tests.test_helpers import create_test_labware_instance


# Constructor-name round-trip is the same invariant on every ILabwarePlaceable
# subclass; parametrize over both leaf types rather than duplicate the test
# across `test_deck_site.py::test_name` and `test_gripper_pad.py::test_name`.
@pytest.mark.parametrize(
    ("factory", "name"),
    [
        (DeckSite, "hamilton_1/pos1"),
        (GripperPad, "robotic_arm/gripper"),
    ],
)
def test_labware_placeable_name_round_trips(
    factory: Callable[[str], ILabwarePlaceable], name: str,
) -> None:
    instance = factory(name)
    assert instance.name == name


class TestDeckSiteBasics:

    def test_starts_empty(self) -> None:
        site = DeckSite("hamilton_1/pos1")
        assert site.labware is None
        assert site.loaded_labware == []

    def test_supports_deadlock_resolution_is_false(self) -> None:
        site = DeckSite("hamilton_1/pos1")
        assert site.supports_deadlock_resolution is False


class TestDeckSiteLabwareTracking:

    @pytest.mark.asyncio
    async def test_notify_placed_sets_labware(self) -> None:
        site = DeckSite("hamilton_1/pos1")
        lw = await create_test_labware_instance("plate_1")
        await site.notify_placed(lw, EXTERNAL_MOVER)
        assert site.labware is lw
        assert site.loaded_labware == [lw]

    @pytest.mark.asyncio
    async def test_notify_picked_clears_labware(self) -> None:
        site = DeckSite("hamilton_1/pos1")
        lw = await create_test_labware_instance("plate_1")
        await site.notify_placed(lw, EXTERNAL_MOVER)
        await site.notify_picked(lw, EXTERNAL_MOVER)
        assert site.labware is None
        assert site.loaded_labware == []

    @pytest.mark.asyncio
    async def test_prepare_for_pick_is_noop(self) -> None:
        site = DeckSite("hamilton_1/pos1")
        lw = await create_test_labware_instance("plate_1")
        await site.notify_placed(lw, EXTERNAL_MOVER)
        await site.prepare_for_pick(lw, EXTERNAL_MOVER)
        assert site.labware is lw

    @pytest.mark.asyncio
    async def test_prepare_for_place_is_noop(self) -> None:
        site = DeckSite("hamilton_1/pos1")
        lw = await create_test_labware_instance("plate_1")
        await site.prepare_for_place(lw, EXTERNAL_MOVER)
        assert site.labware is None

    async def test_initialize_labware(self) -> None:
        site = DeckSite("hamilton_1/pos1")
        lw = await create_test_labware_instance("plate_1")
        site.initialize_labware(lw)
        assert site.labware is lw
