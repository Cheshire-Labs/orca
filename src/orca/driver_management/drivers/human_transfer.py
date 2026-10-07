from cheshire_drivers.teachpoints import Teachpoint

from orca.resource_models.transporter import Transporter
from orca.runtime.interfaces import ITeachpointStore
from orca.runtime.teachpoint_service import seeded_teachpoint_service


class HumanTransfer(Transporter):
    """A person who picks and places labware, prompted by orca-client's `human` driver."""

    def __init__(
        self,
        name: str,
        teachpoints: str | list[Teachpoint] | ITeachpointStore | None = None,
    ) -> None:
        if isinstance(teachpoints, str):
            store: ITeachpointStore = seeded_teachpoint_service(
                Teachpoint.load_teachpoints_from_file(teachpoints)
            )
        elif isinstance(teachpoints, list):
            store = seeded_teachpoint_service(teachpoints)
        elif teachpoints is None:
            store = seeded_teachpoint_service()
        else:
            store = teachpoints
        super().__init__(name, teachpoint_store=store)
