
from abc import ABC
from typing import Optional, List, Dict, Any


class IOrcaShell(ABC):
    def load(self, config_file: str):
        raise NotImplementedError

    def init(self, config_file: Optional[str] = None, resource_list: Optional[List[str]] = None, options: Dict[str, Any] = {}):
        raise NotImplementedError

    def run_workflow(self, workflow_name: str, config_file: Optional[str] = None, options: Dict[str, Any] = {}):
        raise NotImplementedError

    def run_method(self, method_name: str, start_map_json: str, end_map_json: str, config_file: Optional[str] = None, options: Dict[str, str] = {}):
        raise NotImplementedError