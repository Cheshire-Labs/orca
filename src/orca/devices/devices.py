from typing import Any, Dict, Optional
from orca.devices.device_interfaces import IDelidder, ILiquidHandler, IPlateWasher, IReader, IStorage, IWaste
from cheshire_drivers import (
    IDelidderDriver, ILiquidHandlerDriver, IPlateWasherDriver, IReaderDriver, IStorageDriver, IWasteDriver,
    SimDelidderDriver, SimLiquidHandlerDriver, SimPlateWasherDriver, SimReaderDriver, SimStorageDriver, SimWasteDriver
)
from orca.resource_models.devices import Device


class PlateWasher(Device[IPlateWasherDriver], IPlateWasher):
    def __init__(self, 
                 name: str, 
                 driver: IPlateWasherDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IPlateWasherDriver] = None) -> None:

        super().__init__( 
                 name,
                 driver,
                 sim_driver or SimPlateWasherDriver(name),
                 sim)
        
    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        await self.driver.run_protocol(protocol_filepath, params)

    

class LiquidHandler(Device[ILiquidHandlerDriver], ILiquidHandler):
    def __init__(self, 
                 name: str, 
                 driver: ILiquidHandlerDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[ILiquidHandlerDriver] = None) -> None:

       super().__init__(name, driver, sim_driver or SimLiquidHandlerDriver(name), sim)


    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        await self.driver.run_protocol(protocol_filepath, params)

class Delidder(Device[IDelidderDriver], IDelidder):
    def __init__(self, 
                 name: str, 
                 driver: IDelidderDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IDelidderDriver] = None) -> None:

        super().__init__(name, driver, sim_driver or SimDelidderDriver(name), sim)

    async def delid(self):
        await self.driver.delid()

class Waste(Device[IWasteDriver], IWaste):
    def __init__(self, 
                 name: str, 
                 driver: IWasteDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IWasteDriver] = None) -> None:

        super().__init__(name, driver, sim_driver or SimWasteDriver(name), sim)


class Storage(Device[IStorageDriver], IStorage):
    def __init__(self, 
                 name: str, 
                 driver: IStorageDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IStorageDriver] = None) -> None:
        super().__init__(name, driver, sim_driver or SimStorageDriver(name), sim)

class Reader(Device[IReaderDriver], IReader):
    def __init__(self, 
                 name: str, 
                 driver: IReaderDriver, 
                 sim: bool = False, 
                 sim_driver: Optional[IReaderDriver] = None) -> None:
        super().__init__(name, driver, sim_driver or SimReaderDriver(name), sim)

    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        await self.driver.read(protocol_filepath, output_filepath)