"""LabwarePlacer writes each position holder whole or not at all, and never undoes a plate that is physically there."""

from unittest.mock import AsyncMock, patch

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import InMemoryLabwareLocationService, PlacementState
from orca.resource_models.labware_placement import Arrival, LabwarePlacer, Relocation
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.workflow_models import spawn_actions
from tests.test_helpers import no_forgetting
from tests.placements_for_tests import this_tests_placements
from tests.memory_activity import memory_labware_service, memory_unsaved_positions

pytestmark = pytest.mark.asyncio


class _UnreachableDeck:
    async def __call__(self, labware: LabwareInstance, *locations: Location) -> None:
        raise RuntimeError("the handler did not answer")


async def _no_rebuild(device_location: Location) -> None:
    return None


async def _no_staling() -> None:
    return None


def _pad(name: str) -> Location:
    return Location(name, resource=PlatePad(name))


async def test_a_move_the_device_refuses_leaves_the_plate_where_it_was() -> None:
    """Half a move is worse than none: a refused projection puts both slots and the ledger back."""
    service = InMemoryLabwareLocationService(this_tests_placements())
    placer = LabwarePlacer(service, memory_labware_service(), memory_unsaved_positions(), _UnreachableDeck(), _no_staling, _no_staling, _no_rebuild, no_forgetting)
    source, target = _pad("source"), _pad("target")
    plate = LabwareInstance("plate", "96_well")
    await source.place_labware(plate)
    service.update(plate, source)

    with pytest.raises(RuntimeError, match="did not answer"):
        await placer.relocate(plate, source, target, Relocation.STATED)

    assert (source.labware, target.labware) == (plate, None)
    assert service.get(plate) is source


async def test_a_spawned_plate_stays_recorded_when_the_device_cannot_be_told() -> None:
    """A dispense put the plate there. Undoing its record would let the next dispense jam the output."""
    service = InMemoryLabwareLocationService(this_tests_placements())
    placer = LabwarePlacer(service, memory_labware_service(), memory_unsaved_positions(), _UnreachableDeck(), _no_staling, _no_staling, _no_rebuild, no_forgetting)
    output = _pad("output")
    plate = LabwareInstance("plate", "96_well")

    with pytest.raises(RuntimeError, match="did not answer"):
        await spawn_actions._place_retrying_on_busy(output, plate, "test", placer)

    assert output.labware is plate
    assert service.get(plate) is output


async def test_labware_that_leaves_is_retired_even_when_its_slot_will_not_let_go() -> None:
    service = InMemoryLabwareLocationService(this_tests_placements())
    placer = LabwarePlacer(service, memory_labware_service(), memory_unsaved_positions(), _UnreachableDeck(), _no_staling, _no_staling, _no_rebuild, no_forgetting)
    pad = _pad("pad")
    plate = LabwareInstance("plate", "96_well")
    await placer.arrived(plate, pad, Arrival.RESTORED)

    with patch.object(pad, "dispose_labware", AsyncMock(side_effect=RuntimeError("the slot is stuck"))):
        with pytest.raises(RuntimeError, match="stuck"):
            await placer.left(plate, pad)

    assert service.placement(plate) is PlacementState.RETIRED
