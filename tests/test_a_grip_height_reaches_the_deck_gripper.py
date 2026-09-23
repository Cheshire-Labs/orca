"""What a labware's grip profile says reaches the handler's own gripper.

Nothing set `grip_distance_from_top` on a Flex gripper move, at any call
site. Every move took the driver default, which for a
synthesized Opentrons definition is the labware's MID-HEIGHT. Opentrons' own
definition for the plate on the bench puts the grip 12.2 mm up a 14.22 mm plate;
mid-height is 7.11. Five millimetres too low, on every gripper move ever made.

The height itself is a measurement, per labware type, and belongs in a grip
profile. What these pin is that the profile reaches the move.
"""

from typing import Optional, cast

import pytest
from cheshire_drivers.liquid_handler_models import MovePlateRequest
from cheshire_drivers.move_parameters import MoveParameterPatch

from orca.devices.deck_gripper_transporter import DeckGripperTransporter
from orca.devices.devices import LiquidHandler
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.tracked_lock import TrackedLock

PLATE_TYPE = "Cor_96_wellplate_360ul_Fb"

# Opentrons' own corning_96_wellplate_360ul_flat: 14.22 mm tall, gripped at
# 12.2 mm from the bottom.
CORNING_BELOW_TOP = 2.02


def _as_handler(fake: "_FakeHandler") -> LiquidHandler:
    """The fake, typed as what the gripper takes.

    One cast in one place instead of a type-ignore at each construction: the
    gripper reaches only `name`, `driver` and `lock`, and `_FakeHandler` is
    exactly those three.
    """
    return cast(LiquidHandler, fake)


class _RecordingProfiles:
    """The whole IGripProfileStore, holding whatever a test put in it.

    Every member, not just the one the gripper calls: a partial double lets the
    Protocol grow a method without any test noticing, and it is what forced the
    casts this file used to carry.
    """

    def __init__(self, profiles: Optional[dict[str, MoveParameterPatch]] = None) -> None:
        self._profiles = dict(profiles or {})
        self.asked_for: list[str] = []

    async def get(self, labware_type: str) -> MoveParameterPatch | None:
        self.asked_for.append(labware_type)
        return self._profiles.get(labware_type)

    async def list(self) -> dict[str, MoveParameterPatch]:
        return dict(self._profiles)

    async def set(self, labware_type: str, patch: MoveParameterPatch) -> None:
        self._profiles[labware_type] = patch

    async def delete(self, labware_type: str) -> bool:
        return self._profiles.pop(labware_type, None) is not None

    async def create_schema(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _RecordingDriver:
    def __init__(self) -> None:
        self.moves: list[MovePlateRequest] = []

    async def move_plate(self, request: MovePlateRequest) -> None:
        self.moves.append(request)


class _FakeHandler:
    """Only the two members the deck gripper touches on a place."""

    def __init__(self) -> None:
        self.name = "flex_1"
        self.driver = _RecordingDriver()
        self.lock = TrackedLock("lh device lock")


async def _gripper_holding(
    profiles: _RecordingProfiles, labware_type: str = PLATE_TYPE,
) -> tuple[DeckGripperTransporter, _FakeHandler]:
    """A deck gripper that has picked, reached through the public surface.

    `pick` is what writes where the plate came from, so going through it means
    the fixture cannot drift from what a real move leaves behind.
    """
    handler = _FakeHandler()
    gripper = DeckGripperTransporter(_as_handler(handler))
    gripper.bind_grip_profiles(profiles)
    plate = LabwareInstance("r6_source", labware_type)
    source = Location("flex_1/B4-slot", PlatePad("flex_1/B4-slot"))
    source.initialize_labware(plate)
    await gripper.pick(source)
    # The deck gripper's pick actuates nothing, so it moves no plate and
    # records none. In a real move the placer writes it into the jaws.
    gripper.gripper_location.initialize_labware(plate)
    return gripper, handler


def _target() -> Location:
    return Location("flex_1/C2-slot", PlatePad("flex_1/C2-slot"))


@pytest.mark.asyncio
async def test_a_profiles_grip_height_rides_the_move() -> None:
    profiles = _RecordingProfiles(
        {PLATE_TYPE: MoveParameterPatch(grip_distance_from_top=CORNING_BELOW_TOP)}
    )
    gripper, handler = await _gripper_holding(profiles)

    await gripper._do_place(_target())

    assert handler.driver.moves[0].grip_distance_from_top == CORNING_BELOW_TOP


@pytest.mark.asyncio
async def test_the_profile_is_looked_up_by_the_labware_being_carried() -> None:
    profiles = _RecordingProfiles()
    gripper, _ = await _gripper_holding(
        profiles, labware_type="nest_96_wellplate_2ml_deep",
    )

    await gripper._do_place(_target())

    assert profiles.asked_for == ["nest_96_wellplate_2ml_deep"]


@pytest.mark.asyncio
async def test_no_profile_leaves_the_height_to_the_definition() -> None:
    """None is a value: it means nobody has measured this labware, so the
    robot's own definition decides. It is NOT a number this layer invents."""
    gripper, handler = await _gripper_holding(_RecordingProfiles())

    await gripper._do_place(_target())

    assert handler.driver.moves[0].grip_distance_from_top is None


@pytest.mark.asyncio
async def test_a_profile_about_other_things_does_not_invent_a_height() -> None:
    profiles = _RecordingProfiles(
        {PLATE_TYPE: MoveParameterPatch(resource_width=85.0)}
    )
    gripper, handler = await _gripper_holding(profiles)

    await gripper._do_place(_target())

    assert handler.driver.moves[0].grip_distance_from_top is None


@pytest.mark.asyncio
async def test_an_unbound_store_still_moves() -> None:
    """A deployment that never mentions grip profiles keeps working."""
    handler = _FakeHandler()
    gripper = DeckGripperTransporter(_as_handler(handler))
    plate = LabwareInstance("r6_source", PLATE_TYPE)
    source = Location("flex_1/B4-slot", PlatePad("flex_1/B4-slot"))
    source.initialize_labware(plate)
    await gripper.pick(source)
    gripper.gripper_location.initialize_labware(plate)

    await gripper._do_place(_target())

    assert handler.driver.moves[0].grip_distance_from_top is None
