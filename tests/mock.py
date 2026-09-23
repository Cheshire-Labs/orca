from typing import Any, ClassVar, Dict, List, Optional
import logging
from unittest.mock import MagicMock
from cheshire_drivers import (
    BaseSimDriver,
    ShakerSimMixin, SealerSimMixin, TempSettableSimMixin, TempGettableSimMixin,
    CentrifugeSimMixin, ReaderSimMixin, DelidderSimMixin, ProtocolRunnerSimMixin,
)
from cheshire_drivers.centrifuge_models import CentrifugeRequest
from cheshire_drivers.delidder_models import DelidRequest
from cheshire_drivers.protocol_runner_models import RunProtocolRequest
from cheshire_drivers.reader_models import ReadRequest
from cheshire_drivers.sealer_models import SealRequest
from cheshire_drivers.shaker_models import ShakeRequest
from orca.resource_models.devices import Device
from orca.resource_models.location import Location
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.runtime.run_modes import WorkflowRunMode
from orca.devices.device_interfaces import (
    IGenericExecutable, IProtocolRunner, ISealer,
    IShaker, ICentrifuge, IReader, IDelidder
)

orca_logger = logging.getLogger("orca")


class StubMover(IPlateMover):
    """Stands in for the acting mover where a test drives placement hooks
    directly. Deliberately NOT any device's own gripper, so device hooks take
    the external-arm path and emit their full driver sequence."""

    def __init__(self, name: str = "stub_mover") -> None:
        self._name = name
        self._labware: Optional[LabwareInstance] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._labware

    @property
    def gripper_position_id(self) -> str:
        return f"{self._name}/gripper"


EXTERNAL_MOVER = StubMover()

# The full capability contract UniversalSimDriver / UniversalMockDevice
# advertise. Connection-card test helpers reference this so a simulated
# live connection's advertised interfaces match what the universal-mock
# topology device declares (the connect-time superset check compares the two).
UNIVERSAL_MOCK_INTERFACES: frozenset[str] = frozenset({
    "IShaker", "ISealer", "ITempSettable", "ITempGettable",
    "ICentrifuge", "IReader", "IDelidder", "IProtocolRunner",
})

# What a connected arm advertises. An arm homes, and homing is its own
# capability rather than something ITransporter implies, so a connection card
# that names only ITransporter is not a superset of what an arm declares and
# the connect-time check refuses it.
TRANSPORTER_MOCK_INTERFACES: frozenset[str] = frozenset({
    "ITransporter", "IHomeable",
})


class UniversalSimDriver(
    BaseSimDriver, ShakerSimMixin, SealerSimMixin, TempSettableSimMixin,
    TempGettableSimMixin, CentrifugeSimMixin, ReaderSimMixin, DelidderSimMixin,
    ProtocolRunnerSimMixin,
):
    """Universal simulation driver for testing - supports ALL action interfaces.

    `interfaces` is declared explicitly as the union of every mixin's
    contract. Without this override Python's MRO surfaces only the FIRST
    mixin's `interfaces` ClassVar ({"IShaker"}), so the driver would
    under-advertise its real capability set. The DeviceFacade derives the
    operator-invoke surface from this ClassVar, so the override keeps the
    multi-capability tests that depend on this driver honest.
    """

    interfaces: ClassVar[frozenset[str]] = UNIVERSAL_MOCK_INTERFACES

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        """Execute a generic command"""
        await self._sim(f"Executing command: {command} with options: {options}")
        orca_logger.info(f"Command {command} executed successfully.")

    async def open(self) -> None:
        """Override to use BaseSimDriver's version with self.name"""
        await BaseSimDriver.open(self)

    async def close(self) -> None:
        """Override to use BaseSimDriver's version with self.name"""
        await BaseSimDriver.close(self)


class UniversalMockDevice(Device, IGenericExecutable, IProtocolRunner, ISealer, IShaker, ICentrifuge, IReader, IDelidder):
    """Universal mock device for testing - supports ALL action types"""

    KIND: ClassVar[str] = "mock"

    def __init__(
        self,
        name: str,
        sim_override: WorkflowRunMode | None = None,
        site_names: list[str] | None = None,
        driver: UniversalSimDriver | None = None,
        sim_driver: UniversalSimDriver | None = None,
    ) -> None:
        driver = driver or UniversalSimDriver(name)
        super().__init__(
            name, driver, sim_driver or driver,
            sim_override=sim_override, site_names=site_names,
        )

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        await self.driver.execute(command, options)

    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        await self.driver.run_protocol(
            RunProtocolRequest(protocol_filepath=protocol_filepath, params=params)
        )

    async def seal(self, temperature: int, duration: float) -> None:
        await self.driver.seal(SealRequest(temperature=temperature, duration=duration))

    async def shake(self, duration: int, speed: int) -> None:
        await self.driver.shake(ShakeRequest(speed=float(speed), duration=float(duration)))

    async def centrifuge(self, g: int, duration: int) -> None:
        await self.driver.centrifuge(
            CentrifugeRequest(g=float(g), duration=float(duration))
        )

    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        await self.driver.read(
            ReadRequest(protocol_filepath=protocol_filepath, output_filepath=output_filepath)
        )

    async def delid(self) -> None:
        await self.driver.delid(DelidRequest())


