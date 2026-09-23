"""MoveDefaultsFacade: the external surface over what an arm's moves start from.

REST/MCP/CLI route through `deployment_registries.move_defaults.<method>`. The
store instance the runtime resolves moves against IS the one this facade edits,
so an operator's change reaches the next move rather than a second copy of the
numbers.

Reads never write. A transporter nobody has tuned reports the seed and says so,
which keeps "this deployment chose 25" apart from "nobody has ever looked at
this number" -- a distinction seeding a row on first read would destroy.

Not everything in the record is editable here. The four fields describing how a
position is entered and left belong to the access config, and an edit naming one
is refused rather than stored where no move would read it.
"""

from collections.abc import Iterable

from cheshire_drivers.move_parameters import MoveParameterField, MoveParameterPatch

from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.move_defaults_service import MoveDefaultsService
from orca.runtime.move_parameters import (
    reject_contradiction,
    reject_labware_owned,
    reject_site_owned,
    resolve_move_defaults,
)
from orca.runtime.move_parameter_models import TransporterMoveDefaults
from orca.runtime.runtime_interface import IMoveDefaultsFacade
from orca.system.resource_registry import IResourceRegistry


class MoveDefaultsFacade(IMoveDefaultsFacade):
    """Concrete IMoveDefaultsFacade implementation."""

    def __init__(self, service: MoveDefaultsService) -> None:
        self._service = service

    async def get(self, transporter_name: str) -> TransporterMoveDefaults:
        stored = await self._service.get(transporter_name) or MoveParameterPatch()
        return _record(transporter_name, stored)

    async def list(
        self, system: IResourceRegistry | None = None,
    ) -> list[TransporterMoveDefaults]:
        """Every arm's record: the tuned ones, plus the rest when a system is up.

        A deployment with nothing mounted can still be edited, and then only the
        tuned arms have names anybody knows. Once a system is mounted its arms
        belong in the list even untouched, or an operator reading the list would
        conclude an arm they have not tuned yet does not exist.
        """
        stored = await self._service.list()
        names = list(stored)
        if system is not None:
            names += [t.name for t in system.transporters if t.name not in stored]
        return [
            _record(name, stored.get(name) or MoveParameterPatch())
            for name in sorted(names)
        ]

    @dangerous(
        name="move_defaults.apply",
        level=DangerLevel.PHYSICAL,
        message="Change how '{transporter_name}' picks and places. Every move it "
                "makes from now on uses these numbers, including moves already "
                "queued. A clearance or grip width that does not match the "
                "hardware crashes the arm into the deck.",
    )
    async def apply(
        self,
        transporter_name: str,
        patch: MoveParameterPatch,
        clear: Iterable[MoveParameterField] = (),
    ) -> TransporterMoveDefaults:
        clear = tuple(clear)
        reject_site_owned(patch, clear)
        reject_labware_owned(patch, clear)
        reject_contradiction(patch, clear)
        stored = await self._service.apply(transporter_name, patch, clear)
        return _record(transporter_name, stored)

    @dangerous(
        name="move_defaults.reset",
        level=DangerLevel.PHYSICAL,
        message="Discard every number tuned for '{transporter_name}' and put it "
                "back on the built-in seed. Whatever the arm was calibrated to "
                "is gone; the seed is one machine's measurements, not this one's.",
    )
    async def reset(self, transporter_name: str) -> bool:
        return await self._service.delete(transporter_name)


def _record(
    transporter_name: str, stored: MoveParameterPatch,
) -> TransporterMoveDefaults:
    resolved = resolve_move_defaults(stored)
    return TransporterMoveDefaults(
        transporter_name=transporter_name,
        parameters=resolved.parameters,
        sources=resolved.sources,
    )
