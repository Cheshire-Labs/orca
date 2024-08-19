from typing import Any, Dict, Optional
from orca.scripting.scripting import IScriptRegistry
from orca.system.system import System
from orca.yml_config_builder.configs import SystemConfig, SystemOptionsConfig
from orca.yml_config_builder.dynamic_config import DynamicSystemConfig
from orca.yml_config_builder.template_factories import ConfigToSystemBuilder

import yaml

from orca.yml_config_builder.variable_resolution import VariablesRegistry

# TODO: This class and ConfigToSystemBuilder are too intertwined - need to refactor
class ConfigFile:
    def __init__(self, config_filepath: str) -> None:
        self._config_filepath = config_filepath
        with open( self._config_filepath, "r") as f:
            yml_content = f.read()
        self._yml = yaml.load(yml_content, Loader=yaml.FullLoader)
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

    def get_system(self, builder: ConfigToSystemBuilder) -> System:
        builder.set_config(self._config)
        builder.set_config_filepath(self._config_filepath)

        return builder.get_system()