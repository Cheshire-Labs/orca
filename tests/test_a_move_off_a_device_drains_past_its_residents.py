"""A device carrying a resident must still be given up when the traveller leaves.

``release_after_move_if_drained`` asked whether the device was empty of the
action's outputs. A deck-resident reagent trough is one of those outputs and
never comes off, so on any device holding one the answer was permanently no and
the hold was never dropped here.

It runs after every completed move, and the one that matters is the move that
takes a finished thread to its end location: on an SMC run, measured, 34 holds
per run that this site could have dropped a hop earlier and did not.
"""
from unittest.mock import MagicMock

import pytest

from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import LocationReservationManager
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.actions.executable_location_action import (
    ExecutableLocationAction,
)
from orca.workflow_models.actions.location_action import ActionBodyLocationAction
from orca.workflow_models.labware_threads.reservation_holdover import (
    ReservationHoldover,
)
from orca.workflow_models.actions.util import AssignedLabwareManager

from tests.mock import EXTERNAL_MOVER, UniversalMockDevice
from tests.test_helpers import create_test_labware_instance


async def _device_holding_a_trough_and_a_plate() -> tuple[
    LocationReservationManager,
    ActionBodyLocationAction,
    DeckSiteLocation,
    LabwareInstance,
    LabwareInstance,
]:
    """An action holding a two-site device, with a trough and a plate as its
    declared outputs, both standing on the device."""
    device = UniversalMockDevice("lh")
    system_map = SystemMap(ResourceRegistry())
    mutex = Location("lh")
    system_map.register_mutex_location(mutex)

    trough_site = DeckSiteLocation("lh/trough-slot", device, mutex_position_id="lh")
    plate_site = DeckSiteLocation("lh/plate-slot", device, mutex_position_id="lh")
    for site in (trough_site, plate_site):
        await system_map.add_site_location(site)
        device.add_site(site)

    manager = LocationReservationManager(system_map)
    trough = await create_test_labware_instance("trough_1")
    plate = await create_test_labware_instance("plate_1")
    trough_template, plate_template = trough.template, plate.template
    assert trough_template is not None and plate_template is not None

    async def _body(ctx: ActionContext) -> None:
        del ctx

    action = ActionBodyLocationAction(_body, "add_reagent")
    action.set_device(device)
    labware_manager = AssignedLabwareManager(
        [trough_template, plate_template], [trough_template, plate_template]
    )
    action.set_assigned_labware_manager(labware_manager)
    labware_manager.assign_input(trough_template, trough)
    labware_manager.assign_input(plate_template, plate)

    hold = LocationReservation(mutex)
    await manager.attempt_reservation("lh", hold, thread_id="owner")
    assert hold.granted.is_set()
    action.set_location_reservation(hold)

    await trough_site.notify_placed(trough, EXTERNAL_MOVER)
    await plate_site.notify_placed(plate, EXTERNAL_MOVER)
    return manager, action, plate_site, plate, trough


async def _pick(site: DeckSiteLocation, labware: LabwareInstance) -> None:
    await site.prepare_for_pick(labware, EXTERNAL_MOVER)
    await site.notify_picked(labware, EXTERNAL_MOVER)


def _holdover_holding(action: ActionBodyLocationAction, trough_id: str) -> ReservationHoldover:
    """A holdover carrying the same residency check a real thread carries."""
    holdover = ReservationHoldover(lambda labware_id: labware_id == trough_id)
    executable = MagicMock(spec=ExecutableLocationAction)
    executable.action = action
    holdover.acquire_after_action(executable, owns_reservation=True)
    return holdover


@pytest.mark.asyncio
async def test_a_move_off_the_device_drops_the_hold_when_only_a_resident_is_left() -> None:
    """The whole point: the traveller leaves, the trough stays, and the move
    releases the device instead of holding it for a departure that will never
    happen."""
    manager, action, plate_site, plate, trough = (
        await _device_holding_a_trough_and_a_plate()
    )
    holdover = _holdover_holding(action, trough.id)

    holdover.release_after_move_if_drained()
    assert holdover.has_current(), "the traveller is still on the deck"

    await _pick(plate_site, plate)
    holdover.release_after_move_if_drained()
    assert not holdover.has_current()
    assert manager.get_reservation_at("lh") is None, (
        "only the resident trough is left, so the device must read as free"
    )


@pytest.mark.asyncio
async def test_the_holdover_hands_its_own_residency_check_to_the_action() -> None:
    """Without this the action falls back to the strict reading and the fix is
    silently undone."""
    check = MagicMock(return_value=True)
    holdover = ReservationHoldover(check)
    executable = MagicMock(spec=ExecutableLocationAction)
    executable.action = MagicMock()
    executable.action.only_residents_remain = MagicMock(return_value=False)
    holdover.acquire_after_action(executable, owns_reservation=True)

    holdover.release_after_move_if_drained()
    executable.action.set_residency_check.assert_called_once_with(check)


@pytest.mark.asyncio
async def test_a_resident_left_behind_does_not_hold_the_device() -> None:
    """The traveller leaves, the trough stays, and the device counts as drained."""
    _manager, action, plate_site, plate, trough = (
        await _device_holding_a_trough_and_a_plate()
    )
    action.set_residency_check(lambda labware_id: labware_id == trough.id)

    assert action.only_residents_remain() is False, "the traveller is still on the deck"

    await _pick(plate_site, plate)
    assert action.only_residents_remain() is True, (
        "only the resident trough is left, and it is never coming off"
    )


@pytest.mark.asyncio
async def test_without_a_residency_check_the_device_must_be_empty() -> None:
    """No check means nothing counts as resident, so the strict reading holds."""
    _manager, action, plate_site, plate, _trough = (
        await _device_holding_a_trough_and_a_plate()
    )

    await _pick(plate_site, plate)
    assert action.only_residents_remain() is False


@pytest.mark.asyncio
async def test_an_armed_hold_is_never_the_holdover_the_thread_still_carries() -> None:
    """The residency check is set in two places, and the second one would
    rewrite the manager's takeover predicate if it could reach an already-armed
    action. It cannot: arming clears the holdover first."""
    _manager, action, _plate_site, _plate, trough = (
        await _device_holding_a_trough_and_a_plate()
    )
    executable = MagicMock(spec=ExecutableLocationAction)
    executable.action = action
    holdover = ReservationHoldover(lambda labware_id: labware_id == trough.id)
    holdover.acquire_after_action(executable, owns_reservation=True)

    holdover.release_current_when_drained()

    assert action.reservation.pending_drain_check is not None, "not armed"
    assert holdover.current() is None, (
        "an armed action is still the current holdover, so a later "
        "set_residency_check could rewrite the manager's predicate"
    )
