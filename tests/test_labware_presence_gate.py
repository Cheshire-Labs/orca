"""W3/W4: the all-labware-present gate is set explicitly, never on a read.

Pins the 2E behavior:
- ``all_labware_is_present`` is a pure-read property: reading it never fires
  the gate (pre-2E the getter called the side-effecting checker, so a snapshot
  read could race the dispatch loop open).
- ``refresh_labware_presence`` is the explicit gate-set path: it opens the gate
  when no expected input is missing and is a no-op otherwise.
- The placed-labware observer drives that same explicit path.
"""

from typing import List

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import LabwareLocationEvent, Location
from orca.resource_models.plate_pad import PlatePad
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.actions.location_action import ActionBodyLocationAction

from tests.test_helpers import create_test_labware_instance


async def _noop_action_body(ctx: ActionContext) -> None:
    return None


class _GateProbeAction(ActionBodyLocationAction):
    """ActionBodyLocationAction whose missing-input view is controlled directly,
    so the gate wiring can be exercised without a device/location graph."""

    def __init__(self, missing: List[LabwareInstance]) -> None:
        super().__init__(func=_noop_action_body, command="probe")
        self._missing = missing

    def peek_missing_input_labware(self) -> List[LabwareInstance]:
        return list(self._missing)


def _location() -> Location:
    return Location("probe", resource=PlatePad("probe_pad"))


class TestLabwarePresenceGate:
    def test_reading_property_does_not_fire_gate(self) -> None:
        action = _GateProbeAction(missing=[])
        event = action.all_labware_is_present
        assert not event.is_set()

    def test_refresh_opens_gate_when_nothing_missing(self) -> None:
        action = _GateProbeAction(missing=[])
        action.refresh_labware_presence()
        assert action.all_labware_is_present.is_set()

    async def test_refresh_is_noop_when_labware_missing(self) -> None:
        action = _GateProbeAction(missing=[await create_test_labware_instance("plate")])
        action.refresh_labware_presence()
        assert not action.all_labware_is_present.is_set()

    @pytest.mark.asyncio
    async def test_placed_observer_opens_gate_when_present(self) -> None:
        action = _GateProbeAction(missing=[])
        labware = await create_test_labware_instance("plate")
        await action.notify_labware_location_change(LabwareLocationEvent.PLACED, _location(), labware)
        assert action.all_labware_is_present.is_set()

    @pytest.mark.asyncio
    async def test_non_placed_event_does_not_fire_gate(self) -> None:
        action = _GateProbeAction(missing=[])
        labware = await create_test_labware_instance("plate")
        await action.notify_labware_location_change(LabwareLocationEvent.PICKED, _location(), labware)
        assert not action.all_labware_is_present.is_set()
