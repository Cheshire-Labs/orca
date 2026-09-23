"""A device mutex keeps outside labware out; it must not lock inside labware in.

Reproduced on the PLR example: a thread runs two actions back to back on the
same liquid handler. The second action's hold sanctions only its own inputs, so
the plate and the tip rack the FIRST action left on the deck were refused every
crossing site they needed to leave. They stayed put, the site the second
action's new tip rack had to land on stayed occupied, and the whole execution
sat there until an external timeout: a plate that cannot leave a device blocks
the device it is sitting in.
"""
import pytest

from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import LocationReservationManager
from orca.system.resource_registry import ResourceRegistry
from orca.resource_models.location import Location
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.location_action import ActionBodyLocationAction
from orca.workflow_models.actions.util import AssignedLabwareManager
from orca.workflow_models.action_context import ActionContext

from tests.mock import EXTERNAL_MOVER, UniversalMockDevice
from tests.test_helpers import create_test_labware_instance


async def _device_held_by_an_action_with_no_inputs() -> tuple[
    LocationReservationManager, DeckSiteLocation
]:
    """A two-site device whose mutex is held by an action that claims no labware.

    That is the successor action mid-wait: it holds the device, and nothing on
    the deck belongs to it.
    """
    device = UniversalMockDevice("flex")
    system_map = SystemMap(ResourceRegistry())
    mutex = Location("flex")
    system_map.register_mutex_location(mutex)

    deck_slot = DeckSiteLocation("flex/C2-slot", device, mutex_position_id="flex")
    crossing = DeckSiteLocation("flex/handoff", device, mutex_position_id="flex")
    for site in (deck_slot, crossing):
        await system_map.add_site_location(site)
        device.add_site(site)
    await system_map.add_location(Location("pad_1"))

    manager = LocationReservationManager(system_map)

    async def _body(ctx: ActionContext) -> None:
        del ctx

    action = ActionBodyLocationAction(_body, "second_action")
    action.set_device(device)
    action.set_assigned_labware_manager(AssignedLabwareManager([], []))
    hold = LocationReservation(mutex)
    await manager.attempt_reservation("flex", hold, thread_id="owner-thread")
    assert hold.granted.is_set()
    action.set_location_reservation(hold)
    return manager, deck_slot


@pytest.mark.asyncio
async def test_a_plate_left_on_the_deck_can_still_reserve_a_crossing_site() -> None:
    manager, deck_slot = await _device_held_by_an_action_with_no_inputs()
    leftover = await create_test_labware_instance("leftover_plate")
    await deck_slot.notify_placed(leftover, EXTERNAL_MOVER)

    assert manager.can_reserve(
        "flex/handoff", thread_id="leftover-thread", requesting_labware_id=leftover.id
    ) is True, "labware standing on the device must be able to step off it"


@pytest.mark.asyncio
async def test_labware_outside_the_device_is_still_refused() -> None:
    manager, _deck_slot = await _device_held_by_an_action_with_no_inputs()
    outsider = await create_test_labware_instance("outsider_plate")

    assert manager.can_reserve(
        "flex/handoff", thread_id="outsider-thread", requesting_labware_id=outsider.id
    ) is False, "the hold still keeps uninvited labware from arriving"


@pytest.mark.asyncio
async def test_the_sanction_lapses_once_the_labware_has_left() -> None:
    manager, deck_slot = await _device_held_by_an_action_with_no_inputs()
    leaver = await create_test_labware_instance("leaver_plate")
    await deck_slot.notify_placed(leaver, EXTERNAL_MOVER)
    await deck_slot.prepare_for_pick(leaver, EXTERNAL_MOVER)
    await deck_slot.notify_picked(leaver, EXTERNAL_MOVER)

    assert manager.can_reserve(
        "flex/handoff", thread_id="leaver-thread", requesting_labware_id=leaver.id
    ) is False, "the sanction follows where the labware is, it does not stick"
