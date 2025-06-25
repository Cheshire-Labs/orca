from orca.driver_management.driver_interfaces import ISealer, ITempGettable, ITempSettable
from orca.driver_management.drivers.sealers.a4s_sealer import A4SSealerDriver
from orca.driver_management.drivers.simulation_labware_placeable.simulation_labware_placeable import SimulationDeviceDriver
from orca.resource_models.base_resource import Equipment, EquipmentLabwareRegistry, ILabwarePlaceable, ISimulationable, orca_logger
from orca.resource_models.labware import LabwareInstance


from orca_driver_interface.driver_interfaces import ILabwarePlaceableDriver


from typing import List, Optional


class Device(Equipment, ILabwarePlaceable, ISimulationable):
    """A class that represents a device that can operate on labware."""
    def __init__(self, name: str, driver: ILabwarePlaceableDriver, sim: bool = False) -> None:
        """Initialize the device with a name and a driver.
        Args:
            name (str): The name of the device.
            driver (ILabwarePlaceableDriver): The driver for the device.
        """
        super().__init__(name, driver)
        self._live_driver: ILabwarePlaceableDriver = driver
        self._sim_driver: ILabwarePlaceableDriver = SimulationDeviceDriver(name, driver.name)
        self._driver: ILabwarePlaceableDriver = driver
        self._labware_reg = EquipmentLabwareRegistry()
        self._sim = False
        self.set_simulating(sim)

    @property
    def is_simulating(self) -> bool:
        """Returns whether the device is simulating or not."""
        return self._sim

    def set_simulating(self, simulating: bool) -> None:
        """Sets the simulation state of the device."""
        self._sim = simulating
        if simulating:
            self._driver = self._sim_driver
        else:
            self._driver = self._live_driver

    @property
    def labware(self) -> Optional[LabwareInstance]:
        return self._labware_reg.stage

    @property
    def loaded_labware(self) -> List[LabwareInstance]:
        return self._labware_reg.loaded_labware

    def initialize_labware(self, labware: LabwareInstance) -> None:
        if labware in self._labware_reg.loaded_labware:
            return
        else:
            self._labware_reg.set_stage(labware)

    async def prepare_for_place(self, labware: LabwareInstance) -> None:
        if self._labware_reg.stage is not None:
            raise ValueError(f"{self} - Stage already contains labware: {self._labware_reg.stage}.  Unable to place {labware}")
        orca_logger.info(f"{self} - preparing for place of {labware}")
        await self._driver.prepare_for_place(labware.name, labware.labware_type)

    async def prepare_for_pick(self, labware: LabwareInstance) -> None:
        if self._labware_reg.stage == labware:
            return
        else:
            orca_logger.info(f"{self} - preparing for pick of {labware}")
            await self._driver.prepare_for_pick(labware.name, labware.labware_type)
            self._labware_reg.unload_labware_to_stage(labware)

    async def notify_picked(self, labware: LabwareInstance) -> None:
        if self._labware_reg.stage != labware:
            raise ValueError(f"{self} - An error has ocurred.  The labware {labware} notified as picked does not match the previously staged labware. "
                              "The wrong labware may have been picked.")
        orca_logger.info(f"{self} - labware {labware} picked from stage")
        self._labware_reg.set_stage(None)
        await self._driver.notify_picked(labware.name, labware.labware_type)

    async def notify_placed(self, labware: LabwareInstance) -> None:
        if self._labware_reg.stage is not None:
            raise ValueError(f"{self} - An error has ocurred.  The labware {labware} notified as placed was placed with labware {self._labware_reg.stage} already on the stage.  "
                             "A crash may have occurred.")
        self._labware_reg.set_stage(labware)
        orca_logger.info(f"{self} - labware {labware} received on stage")
        await self._driver.notify_placed(labware.name, labware.labware_type)
        self._labware_reg.load_labware_from_stage(labware)

    def __str__(self) -> str:
        return f"Equipment: {self._name}"


class Sealer(Device, ISealer, ITempGettable, ITempSettable):
    def __init__(
        self,
        name: str,
        driver: A4SSealerDriver,
        sim: bool = False
    ):
        super().__init__(name, driver, sim)
        self._driver: A4SSealerDriver = driver

    async def seal(self, temperature: int, duration: float) -> None:
        await self._driver.seal(temperature, duration)

    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the sealer."""
        await self._driver.set_temperature(temperature)

    async def get_temperature(self) -> float:
        """Get the current temperature of the sealer."""
        return await self._driver.get_temperature()