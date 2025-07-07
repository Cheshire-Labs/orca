import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from orca.driver_management.driver_interfaces import ICentrifuge, IDelidder, IGenericExecutable, IProtocolRunner, IReader, ISealer, IShaker, ITempGettable, ITempSettable
from orca.resource_models.simulation_manager import SimulationManager
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.resource_extras.teachpoints import Teachpoint
from orca.resource_models.transporter import Transporter

orca_logger = logging.getLogger("orca")


class MockBaseDriver:
    def __init__(self, name: str, mocking_type: Optional[str] = None, sim_time: float = 0.2):
        self._name: str = name
        self._mocking_type = mocking_type
        self._is_initialized: bool = False
        self._is_running: bool = False
        self._connected: bool = False
        self._sim_time = sim_time

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def initialize(self) -> None:
        orca_logger.info(f"Initializing {self._name}...")
        self._sleep()
        self._is_initialized = True
        orca_logger.info(f"{self._name} initialized successfully.")

    def _sleep(self) -> None:
        self._is_running = True
        time.sleep(self._sim_time)
        self._is_running = False
        
class MockDriver(MockBaseDriver, IGenericExecutable, IProtocolRunner, ISealer, ITempGettable, ITempSettable, IShaker, ICentrifuge, IReader, IDelidder):
    def __init__(self, name: str, mocking_type: str, sim_time=0.2) -> None:
        super().__init__(name, mocking_type, sim_time=0.2)
        
    async def execute(self, command: str, options: dict[str, Any]) -> None:
        orca_logger.info(f"Executing command: {command} with options: {options}...")
        self._sleep()
        orca_logger.info(f"Command {command} executed successfully.")

    async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
        orca_logger.info(f"Driver: {self._name} preparing for pick...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} prepared for pick")

    async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
        orca_logger.info(f"Driver: {self._name} preparing for place...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} prepared for place")

    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        orca_logger.info(f"Driver: {self._name} notified picked...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} notified picked")

    async def _do_notify_placed(self, labware: LabwareInstance) -> None:
        orca_logger.info(f"Driver: {self._name} notified placed...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} notified placed")

    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        orca_logger.info(f"Running protocol: {protocol_filepath} with params: {params}...")
        self._sleep()
        orca_logger.info(f"Protocol {protocol_filepath} executed successfully.")

    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        orca_logger.info(f"Sealing at {temperature}°C for {duration} seconds...")
        self._sleep()
        orca_logger.info("Sealing completed successfully.")

    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the sealer."""
        orca_logger.info(f"Setting temperature to {temperature}°C.")

    async def get_temperature(self) -> float:
        """Get the current temperature of the sealer."""
        orca_logger.info("Getting current temperature.")
        return 25.0  # Mock temperature value
    
    async def shake(self, duration: int, speed: int) -> None:
        """Shake the device for a specified duration and speed."""
        orca_logger.info(f"Shaking for {duration} seconds at speed {speed}...")
        self._sleep()
        orca_logger.info("Shaking completed successfully.")

    async def centrifuge(self, speed: int, duration: int) -> None:
        """Spin the centrifuge at a specified speed for a specified duration."""
        orca_logger.info(f"Spinning centrifuge for {duration} seconds at speed {speed}...")
        self._sleep()
        orca_logger.info("Centrifuge spin completed successfully.")
    
    async def delid(self) -> None:
        """Delid the specified labware."""
        orca_logger.info(f"Delidding labware...")
        self._sleep()
        orca_logger.info(f"Labware delidded successfully.")

    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        """Read data from the specified protocol and output to a file."""
        orca_logger.info(f"Reading data from {protocol_filepath} and writing to {output_filepath}...")
        self._sleep()
        orca_logger.info(f"Data read successfully from {protocol_filepath} and written to {output_filepath}.")

class MockDevice(Device, IGenericExecutable, IProtocolRunner, ISealer, ITempGettable, ITempSettable, IShaker, ICentrifuge, IReader, IDelidder):
    def __init__(self, name: str, mocking_type: str = "MockDevice", sim_time: float =0.2, sim: bool = False) -> None:
        self._sim_manager = SimulationManager(
            live_driver=MockDriver(name, mocking_type, sim_time),
            sim_driver=MockDriver(name, mocking_type, sim_time),
            sim=sim
        )
        super().__init__(name, self._sim_manager)
    
    @property
    def is_initialized(self) -> bool:
        return self._sim_manager.driver.is_initialized
    
    async def initialize(self) -> None:
        """Initialize the driver."""
        await self._sim_manager.driver.initialize()
        orca_logger.info(f"{self.name} initialized successfully.")

    async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
        """Prepare the device for picking labware."""
        await self._sim_manager.driver._do_prepare_for_pick(labware)

    async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
        """Prepare the device for placing labware."""
        await self._sim_manager.driver._do_prepare_for_place(labware)
    
    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        """Notify the device that labware has been picked up."""
        await self._sim_manager.driver._do_notify_picked(labware)

    async def _do_notify_placed(self, labware: LabwareInstance) -> None:
        """Notify the device that labware has been placed."""
        await self._sim_manager.driver._do_notify_placed(labware)

    async def execute(self, command: str, options: dict[str, Any]) -> None:
        """Execute a command with the driver."""
        await self._sim_manager.driver.execute(command, options)

    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        """Run a protocol with the driver."""
        await self._sim_manager.driver.run_protocol(protocol_filepath, params)

    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        await self._sim_manager.driver.seal(temperature, duration)

    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the device."""
        await self._sim_manager.driver.set_temperature(temperature)

    async def get_temperature(self) -> float:
        """Get the current temperature of the device."""
        return await self._sim_manager.driver.get_temperature()
        
    async def shake(self, duration: int, speed: int) -> None:
        """Shake the device for a specified duration and speed."""
        await self._sim_manager.driver.shake(duration, speed)

    async def centrifuge(self, speed: int, duration: int) -> None:
        """Spin the centrifuge at a specified speed for a specified duration."""
        await self._sim_manager.driver.centrifuge(duration, speed)

    async def delid(self) -> None:
        """Delid the specified labware."""
        await self._sim_manager.driver.delid()

    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        """Read data from the specified protocol and output to a file."""
        await self._sim_manager.driver.read(protocol_filepath, output_filepath)


class MockTransporterDriver(MockBaseDriver):
    """ A simulation driver for a robotic arm that can pick and place labware."""
    def __init__(self, 
                 name: str, 
                 mocking_type: Optional[str] = None, 
                 teachpoints: str | List[str] | None = None, 
                 sim_time: float = 0.2
                 ) -> None:
        """ Initializes the SimulationRoboticArmDriver with a name, mocking type, and optional teachpoints.
        Args:
            name (str): The name of the driver.
            mocking_type (Optional[str]): The type of equipment this simulation is mocking, e.g., "robotic_arm".
            teachpoints (str | List[str] | None): The teachpoints to use for the robotic arm, can be a file path or a list of position names.
            sim_time (float): The time to simulate for each operation, default is 0.2 seconds.
        """
        super().__init__(name, mocking_type, sim_time)
        self._positions: List[str] = []
        self.set_teachpoints(teachpoints if teachpoints is not None else [])


    async def initialize(self) -> None:
        self._sleep()
        self._is_initialized = True

    async def pick(self, teachpoint_name: str, labware_type: str) -> None:
        self._validate_position(teachpoint_name)
        self._sleep()

    async def place(self, teachpoint_name: str, labware_type: str) -> None:
        self._validate_position(teachpoint_name)
        self._sleep()

    def _validate_position(self, position_name: str) -> None:
        if position_name not in self._positions:
            raise ValueError(f"The position '{position_name}' is not taught for {self._name}")

    def get_taught_positions(self) -> List[str]:
        return self._positions
    
    def set_teachpoints(self, teachpoints: str | List[str]) -> None:
        if isinstance(teachpoints, str):
            self._positions = self._load_taught_positions(teachpoints)
        elif isinstance(teachpoints, list):
            self._positions = teachpoints

    def _load_taught_positions(self, filepath: str) -> List[str]:
        return [t.name for t in Teachpoint.load_teachpoints_from_file(filepath)]
  

class MockTransporter(Transporter):
    def __init__(self, name: str, mocking_type: str = "MockTransporter", teachpoints: str | List[str] | None = None, sim_time: float = 0.2, sim: bool = False) -> None:
        self._name = name
        self._sim_manager = SimulationManager(
            live_driver=MockTransporterDriver(name, mocking_type, teachpoints, sim_time),
            sim_driver=MockTransporterDriver(name, mocking_type, teachpoints, sim_time),
            sim=sim
        )
        self._lock = asyncio.Lock()
        super().__init__(name, self._sim_manager)
        self._is_initialized = False

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def in_use(self) -> bool:
        return self._lock.locked()
    
    async def initialize(self) -> None:
        async with self._lock:
            await self._sim_manager.driver.initialize()
            self._is_initialized = True

    def get_taught_positions(self) -> List[str]:
        """Get the taught positions of the transporter."""
        return self._sim_manager.driver.get_taught_positions()

    async def _do_pick(self, teachpoint_name: str, labware_type: str) -> None:
        return await self._sim_manager.driver.pick(teachpoint_name, labware_type)
    
    async def _do_place(self, teachpoint_name: str, labware_type: str) -> None:
        return await self._sim_manager.driver.place(teachpoint_name, labware_type)

