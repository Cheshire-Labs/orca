import logging
from typing import Any, Dict, List, Optional


from orca.driver_management.drivers.simulation_base.simulation_base import SimulationBaseDriver
from orca.helper import FilepathReconciler
from orca.resource_models.resource_extras.teachpoints import Teachpoint
from orca_driver_interface.transporter_interfaces import ITransporterDriver

orca_logger = logging.getLogger("orca")

class SimulationRoboticArmDriver(SimulationBaseDriver, ITransporterDriver):
    def __init__(self, name: str, filepath_reconciler: FilepathReconciler,  mocking_type: Optional[str] = None, sim_time: float = 0.2) -> None:
        super().__init__(name, mocking_type, sim_time)
        self._filepath_reconciler = filepath_reconciler
        self._positions: List[str] = []

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        super().set_init_options(init_options)
        self._load_taught_positions(self._init_options)

    async def initialize(self) -> None:
        self._sleep()
        self._is_initialized = True

    async def pick(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        orca_logger.info(f"Driver: {self._name} picking from {position_name}, labware type: {labware_type} picking...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} picked from {position_name}, labware type: {labware_type} picked")

    async def place(self, position_name: str, labware_type: str) -> None:
        self._validate_position(position_name)
        orca_logger.info(f"Driver: {self._name} placing to {position_name}, labware type: {labware_type} placing...")
        self._sleep()
        orca_logger.info(f"Driver: {self._name} placed to {position_name}, labware type: {labware_type} placed")

    def _validate_position(self, position_name: str) -> None:
        if position_name not in self._positions:
            raise ValueError(f"The position '{position_name}' is not taught for {self._name}")

    def get_taught_positions(self) -> List[str]:
        return self._positions

    def _load_taught_positions(self, init_options: Dict[str, Any]) -> None:
        if "positions" in init_options:
            positions_config = init_options["positions"]
            if isinstance(positions_config, list):
                self._positions = positions_config
            elif isinstance(positions_config, str):
                positions_filepath = self._filepath_reconciler.reconcile_filepath(positions_config)
                self._positions = [t.name for t in Teachpoint.load_teachpoints_from_file(positions_filepath)]
            else:
                raise ValueError(f"Positions configuration for {self._name} is not recognized.  Must be a filepath string or list of strings naming the teachpoints")