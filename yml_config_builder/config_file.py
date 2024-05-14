from typing import Any, Dict
from system.system import System
from yml_config_builder.configs import SystemConfig, SystemOptionsConfig
from yml_config_builder.dynamic_config import DynamicSystemConfig
from yml_config_builder.template_factories import ConfigToSystemBuilder

import yaml

from yml_config_builder.variable_resolution import VariablesRegistry


class ConfigFile:
    def __init__(self, path: str) -> None:
        self._path = path
        with open(self._path, 'r') as f:
            self._yml = yaml.load(f, Loader=yaml.FullLoader)
        self._system_config = SystemConfig.model_validate(self._yml)
        self._variable_registry = self._get_variable_registry(self._system_config)
        self._config = DynamicSystemConfig(self._system_config, self._variable_registry)

    def _get_variable_registry(self, system_config: SystemConfig) -> VariablesRegistry:
        registry = VariablesRegistry()
        registry.add_config("labwares", system_config.labwares)
        registry.add_config("config", system_config.config)
        return registry
    
    def set_command_line_options(self, options: Dict[str, Any]) -> None:
        if self._system_config.options is None:
            self._system_config.options = SystemOptionsConfig()
        # TODO: thrown together, make this nicer
        self._system_config.options.stage = options.get("stage", "prod")
        self._variable_registry.add_config("opt", self._system_config.options)

    def get_system(self) -> System:
        return ConfigToSystemBuilder(self._config).get_system()