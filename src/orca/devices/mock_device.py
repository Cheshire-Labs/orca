from orca.devices.device_interfaces import ICentrifuge, IDelidder, IGenericExecutable, IProtocolRunner, IReader, ISealer, IShaker, ITempGettable, ITempSettable
from orca.driver_management.drivers.mock import orca_logger
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.simulation_manager import SimulationManager


from typing import Any, Dict, Union

# class MockDevice(Device, IGenericExecutable, IProtocolRunner, ISealer, ITempGettable, ITempSettable, IShaker, ICentrifuge, IReader, IDelidder):
#     def __init__(self, name: str, driver: MockDriver, sim: bool = False) -> None:
#         self._sim_manager = SimulationManager(
#             live_driver=driver,
#             sim_driver=MockDriver(name, "generic_device"),
#             sim=sim
#         )
#         super().__init__(name, self._sim_manager)

#     @property
#     def is_initialized(self) -> bool:
#         return self._sim_manager.driver.is_initialized

#     async def initialize(self) -> None:
#         """Initialize the driver."""
#         await self._sim_manager.driver.initialize()
#         orca_logger.info(f"{self.name} initialized successfully.")

#     async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
#         """Prepare the device for picking labware."""
#         await self._sim_manager.driver._do_prepare_for_pick(labware)

#     async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
#         """Prepare the device for placing labware."""
#         await self._sim_manager.driver._do_prepare_for_place(labware)

#     async def _do_notify_picked(self, labware: LabwareInstance) -> None:
#         """Notify the device that labware has been picked up."""
#         await self._sim_manager.driver._do_notify_picked(labware)

#     async def _do_notify_placed(self, labware: LabwareInstance) -> None:
#         """Notify the device that labware has been placed."""
#         await self._sim_manager.driver._do_notify_placed(labware)

#     async def execute(self, command: str, options: dict[str, Any]) -> None:
#         """Execute a command with the driver."""
#         await self._sim_manager.driver.execute(command, options)

#     async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
#         """Run a protocol with the driver."""
#         await self._sim_manager.driver.run_protocol(protocol_filepath, params)

#     async def seal(self, temperature: int, duration: float) -> None:
#         """Seal the plate at a specified temperature and duration."""
#         await self._sim_manager.driver.seal(temperature, duration)

#     async def set_temperature(self, temperature: float) -> None:
#         """Set the temperature of the device."""
#         await self._sim_manager.driver.set_temperature(temperature)

#     async def get_temperature(self) -> float:
#         """Get the current temperature of the device."""
#         return await self._sim_manager.driver.get_temperature()

#     async def shake(self, duration: int, speed: int) -> None:
#         """Shake the device for a specified duration and speed."""
#         await self._sim_manager.driver.shake(duration, speed)

#     async def centrifuge(self, speed: int, duration: int) -> None:
#         """Spin the centrifuge at a specified speed for a specified duration."""
#         await self._sim_manager.driver.centrifuge(duration, speed)

#     async def delid(self) -> None:
#         """Delid the specified labware."""
#         await self._sim_manager.driver.delid()

#     async def read(self, protocol_filepath: str, output_filepath: str) -> None:
#         """Read data from the specified protocol and output to a file."""
#         await self._sim_manager.driver.read(protocol_filepath, output_filepath)