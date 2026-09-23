"""GripProfileFacade: the external surface over how each labware type is held.

REST/MCP/CLI route through `deployment_registries.grip_profiles.<method>`. The
store instance the runtime resolves moves against IS the one this facade edits,
so a width someone corrects on the bench reaches the next pick rather than a
second copy of the numbers.

Reads never write. A type nobody has measured reports an empty profile and stays
that way until somebody edits it, which is what keeps "this deck grips a Costar
at 76" apart from "nobody has said anything about a Costar".
"""

from collections.abc import Iterable

from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch

from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.grip_profile_service import GripProfileService
from orca.runtime.move_parameters import reject_contradiction, reject_site_owned
from orca.runtime.move_parameter_models import LabwareGripProfile
from orca.runtime.runtime_interface import IGripProfileFacade


class GripProfileFacade(IGripProfileFacade):
    """Concrete IGripProfileFacade implementation."""

    def __init__(self, service: GripProfileService) -> None:
        self._service = service

    async def get(self, labware_type: str) -> LabwareGripProfile:
        stored = await self._service.get(labware_type) or MoveParameterPatch()
        return LabwareGripProfile(labware_type=labware_type, patch=stored)

    async def list(self) -> list[LabwareGripProfile]:
        """Every type somebody has measured.

        Unlike the arms, this does not fill in the untouched ones from a mounted
        system: a deployment's catalog runs to hundreds of labware types, and a
        list where all but three rows say nothing hides the three that matter.
        """
        stored = await self._service.list()
        return [
            LabwareGripProfile(labware_type=labware_type, patch=patch)
            for labware_type, patch in sorted(stored.items())
        ]

    @dangerous(
        name="grip_profiles.apply",
        level=DangerLevel.PHYSICAL,
        message="Change how every arm holds '{labware_type}'. Each pick and "
                "place of this labware from now on uses these numbers, including "
                "moves already queued. A grip width that does not match the "
                "labware drops it, and a grip height that does not crushes it "
                "against the nest.",
    )
    async def apply(
        self,
        labware_type: str,
        patch: MoveParameterPatch,
        clear: Iterable[MoveParameterField] = (),
    ) -> LabwareGripProfile:
        clear = tuple(clear)
        reject_site_owned(patch, clear)
        reject_contradiction(patch, clear)
        merged = await self._service.apply(labware_type, patch, clear)
        return LabwareGripProfile(labware_type=labware_type, patch=merged)

    @dangerous(
        name="grip_profiles.reset",
        level=DangerLevel.PHYSICAL,
        message="Discard everything measured for '{labware_type}' and hold it "
                "the way the arm holds anything else. Whatever this labware was "
                "calibrated to is gone.",
    )
    async def reset(self, labware_type: str) -> bool:
        return await self._service.delete(labware_type)
