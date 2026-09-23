from typing import List

from cheshire_drivers.liquid_handler_models import MovePlateRequest
from cheshire_drivers.teachpoints import Teachpoint
from orca.devices.devices import LiquidHandler
from orca.resource_models.location import Location
from orca.resource_models.transporter_base import TransporterBase


def _driver_site(position_id: str) -> str:
    """'flex/C2-slot' -> 'C2-slot': the driver names sites without the owner."""
    return position_id.split("/", 1)[1] if "/" in position_id else position_id


class DeckGripperTransporter(TransporterBase):
    """The liquid handler's on-deck gripper as a first-class plate mover:
    routed, reserved, journey-visible like any transporter, with
    actuation delegated to the paired device's driver."""

    def __init__(self, device: LiquidHandler) -> None:
        super().__init__(f"{device.name}/gripper")
        self._device = device

    @property
    def device(self) -> LiquidHandler:
        return self._device

    @property
    def pick_moves_the_plate(self) -> bool:
        """``move_plate`` carries the plate the whole way in one call, and that
        call is the place. Until it returns the plate is still on its slot."""
        return False

    async def initialize(self) -> None:
        """Nothing to bring up: the paired device owns physical initialization."""
        return

    @property
    def is_initialized(self) -> bool:
        return True

    async def get_teachpoints(self) -> List[Teachpoint]:
        """No coordinate teachpoints: site edges are wired at build."""
        return []

    @property
    def under_external_control(self) -> bool:
        """The paired device's interlock covers its own gripper, and the
        gripper can also be held in its own right."""
        return (
            self._device.under_external_control
            or self._under_external_control
            or self._external_control_hold is not None
        )

    async def _do_pick(self, location: Location) -> None:
        """Nothing to actuate: the base notes the source and ``_do_place``
        issues the one call that carries the plate the whole way."""
        return

    async def _do_place(self, location: Location) -> None:
        held = self.labware
        picked_from = self.picked_from_position_id
        if held is None or picked_from is None:
            raise ValueError(
                f"{self} cannot place: holding {held}, picked from "
                f"{picked_from!r}. Both are required to issue move_plate."
            )
        grip = await self._grip_distance_from_top(held.labware_type)
        # Same lock the action dispatcher and reconcile hold, so the gripper
        # cannot drive the device's driver concurrently with either.
        async with self._device.lock.held_for("move_plate"):
            await self._device.driver.move_plate(MovePlateRequest(
                plate=held.name,
                from_position=_driver_site(picked_from),
                to_position=_driver_site(location.position_id),
                grip_distance_from_top=grip,
            ))

    async def _grip_distance_from_top(self, labware_type: str) -> float | None:
        """How far below its top this labware is gripped, if anyone has said.

        None leaves the height to the robot's own labware definition, and a
        definition that states none leaves an Opentrons Flex gripping at the
        labware's mid-height. That is about 5 mm too low on a standard
        flat-bottom microplate, so a plate that is not held where it should be
        wants a grip profile, not a change here.
        """
        profile = await self.grip_profile(labware_type)
        return profile.grip_distance_from_top if profile is not None else None
