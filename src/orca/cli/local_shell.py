from typing import Any, Dict, List, Optional
import asyncio
import json
from orca.orca_core import OrcaCore

from orca.cli.shell_interface import IOrcaShell
    
class LocalOrcaShell(IOrcaShell):
    def __init__(self) -> None:
        self._orca: Optional[OrcaCore] = None

    def load(self, config_filepath: str):
        self._orca = OrcaCore(config_filepath)

    def init(self,
            config_file: Optional[str] = None, 
            resource_list: Optional[List[str]] = None, 
            options: Dict[str, Any] = {}):
        if config_file is not None:
            self.load(config_file)
        if self._orca is None:
            raise ValueError("An orca configuration file has not been initialized.  Please provide a configuration file.")
        asyncio.run(self._orca.initialize(resource_list, options))

    def run_workflow(self, 
                     workflow_name: str, 
                     config_file: Optional[str] = None, 
                     options: Dict[str, Any] = {}):
        if config_file is not None:
            self.load(config_file)
        if self._orca is None:
            raise ValueError("An orca configuration file has not been initialized.  Please provide a configuration file.")
        asyncio.run(self._orca.run_workflow(workflow_name))

    def run_method(self,
                   method_name: str, 
                   start_map_json: str, 
                   end_map_json: str,
                   config_file: Optional[str] = None, 
                   options: Dict[str, str] = {}):
        if config_file is not None:
            self.load(config_file)
        if self._orca is None:
            raise ValueError("An orca configuration file has not been initialized.  Please provide a configuration file.")
        start_map = json.loads(start_map_json)
        end_map = json.loads(end_map_json)
        asyncio.run(self._orca.run_method(method_name, start_map, end_map))
