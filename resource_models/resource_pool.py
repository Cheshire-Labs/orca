from typing import Any, Dict, List, Optional

from resource_models.base_resource import EquipmentResource
from resource_models.labware import Labware

class AllResourcesBusyError(Exception):
    def __init__(self, resource_pool_name: str, message: str = "All resources are busy"):
        message = f"Resource pool {resource_pool_name}: {message}"
        super().__init__(message)
    
class EquipmentResourcePool(EquipmentResource):
    @property
    def _resource(self) -> EquipmentResource:
        return self.get_available_resource()
    

    def __init__(self, name: str, options: Dict[str, Any] = {}):
        self._name = name
        self._resources: Optional[List[EquipmentResource]] = None
        self._pool_init_options: Dict[str, Any] = {}
        self._res_options: Dict[str, Any] = options
        self._res_command: Optional[str] = None
        self._res_command_options: Dict[str, Any] = {}
    
    def is_running(self) -> bool:
        return self._resource.is_running()
    
    def get_available_resource(self) -> EquipmentResource:
        if self._resources is None:
            raise ValueError("No resources defined in pool")
        for resource in self._resources:
            if not resource.is_running():
                return resource
        raise AllResourcesBusyError(self._name, "All resources are busy")

    def initialize(self) -> bool:
       return self._resource.initialize()

    def is_initialized(self) -> bool:
        return self._resource.is_initialized()
    
    def set_resources(self, resources: List[EquipmentResource]) -> None:
        self._resources = resources
    
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._pool_init_options = init_options

    def set_command(self, command: str) -> None:
        # TODO: suggestion: add a check to see if the command is supported by the resources in the pool
        self._res_command = command

    def set_command_options(self, options: Dict[str, Any]) -> None:
        self._res_command_options = options

    def execute(self) -> None:
        if self._res_command is None:
            raise ValueError(f"No command set in resource pool {self._name} for execution")
        self._resource.set_command(self._res_command)
        self._resource.set_command_options(self._res_command_options)
        self._resource.execute()

    def load_labware(self, labware: Labware) -> None:
        return self._resource.load_labware(labware)
    
    def unload_labware(self, labware: Labware) -> None:
        return self._resource.unload_labware(labware)