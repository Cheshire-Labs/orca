from orca.devices.device_interfaces import IGenericExecutable, IProtocolRunner
from orca.driver_management.drivers.venus.venus_driver import SimulationVenusProtocolDriver, VenusProtocolDriver
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.simulation_manager import SimulationManager


from typing import Any, Dict, Optional


class Venus(Device, IProtocolRunner, IGenericExecutable):
    """ Venus device driver for managing Hamilton Venus protocols.
    This class provides methods to prepare labware for picking and placing, notify the driver of picked and placed labware, and execute commands or run protocols."""
    def __init__(self,
                name: str,
                init_protocol: Optional[str]  = None,
                picked_protocol: Optional[str]  = None,
                placed_protocol: Optional[str]  = None,
                prepare_pick_protocol: Optional[str]  = None,
                prepare_place_protocol: Optional[str]  = None,
                exe_path: str = r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe",
                methods_folder: str = r"C:\Program Files (x86)\HAMILTON\Methods",
                sim: bool = False) -> None:
        """ Initializes the Venus device driver.
        Args:
            name (str): The name of the Venus device.
            init_protocol (Optional[str]): The protocol to run during initialization.
            picked_protocol (Optional[str]): The protocol to run when labware is picked.
            placed_protocol (Optional[str]): The protocol to run when labware is placed.
            prepare_pick_protocol (Optional[str]): The protocol to prepare for picking labware.
            prepare_place_protocol (Optional[str]): The protocol to prepare for placing labware.
            exe_path (str): Path to the Hamilton Venus HxRun executable.
            methods_folder (str): Path to the folder containing Venus methods. This is prepended to the protocol paths.
            sim (bool): Whether to use simulation mode.
        """
        self._sim_manager = SimulationManager(VenusProtocolDriver(name,
                                                                  init_protocol,
                                                                  picked_protocol,
                                                                  placed_protocol,
                                                                  prepare_pick_protocol,
                                                                  prepare_place_protocol,
                                                                  exe_path,
                                                                  methods_folder),
                                            SimulationVenusProtocolDriver(name,
                                                                          init_protocol,
                                                                          picked_protocol,
                                                                          placed_protocol,
                                                                          prepare_pick_protocol,
                                                                          prepare_place_protocol,
                                                                          exe_path,
                                                                          methods_folder),
                                            sim)
        super().__init__(name, self._sim_manager)

    @property
    def driver(self) -> VenusProtocolDriver | SimulationVenusProtocolDriver:
        return self._sim_manager.driver

    @property
    def is_initialized(self) -> bool:
        return self.driver.is_initialized

    async def _do_prepare_for_pick(self, labware: LabwareInstance) -> None:
        await self.driver.prepare_for_pick(labware.name, labware.labware_type, None, None)

    async def _do_prepare_for_place(self, labware: LabwareInstance) -> None:
        await self.driver.prepare_for_place(labware.name, labware.labware_type, None, None)

    async def _do_notify_picked(self, labware: LabwareInstance) -> None:
        await self.driver.notify_picked(labware.name, labware.labware_type, None, None)

    async def _do_notify_placed(self, labware: LabwareInstance) -> None:
        await self.driver.notify_placed(labware.name, labware.labware_type, None, None)

    async def initialize(self) -> None:
        await self.driver.initialize()

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        await self.driver.execute(command, options)

    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        await self.driver.run_protocol(protocol_filepath, params)