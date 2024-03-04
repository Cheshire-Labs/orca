from abc import ABC
from typing import Any, Dict, Optional
from resource_models.base_resource import IExecutable, IUseable
from resource_models.ilabware_loadable import ILabwareLoadable


class EquipmentResource(ILabwareLoadable, IExecutable, IUseable, ABC):

    def __init__(self, name: str):
        self._name = name
        self._is_running = False
        self._command: Optional[str] = None
        self._is_initialized = False
        self._init_options: Dict[str, Any] = {}
        self._options: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def in_use(self) -> bool:
        return self._is_running
    
    def is_initialized(self) -> bool:
        return self._is_initialized
    
    def set_command(self, command: str) -> None:
        self._command = command.upper()

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options
        if "plate-pad" in init_options.keys():
            self._plate_pad = init_options["plate-pad"]

    def set_command_options(self, options: Dict[str, Any]) -> None:
        self._options = options