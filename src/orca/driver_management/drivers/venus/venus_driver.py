import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from orca.driver_management.drivers.driver_interfaces import IProtocolRunnerDriver

orca_logger = logging.getLogger("orca")

class SimulationVenusProtocolDriver(IProtocolRunnerDriver):
    """
    Simulation of Venus Protocol Driver for testing purposes.
    """

    def __init__(self, 
                name: str, 
                init_protocol: Optional[str]  = None,
                picked_protocol: Optional[str]  = None,
                placed_protocol: Optional[str]  = None,
                prepare_pick_protocol: Optional[str]  = None,
                prepare_place_protocol: Optional[str]  = None,
                open_protocol: Optional[str] = None,
                close_protocol: Optional[str] = None,
                exe_path: str = r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe",
                methods_folder: str = r"C:\Program Files (x86)\HAMILTON\Methods",
                sim_time: float = 0.1):
        self._name = name
        self._sim_time = sim_time
        self._exe_path = exe_path
        self._methods_folder = methods_folder
        self._is_initialized = False
        self._is_running = False
        self._init_protocol: Optional[str]  = init_protocol
        self._picked_protocol: Optional[str]  = picked_protocol
        self._placed_protocol: Optional[str]  = placed_protocol
        self._prepare_pick_protocol: Optional[str]  = prepare_pick_protocol
        self._prepare_place_protocol: Optional[str]  = prepare_place_protocol
        self._open_protocol: Optional[str] = open_protocol
        self._close_protocol: Optional[str] = close_protocol

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    async def connect(self) -> None:
        self._is_connected = True

    async def disconnect(self) -> None:
        self._is_connected = False

    async def initialize(self) -> None:
        orca_logger.info(f"Initializing Venus Protocol Driver: {self._name}")
        self._is_initialized = True

    @property
    def is_running(self) -> bool:
        return self._is_running

    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        orca_logger.info(f"Running {self._prepare_place_protocol} for {labware_name} of type {labware_type}")
        await asyncio.sleep(self._sim_time) 

    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        orca_logger.info(f"Running {self._prepare_pick_protocol} for {labware_name} of type {labware_type}")
        await asyncio.sleep(self._sim_time)

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        orca_logger.info(f"Running {self._picked_protocol} for {labware_name} of type {labware_type}")
        await asyncio.sleep(self._sim_time) 

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        orca_logger.info(f"Running {self._placed_protocol} for {labware_name} of type {labware_type}")
        await asyncio.sleep(self._sim_time)

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        orca_logger.info(f"Executing command: {command} with options: {options}")
        await asyncio.sleep(self._sim_time) 
    
    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any] | None = None, options: Dict[str, Any] | None = None) -> None:
        if options is None:
            options = {}
        if params is None:
            params = {}
        orca_logger.info(f"Running protocol: {protocol_filepath} with params: {params} and options: {options}")
        await asyncio.sleep(self._sim_time)

    async def open(self) -> None:
        orca_logger.info(f"{self.name} opening...")
        if self._open_protocol:
            orca_logger.info(f"Running: {self._open_protocol}")
            await asyncio.sleep(self._sim_time)
        orca_logger.info(f"{self.name} opened")

    async def close(self) -> None:
        orca_logger.info(f"{self.name} closing...")
        if self._close_protocol:
            orca_logger.info(f"Running: {self._close_protocol}")
            await asyncio.sleep(self._sim_time)
        orca_logger.info(f"{self.name} closed")

class VenusProtocolDriver(IProtocolRunnerDriver):
    """
    Driver for interfacing with Hamilton Venus protocols.
    """

    def __init__(self,
                name: str, 
                init_protocol: Optional[str]  = None,
                picked_protocol: Optional[str]  = None,
                placed_protocol: Optional[str]  = None,
                prepare_pick_protocol: Optional[str]  = None,
                prepare_place_protocol: Optional[str]  = None,
                open_protocol: Optional[str] = None,
                close_protocol: Optional[str] = None,
                exe_path: str = r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe",
                methods_folder: str = r"C:\Program Files (x86)\HAMILTON\Methods"):
        self._name = name
        self._exe_path = exe_path
        self._methods_folder = methods_folder
        self._is_initialized = False
        self._is_running = False
        self._params_filepath = os.path.join(os.environ["TEMP"], "CheshireLabs\\Orca\\actionConfig.json")
        self._init_protocol: Optional[str]  = init_protocol
        self._picked_protocol: Optional[str]  = picked_protocol
        self._placed_protocol: Optional[str]  = placed_protocol
        self._prepare_pick_protocol: Optional[str]  = prepare_pick_protocol
        self._prepare_place_protocol: Optional[str]  = prepare_place_protocol
        self._open_protocol: Optional[str] = open_protocol
        self._close_protocol: Optional[str] = close_protocol

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    async def connect(self) -> None:
        self._is_connected = True

    async def disconnect(self) -> None:
        self._is_connected = False

    async def initialize(self) -> None:
        if self._init_protocol is not None:
            await self.execute("run", {"method": self._init_protocol})
        
        # create the temporary folder for the parameters file
        os.makedirs(os.path.dirname(self._params_filepath), exist_ok=True)
        if not os.path.exists(self._exe_path):
            raise FileNotFoundError("The executable path for the Venus driver was not provided and could not be found in the default locations")
            
        self._is_initialized = True

    @property
    def is_running(self) -> bool:
        return self._is_running

    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        if self._prepare_place_protocol is not None:
            await self.run_protocol(self._prepare_place_protocol, {
                "action": "prepare_for_place",
                "labware_name": labware_name,
                "labware_type": labware_type,
                "barcode": barcode,
                "alias": alias
            })

    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        if self._prepare_pick_protocol is not None:
            await self.run_protocol(self._prepare_pick_protocol, {
                "action": "prepare_for_pick",
                "labware_name": labware_name,
                "labware_type": labware_type,
                "barcode": barcode,
                "alias": alias
            })

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        if self._picked_protocol is not None:
            await self.run_protocol(self._picked_protocol, {
                "action": "notify_picked",
                "labware_name": labware_name,
                "labware_type": labware_type,
                "barcode": barcode,
                "alias": alias
            })

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        if self._placed_protocol is not None:
            await self.run_protocol(self._placed_protocol, {
                "action": "notify_placed",
                "labware_name": labware_name,
                "labware_type": labware_type,
                "barcode": barcode,
                "alias": alias
            })

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        if command == "run_protocol":
            method = options.get("method")
            if method is None:
                raise KeyError("The venus method was not provided in the command options.  'method' must be included with command")
            params = options.get("params", {})
            await self.run_protocol(method, params, options)
        else:
            raise NotImplementedError(f"The action '{command}' is unknown for {self._name} of type {type(self).__name__}")

    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any] | None = None, options: Dict[str, Any] | None = None) -> None:
        if params is None:
            params = {}
        if options is None:
            options = {}
        params["action"] = "run"
        options["params"] = params
        self._write_options_to_json_file(options)
        return await self._execute_protocol(protocol_filepath)
    
    async def _execute_protocol(self, hsl_method_path: str) -> None:
        if not os.path.exists(hsl_method_path):
            original_path = hsl_method_path
            hsl_method_path = os.path.join(self._methods_folder, hsl_method_path)
            if not os.path.exists(hsl_method_path):
                raise FileNotFoundError(f"The method '{original_path}' does not exist in the provided path or in the methods folder '{self._methods_folder}'.")                
        self._is_running = True

        try:
            process = await asyncio.create_subprocess_exec(
                self._exe_path, "-t", hsl_method_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                print(f"Venus error: {stderr.decode().strip()}")
        finally:
            self._is_running = False

    def _write_options_to_json_file(self, options: Dict[str, Any]) -> None:
        json.dump(options, open(self._params_filepath, "w"))

    async def open(self) -> None:
        if self._open_protocol:
            await self._execute_protocol(self._open_protocol)

    async def close(self) -> None:
        if self._close_protocol:
            await self._execute_protocol(self._close_protocol)

if __name__ == "__main__":
    async def main():
        driver = VenusProtocolDriver("Venus")
        await driver.initialize()
        await driver.prepare_for_place("test", "test")
        await driver.notify_placed("test", "test")
        await driver.execute("run", {
            "method": "Cheshire Labs\\VariableAccessTesting.hsl",
            "params": {
                "strParam": "strParam value transmitted",
                "intParam": 123,
                "fltParam": 1.003
                }
            })
        await driver.prepare_for_pick("test", "test")
        await driver.notify_picked("test", "test")

    asyncio.run(main())
