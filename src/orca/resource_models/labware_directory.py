"""Turns a ledger answer back into the labware object the caller wanted.

Not a physical fact, which is why it does not live with them: the ledger speaks
refs so it can stay free of the topology, and something on this side has to
resolve one. Putting it in `orca/state/` forced a structural protocol on the
ledger's callers and cost them the concrete type they actually use.
"""

from orca.resource_models.labware import LabwareInstance
from orca.state.identity import LabwareRef


class LabwareDirectory:
    def __init__(self) -> None:
        self._by_id: dict[str, LabwareInstance] = {}

    def remember(self, labware: LabwareInstance) -> None:
        self._by_id[labware.id] = labware

    def resolve(self, ref: LabwareRef) -> LabwareInstance:
        found = self._by_id.get(ref.id)
        if found is None:
            raise KeyError(
                f"the ledger holds a placement for {ref.name!r} but no labware "
                f"of that id is registered"
            )
        return found

    def forget(self, labware: LabwareInstance) -> None:
        self._by_id.pop(labware.id, None)


_directory = LabwareDirectory()


def labware_directory() -> LabwareDirectory:
    return _directory


def reset_labware_directory() -> None:
    """Empty it. A system build owns its world; nothing outlives one."""
    global _directory
    _directory = LabwareDirectory()
