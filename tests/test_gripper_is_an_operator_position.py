"""A mover's jaws are a position an operator can name, and one record.

Two gaps this pins. First, `list-labware` prints `<mover>/gripper` from the
ledger while `edit-labware-location` on that exact string answered `not_found`:
the read surface emitted a position the write surface refused. Second, whether
a mover holds labware was tracked twice (the mover's own field and the gripper
`Location`'s slot) and only the field was ever written, so an operator
assertion could set one and leave the arm's own idea of itself untouched.

The jaws stay off the routing graph. Routing answers "which arm carries this
from A to B", which a node every move passes through cannot help with.
"""
import pytest

from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter_base import TransporterBase
from orca.runtime.facades.registry import RegistryFacade
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap


class _FakeMover(TransporterBase):
    async def initialize(self) -> None: ...

    @property
    def is_initialized(self) -> bool:
        return True

    async def _do_pick(self, location: Location) -> None:
        # Stand in for the placement chokepoint, which is what records the
        # plate in the jaws; the mover only reads that record.
        labware = location.labware
        assert labware is not None
        await location.dispose_labware(labware)
        await self.gripper_location.place_labware(labware)

    async def _do_place(self, location: Location) -> None:
        held = self.labware
        assert held is not None
        await self.gripper_location.dispose_labware(held)
        await location.place_labware(held)

    async def get_teachpoints(self):
        return []


async def _map_with_mover() -> tuple[SystemMap, _FakeMover]:
    registry = ResourceRegistry()
    mover = _FakeMover("pf400_1")
    registry.add_resource(mover)
    system_map = SystemMap(registry)
    await system_map.initialize_transporters()
    await system_map.add_location(Location("pad_1", resource=PlatePad("pad_1")))
    return system_map, mover


def _occupied(position_id: str, labware_name: str) -> tuple[Location, LabwareInstance]:
    location = Location(position_id, resource=PlatePad(position_id))
    labware = LabwareInstance(labware_name, "tiprack")
    location.initialize_labware(labware)
    return location, labware


class TestTheJawsAreOneRecord:
    async def test_a_pick_shows_up_on_the_gripper_location_too(self) -> None:
        """The mover's own answer and the gripper Location's slot are the same
        record, so an operator writing either cannot leave the other stale."""
        _, mover = await _map_with_mover()
        source, rack = _occupied("pad_1", "tiprack_1")

        await mover.pick(source)

        assert mover.labware is rack
        assert mover.gripper_location.labware is rack

    async def test_a_place_empties_both(self) -> None:
        _, mover = await _map_with_mover()
        source, rack = _occupied("pad_1", "tiprack_1")
        target = Location("pad_2", resource=PlatePad("pad_2"))
        await mover.pick(source)

        await mover.place(target)

        assert mover.labware is None
        assert mover.gripper_location.labware is None

    async def test_placing_into_full_jaws_is_refused(self) -> None:
        """Guards the arm: a second plate into an occupied gripper is a crash,
        so the gripper refuses it the way any single-occupant slot does."""
        _, mover = await _map_with_mover()
        source, _ = _occupied("pad_1", "tiprack_1")
        await mover.pick(source)
        other, _ = _occupied("pad_2", "tiprack_2")

        with pytest.raises(SlotOccupiedError):
            await mover.pick(other)


class TestAnOperatorCanNameTheJaws:
    async def test_the_gripper_resolves_as_a_placement_target(self) -> None:
        """The string `list-labware` prints is the string edit-location takes."""
        system_map, mover = await _map_with_mover()

        resolved = system_map.resolve_placement_location("pf400_1/gripper")

        assert resolved is mover.gripper_location

    async def test_the_gripper_is_not_a_journey_destination(self) -> None:
        """A thread cannot start or end in a gripper; it is a position labware
        passes through, never one it rests at."""
        system_map, _ = await _map_with_mover()

        with pytest.raises(KeyError):
            system_map.resolve_journey_location("pf400_1/gripper")

    async def test_the_gripper_is_not_a_routing_node(self) -> None:
        system_map, _ = await _map_with_mover()

        assert not system_map.location_exists("pf400_1/gripper")
        assert all(
            location.position_id != "pf400_1/gripper"
            for location in system_map.locations
        )

    async def test_a_topology_site_may_not_shadow_a_gripper(self) -> None:
        """Two Location objects sharing one position id means the ledger holds
        one and the resolver returns the other, so an arm reaches into a slot
        the model calls free."""
        system_map, _ = await _map_with_mover()

        with pytest.raises(ValueError, match="gripper"):
            await system_map.add_location(
                Location("pf400_1/gripper", resource=PlatePad("pf400_1/gripper"))
            )


class TestTheOperatorCanSeeTheJaws:
    async def test_the_snapshot_names_what_the_gripper_holds(self) -> None:
        """`current_labware_id` read an attribute that no longer existed, so it
        answered None with a plate physically in the jaws."""
        _, mover = await _map_with_mover()
        source, rack = _occupied("pad_1", "tiprack_1")
        await mover.pick(source)

        snapshot = RegistryFacade._transporter_snapshot(mover)

        assert snapshot.current_labware_id == rack.id


class TestEveryGripperIsListed:
    async def test_the_mover_list_reaches_a_liquid_handlers_own_gripper(self) -> None:
        """The transporter list is external arms only, so a plate
        stuck in a handler's on-deck gripper appeared on no mover surface at
        all. `list_movers` is the one that answers for every gripper."""
        from orca.runtime.facades.registry import RegistryFacade

        _, mover = await _map_with_mover()
        source, rack = _occupied("pad_1", "tiprack_1")
        await mover.pick(source)

        snapshot = RegistryFacade._mover_snapshot(mover)

        assert snapshot.name == "pf400_1"
        assert snapshot.gripper_position_id == "pf400_1/gripper"
        assert snapshot.current_labware_id == rack.id
