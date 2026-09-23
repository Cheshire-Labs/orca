"""TransporterBase template.

Pins the actuator-agnostic mechanics every plate-mover shares - held-labware
guards, lock/in-use gating, state update ordering - so DeckGripperTransporter
can subclass without inheriting the external arm's driver/store/auto-home.
"""
import pytest

from orca.resource_models.device_error import MoverAlreadyHoldingError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.transporter import Transporter
from orca.resource_models.transporter_base import TransporterBase
from orca.resource_models.transporter_interface import ITransporter
from orca.workflow_models.error_policy_overrides import OverrideWithPauseError


class _FakeMover(TransporterBase):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.actuations: list[str] = []

    async def initialize(self) -> None:
        self.actuations.append("initialize")

    @property
    def is_initialized(self) -> bool:
        return True

    async def _do_pick(self, location: Location) -> None:
        self.actuations.append(f"pick:{location.position_id}")
        # The base no longer writes the jaws; in the running system the
        # placement chokepoint does. Stand in for it here.
        labware = location.labware
        assert labware is not None
        await location.dispose_labware(labware)
        await self.gripper_location.place_labware(labware)

    async def _do_place(self, location: Location) -> None:
        self.actuations.append(f"place:{location.position_id}")
        held = self.labware
        assert held is not None
        await self.gripper_location.dispose_labware(held)
        await location.place_labware(held)

    async def get_teachpoints(self):  # pragma: no cover - unused in tests
        return []


def _occupied_location(position_id: str, labware_name: str) -> Location:
    location = Location(position_id)
    location.initialize_labware(LabwareInstance(labware_name, "plate"))
    return location


def test_transporter_subclasses_the_base() -> None:
    assert issubclass(Transporter, TransporterBase)
    assert issubclass(TransporterBase, ITransporter)


async def test_pick_takes_labware_and_place_clears_it() -> None:
    mover = _FakeMover("gripper_1")
    source = _occupied_location("site_a", "plate_1")
    plate = source.labware
    target = Location("site_b")

    await mover.pick(source)
    assert mover.labware is plate
    assert source.labware is None, "one record cannot hold it in two places"
    assert mover.actuations == ["pick:site_a"]

    await mover.place(target)
    assert mover.labware is None
    assert mover.actuations == ["pick:site_a", "place:site_b"]


async def test_pick_guards_double_load_and_empty_source() -> None:
    """A second pick is refused before the arm actuates, and the refusal is
    typed so the run pauses for the operator instead of dying: only a human can
    say whether the plate is still in the jaws."""
    mover = _FakeMover("gripper_1")
    await mover.pick(_occupied_location("site_a", "plate_1"))

    with pytest.raises(MoverAlreadyHoldingError, match="already holding"):
        await mover.pick(_occupied_location("site_b", "plate_2"))
    assert mover.actuations == ["pick:site_a"], "refused before actuating"
    with pytest.raises(ValueError, match="does not contain labware"):
        await _FakeMover("gripper_2").pick(Location("empty_site"))


async def test_a_refused_pick_pauses_rather_than_aborting_the_run() -> None:
    """`FailurePolicy.ABORT` would otherwise kill the run over a plate a human
    could simply take out of the jaws."""
    mover = _FakeMover("gripper_1")
    await mover.pick(_occupied_location("site_a", "plate_1"))

    with pytest.raises(OverrideWithPauseError):
        await mover.pick(_occupied_location("site_b", "plate_2"))


async def test_place_guards_empty_gripper_and_occupied_target() -> None:
    with pytest.raises(ValueError, match="does not contain labware"):
        await _FakeMover("gripper_1").place(Location("site_b"))

    loaded = _FakeMover("gripper_2")
    await loaded.pick(_occupied_location("site_a", "plate_1"))
    with pytest.raises(ValueError, match="already contains labware"):
        await loaded.place(_occupied_location("site_c", "plate_3"))
    assert loaded.labware is not None


async def test_base_does_not_auto_home() -> None:
    mover = _FakeMover("gripper_1")
    assert mover.actuations == []
    assert mover.in_use is False
    mover.take_external_control()
    assert mover.in_use is True
    mover.release_external_control()
    assert mover.in_use is False
