from cheshire_drivers.human_transporter_driver import HumanTransporterDriver
from cheshire_drivers.teachpoints import Teachpoint

from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.interfaces import ITeachpointStore
from orca.runtime.teachpoint_service import seeded_teachpoint_service


class _HumanTransporterFactory:
    """Single-device factory that returns HumanTransporterDriver in both slots.

    HumanTransfer wires its transporter through this factory so the no-driver
    Transporter ctor resolves the (live, sim) pair to the same human driver
    instance (humans don't have a separate sim).
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def build_drivers(
        self, device_type: str, name: str,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        # Single shared instance covers both slots; the SimulationManager
        # toggle is meaningless for a human-in-the-loop transporter.
        driver = HumanTransporterDriver(self._name)
        return driver, driver


class HumanTransfer(Transporter):
    """A transporter that requires human interaction to pick and place labware."""

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
        # Bind a one-shot factory so the no-driver Transporter ctor resolves
        # both (live, sim) slots to the single human driver instance.
        with use_device_factory(_HumanTransporterFactory(name)):
            super().__init__(name, teachpoint_store=store)
