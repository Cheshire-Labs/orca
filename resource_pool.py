from typing import Any, Dict, List, Optional
from drivers.base_resource import BaseEquipmentResource

class AllResourcesBusyError(Exception):
    def __init__(self, resource_pool_name: str, message: str = "All resources are busy"):
        message = f"Resource pool {resource_pool_name}: {message}"
        super().__init__(message)
    
class ResourcePool(BaseEquipmentResource):
    @property
    def _resource(self) -> BaseEquipmentResource:
        return self.get_available_resource()
    
    @property
    def plate_pad(self) -> str:
        return self._resource.plate_pad

    def __init__(self, name: str, options: Dict[str, Any] = {}):
        self._name = name
        self._resources: Optional[List[BaseEquipmentResource]] = None
        self._pool_init_options: Dict[str, Any] = {}
        self._res_options: Dict[str, Any] = options
        self._res_command: Optional[str] = None
        self._res_command_options: Dict[str, Any] = {}
    
    def is_running(self) -> bool:
        return self._resource.is_running()
    
    def get_available_resource(self) -> BaseEquipmentResource:
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
    
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        if 'resources' not in init_options.keys():
            raise KeyError("No resources defined in pool config")
        self._resources = init_options['resources']
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