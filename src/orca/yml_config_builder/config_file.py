from typing import Any, Dict, Union
from orca.config_interfaces import ILabwareConfig, ILocationConfig, IMethodConfig, IResourceConfig, IResourcePoolConfig, IScriptBaseConfig, ISystemConfig, ISystemOptionsConfig, ISystemSettingsConfig, IVariablesConfig, IWorkflowConfig
from orca.yml_config_builder.configs import SystemConfigModel
from orca.yml_config_builder.dynamic_config import DynamicSystemConfigModel

import yaml

from orca.yml_config_builder.variable_resolution import VariablesRegistry

class SystemConfiguration(ISystemConfig):
   
    def __init__(self, config_filepath: str) -> None:
        self._config_filepath = config_filepath
        with open( self._config_filepath, "r") as f:
            yml_content = f.read()
        self._yml = yaml.load(yml_content, Loader=yaml.FullLoader) # type: ignore
        self._system_config_model = SystemConfigModel.model_validate(self._yml)
        self._variable_registry = VariablesRegistry()
        self._variable_registry.set_selector_configuration("labwares", self._system_config_model.labwares)
        self._variable_registry.set_selector_configuration("config", self._system_config_model.config)
        self._variable_registry.set_selector_configuration("opt", self._system_config_model.options)
        self._config = DynamicSystemConfigModel(self._system_config_model, self._variable_registry)

    def set_deployment_stage(self, stage: str) -> None:
        self._system_config_model.options.stage = stage
    
    def set_options(self, options: Dict[str, Any]) -> None:
        self._config.set_options(options)

    @property    
    def system(self) -> ISystemSettingsConfig:
        return self._config.system

    @property    
    def labwares(self) -> Dict[str, ILabwareConfig]:
        return self._config.labwares
    
    @property    
    def config(self) -> IVariablesConfig:
        return self._config.config

    @property    
    def locations(self) -> Dict[str, ILocationConfig]:
        return self._config.locations

    @property    
    def resources(self) -> Dict[str, Union[IResourceConfig, IResourcePoolConfig]]:
        return self._config.resources

    @property    
    def methods(self) -> Dict[str, IMethodConfig]:
        return self._config.methods

    @property    
    def workflows(self) -> Dict[str, IWorkflowConfig]:
        return self._config.workflows

    @property    
    def scripting(self) -> IScriptBaseConfig:
        return self._config.scripting

    @property
    def options(self) -> ISystemOptionsConfig:
        return self._config.options
