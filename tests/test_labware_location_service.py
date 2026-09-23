"""Tests for ILabwareLocationService -- single source of truth for labware position."""

import pytest
from orca.resource_models.labware_location_service import (
    ArrivalMechanism,
    ILabwareLocationService,
    InMemoryLabwareLocationService,
    PlacementState,
)
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from tests.test_helpers import create_test_labware_instance


def _make_location(name: str) -> Location:
    return Location(name, PlatePad(name))


class TestInMemoryLabwareLocationService:

    async def test_update_and_get(self) -> None:
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")
        loc = _make_location("shaker1")

        service.update(labware, loc)
        assert service.get(labware) is loc

    async def test_get_raises_for_unknown_labware(self) -> None:
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")

        with pytest.raises(KeyError):
            service.get(labware)

    async def test_update_overwrites_previous(self) -> None:
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")
        loc1 = _make_location("shaker1")
        loc2 = _make_location("washer1")

        service.update(labware, loc1)
        service.update(labware, loc2)
        assert service.get(labware) is loc2

    async def test_get_all_returns_copy(self) -> None:
        service = InMemoryLabwareLocationService()
        lw1 = await create_test_labware_instance("plate_1")
        lw2 = await create_test_labware_instance("plate_2")
        loc1 = _make_location("shaker1")
        loc2 = _make_location("washer1")

        service.update(lw1, loc1)
        service.update(lw2, loc2)

        result = service.get_all()
        assert len(result) == 2
        assert result[lw1] is loc1
        assert result[lw2] is loc2

        # Verify it's a copy
        result[lw1] = loc2
        assert service.get(lw1) is loc1

    async def test_update_appends_to_history(self) -> None:
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")
        loc1 = _make_location("shaker1")
        loc2 = _make_location("washer1")
        loc3 = _make_location("reader1")

        service.update(labware, loc1)
        service.update(labware, loc2)
        service.update(labware, loc3)

        history = service.get_history(labware)
        assert len(history) == 3
        names = history.get_history_names()
        assert names == ["shaker1", "washer1", "reader1"]

    async def test_get_history_raises_for_unknown_labware(self) -> None:
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")

        with pytest.raises(KeyError):
            service.get_history(labware)

    async def test_reset_clears_history_and_sets_location(self) -> None:
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")
        loc1 = _make_location("shaker1")
        loc2 = _make_location("washer1")
        loc3 = _make_location("reader1")

        service.update(labware, loc1)
        service.update(labware, loc2)

        service.reset(labware, loc3)

        assert service.get(labware) is loc3
        history = service.get_history(labware)
        assert len(history) == 1
        assert history.get_history_names() == ["reader1"]

    async def test_history_current_location_matches_get(self) -> None:
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")
        loc1 = _make_location("shaker1")
        loc2 = _make_location("washer1")

        service.update(labware, loc1)
        service.update(labware, loc2)

        history = service.get_history(labware)
        assert history.get_current_location() is loc2
        assert service.get(labware) is loc2

    async def test_history_previous_location(self) -> None:
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")
        loc1 = _make_location("shaker1")
        loc2 = _make_location("washer1")

        service.update(labware, loc1)
        service.update(labware, loc2)

        history = service.get_history(labware)
        assert history.get_previous_location() is loc1

    async def test_multiple_labware_independent_histories(self) -> None:
        service = InMemoryLabwareLocationService()
        lw1 = await create_test_labware_instance("plate_1")
        lw2 = await create_test_labware_instance("plate_2")
        loc_a = _make_location("shaker1")
        loc_b = _make_location("washer1")

        service.update(lw1, loc_a)
        service.update(lw2, loc_b)

        assert service.get(lw1) is loc_a
        assert service.get(lw2) is loc_b
        assert len(service.get_history(lw1)) == 1
        assert len(service.get_history(lw2)) == 1


class TestExpectedPlacements:
    """A labware belongs somewhere before it is there, and the ledger has to say
    which of the two it means -- only PRESENT is a claim about physical reality,
    and only PRESENT is mirrored into the durable store."""

    async def test_an_expectation_answers_get_but_fires_no_update(self) -> None:
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")
        pad = _make_location("pad1")
        seen: list[str] = []
        service.add_update_listener(lambda lw, loc: seen.append(lw.id))

        service.expect(labware, pad, ArrivalMechanism.MANUAL_PLACE)

        assert service.get(labware) is pad
        assert service.placement(labware) is PlacementState.EXPECTED
        assert seen == [], "an expectation must not reach the store"
        assert len(service.get_history(labware)) == 0, "nothing has been anywhere"

    async def test_arriving_fires_the_update_even_at_the_expected_location(
        self,
    ) -> None:
        """The position does not change on arrival, so a same-position check
        alone would swallow the one write that matters."""
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")
        pad = _make_location("pad1")
        seen: list[str] = []
        service.add_update_listener(lambda lw, loc: seen.append(lw.id))

        service.expect(labware, pad, ArrivalMechanism.MANUAL_PLACE)
        service.update(labware, pad)

        assert service.placement(labware) is PlacementState.PRESENT
        assert seen == [labware.id]

    async def test_expected_at_answers_the_longest_waiting_first(self) -> None:
        """Two threads awaiting one pad must bind two different labwares, in the
        order they started waiting -- not both take whatever showed up."""
        service = InMemoryLabwareLocationService()
        first = await create_test_labware_instance("plate_1")
        second = await create_test_labware_instance("plate_1")
        pad = _make_location("pad1")
        service.expect(first, pad, ArrivalMechanism.MANUAL_PLACE)
        service.expect(second, pad, ArrivalMechanism.MANUAL_PLACE)

        assert service.expected_at(
            "pad1", ArrivalMechanism.MANUAL_PLACE, "plate_1",
        ) is first
        service.update(first, pad)
        assert service.expected_at(
            "pad1", ArrivalMechanism.MANUAL_PLACE, "plate_1",
        ) is second

    async def test_expected_at_discriminates_template_and_mechanism(self) -> None:
        service = InMemoryLabwareLocationService()
        plate = await create_test_labware_instance("plate_1")
        pad = _make_location("pad1")
        service.expect(plate, pad, ArrivalMechanism.MANUAL_PLACE)

        assert service.expected_at(
            "pad1", ArrivalMechanism.MANUAL_PLACE, "tips_1",
        ) is None
        assert service.expected_at("pad1", ArrivalMechanism.DISPENSE) is None
        # No template filter is how a caller finds out something else is awaited.
        assert service.expected_at("pad1", ArrivalMechanism.MANUAL_PLACE) is plate

    async def test_stop_expecting_erases_it_where_retire_would_remember(
        self,
    ) -> None:
        """A retired labware keeps its last position because it really was
        there. One that never arrived has nothing worth keeping."""
        service = InMemoryLabwareLocationService()
        arrived = await create_test_labware_instance("plate_1")
        never = await create_test_labware_instance("plate_2")
        pad = _make_location("pad1")
        other = _make_location("pad2")
        dropped: list[str] = []
        service.add_expectation_dropped_listener(lambda lw: dropped.append(lw.id))

        service.update(arrived, pad)
        service.retire(arrived)
        service.expect(never, other, ArrivalMechanism.MANUAL_PLACE)
        service.stop_expecting(never)

        assert service.placement(arrived) is PlacementState.RETIRED
        assert service.get(arrived) is pad
        assert dropped == [never.id]
        with pytest.raises(KeyError):
            service.placement(never)

    async def test_retire_leaves_an_expectation_alone(self) -> None:
        """The two verbs guard on different states, so a caller that fires both
        (a wipe) does the right one and no-ops the other."""
        service = InMemoryLabwareLocationService()
        labware = await create_test_labware_instance("plate_1")
        pad = _make_location("pad1")
        service.expect(labware, pad, ArrivalMechanism.MANUAL_PLACE)

        service.retire(labware)

        assert service.placement(labware) is PlacementState.EXPECTED
