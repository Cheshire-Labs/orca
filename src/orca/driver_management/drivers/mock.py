from abc import ABC
import asyncio
import logging
from typing import Any, Dict, List, Optional

from orca.devices.device_interfaces import ICentrifuge, IDelidder, IGenericExecutable, IProtocolRunner, IReader, ISealer, IShaker, ITempGettable, ITempSettable
from orca.driver_management.drivers.driver_interfaces import ITransporterDriver
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.resource_extras.teachpoints import Teachpoint

orca_logger = logging.getLogger("orca")


# class MockBaseDriver:
#     def __init__(self, name: str, mocking_type: Optional[str] = None, sim_strategy: Optional[SimStrategy] = None) -> None:
#         self._name: str = name
#         self._mocking_type = mocking_type
#         self._is_initialized: bool = False
#         self._is_running: bool = False
#         self._connected: bool = False
#         self._waiter = sim_strategy if sim_strategy is not None else SleepSim()

#     @property
#     def name(self) -> str:
#         return self._name

#     @property
#     def is_initialized(self) -> bool:
#         return self._is_initialized

#     @property
#     def is_running(self) -> bool:
#         return self._is_running

#     @property
#     def is_connected(self) -> bool:
#         return self._connected

#     async def connect(self) -> None:
#         self._connected = True

#     async def disconnect(self) -> None:
#         self._connected = False

#     async def initialize(self) -> None:
#         await self._sim(f"Initialization of {self._name} in progress...")
#         self._is_initialized = True
#         orca_logger.info(f"{self._name} initialized successfully.")

#     async def _sim(self, message: str) -> None:
#         self._is_running = True
#         await self._waiter.sim(message)
#         self._is_running = False
        
# class MockDriver(MockBaseDriver):
#     def __init__(self, name: str, mocking_type: str, sim_strategy: Optional[SimStrategy] = None) -> None:
#         super().__init__(name, mocking_type, sim_strategy)

#     async def open(self) -> None:
#         await self._sim(f"Opening {self._name}...")
#         orca_logger.info(f"{self._name} opened successfully.")

#     async def close(self) -> None:
#         await self._sim(f"Closing {self._name}...")
#         orca_logger.info(f"{self._name} closed successfully.")
        
#     async def execute(self, command: str, options: dict[str, Any]) -> None:
#         await self._sim(f"Executing command: {command} with options: {options}...")
#         orca_logger.info(f"Command {command} executed successfully.")

#     async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
#         await self._sim(f"Driver: {self._name} preparing for pick...")
#         orca_logger.info(f"Driver: {self._name} prepared for pick")

#     async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
#         await self._sim(f"Driver: {self._name} preparing for place...")
#         orca_logger.info(f"Driver: {self._name} prepared for place")

#     async def _do_notify_picked(self, labware: LabwareInstance) -> None:
#         await self._sim(f"Driver: {self._name} notified picked...")
#         orca_logger.info(f"Driver: {self._name} notified picked")

#     async def _do_notify_placed(self, labware: LabwareInstance) -> None:
#         await self._sim(f"Driver: {self._name} notified placed...")
#         orca_logger.info(f"Driver: {self._name} notified placed")

#     async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
#         await self._sim(f"Running protocol: {protocol_filepath} with params: {params}...")
#         orca_logger.info(f"Protocol {protocol_filepath} executed successfully.")

#     async def seal(self, temperature: int, duration: float) -> None:
#         """Seal the plate at a specified temperature and duration."""
#         await self._sim(f"Sealing at {temperature}°C for {duration} seconds...")
#         orca_logger.info("Sealing completed successfully.")

#     async def set_temperature(self, temperature: float) -> None:
#         """Set the temperature of the sealer."""
#         orca_logger.info(f"Setting temperature to {temperature}°C.")

#     async def get_temperature(self) -> float:
#         """Get the current temperature of the sealer."""
#         orca_logger.info("Getting current temperature.")
#         return 25.0  # Mock temperature value
    
#     async def stop(self) -> None:
#         """Stop the shaker backend."""
#         ...

#     @property
#     def supports_locking(self) -> bool:
#         """Check if the shaker supports locking the plate"""
#         ...

#     async def unlock_plate(self) -> None:
#         """Unlock the plate"""
#         ...

#     async def lock_plate(self):
#         """Lock the plate"""
#         ...

#     async def stop_shaking(self) -> None:
#         """Stop shaking"""
#         ...
    
#     async def shake(self, duration: float, speed: float) -> None:
#         """Shake the device for a specified duration and speed."""
#         await self._sim(f"Shaking at speed {speed}...")
#         orca_logger.info("Shaking completed successfully.")

#     async def centrifuge(self, speed: int, duration: int) -> None:
#         """Spin the centrifuge at a specified speed for a specified duration."""
#         await self._sim(f"Spinning centrifuge for {duration} seconds at speed {speed}...")
#         orca_logger.info("Centrifuge spin completed successfully.")
    
#     async def delid(self) -> None:
#         """Delid the specified labware."""
#         await self._sim(f"Delidding labware...")
#         orca_logger.info(f"Labware delidded successfully.")

#     async def read(self, protocol_filepath: str, output_filepath: str) -> None:
#         """Read data from the specified protocol and output to a file."""
#         await self._sim(f"Reading data from {protocol_filepath} and writing to {output_filepath}...")
#         orca_logger.info(f"Data read successfully from {protocol_filepath} and written to {output_filepath}.")

# class MockTransporterDriver(MockBaseDriver, ITransporterDriver):
#     """ A simulation driver for a robotic arm that can pick and place labware."""
#     def __init__(self, 
#                  name: str, 
#                  mocking_type: Optional[str] = None, 
#                  await_strategy: Optional[SimStrategy] = None,
#                  ) -> None:
#         """ Initializes the SimulationRoboticArmDriver with a name, mocking type, and optional teachpoints.
#         Args:
#             name (str): The name of the driver.
#             mocking_type (Optional[str]): The type of equipment this simulation is mocking, e.g., "robotic_arm".
#             teachpoints (str | List[str] | None): The teachpoints to use for the robotic arm, can be a file path or a list of position names.
#             sim_time (float): The time to simulate for each operation, default is 0.2 seconds.
#         """
#         super().__init__(name, mocking_type, await_strategy)
#         self._positions = []

#     async def pick(self, position_name: str, labware_type: str) -> None:
#         self._validate_position(position_name)
#         await self._sim(f"Driver: {self._name} picking from {position_name}, labware type: {labware_type} picking...")
#         orca_logger.info(f"Driver: {self._name} picked from {position_name}, labware type: {labware_type} picked")

#     async def place(self, position_name: str, labware_type: str) -> None:
#         self._validate_position(position_name)
#         await self._sim(f"Driver: {self._name} placing to {position_name}, labware type: {labware_type} placing...")
#         orca_logger.info(f"Driver: {self._name} placed to {position_name}, labware type: {labware_type} placed")

#     def _validate_position(self, position_name: str) -> None:
#         if position_name not in self._positions:
#             raise ValueError(f"The position '{position_name}' is not taught for {self._name}")

#     def get_taught_positions(self) -> List[str]:
#         return [t.name for t in self._positions]

#     def load_positions(self, positions: List[Teachpoint]) -> None:
#         self._positions = positions


