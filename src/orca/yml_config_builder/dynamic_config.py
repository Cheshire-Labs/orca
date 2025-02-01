import re
from typing import Any, Dict, Generic, List, Optional, TypeVar, Union
from orca.config_interfaces import ISystemOptionsConfig, ISystemConfig, ISystemSettingsConfig, ILabwareConfig, IVariablesConfig, ILocationConfig, IResourceConfig, IResourcePoolConfig, IWorkflowConfig, ILabwareThreadConfig, IThreadStepConfig, IMethodConfig, IMethodActionConfig, IScriptBaseConfig, IScriptConfig
from orca.yml_config_builder.configs import SystemOptionsConfigModel, ConfigModel, LabwareConfigModel, LabwareThreadConfigModel, LocationConfigModel, MethodActionConfigModel, MethodConfigModel, ResourceConfigModel, ResourcePoolConfigModel, ScriptBaseConfigModel, ScriptConfigModel, SystemConfigModel, SystemSettingsConfigModel, ThreadStepConfigModel, VariablesConfigModel, WorkflowConfigModel
from orca.yml_config_builder.variable_resolution import VariablesRegistry


T = TypeVar('T', bound=ConfigModel)


class DynamicBaseConfigModel(Generic[T]):
    def __init__(self, config: T, registry: VariablesRegistry) -> None:
        self._config: T = config
        self._registry = registry

    def get_dynamic(self, value: Any) -> Any:
        """
        Resolves dynamic values.
        If the value is a string referencing a dynamic variable, resolve it.
        If it's a dictionary or list, resolve all nested values recursively.
        """
        if isinstance(value, dict):
            return {key: self.get_dynamic(val) for key, val in value.items()}
        elif isinstance(value, list):
            return [self.get_dynamic(item) for item in value]
        elif isinstance(value, str):
            return self._registry.get(value)  # Resolve dynamic values
        return value

class DynamicSystemOptionsConfigModel(DynamicBaseConfigModel[SystemOptionsConfigModel], ISystemOptionsConfig):
    pass

class DynamicVariablesConfigModel(DynamicBaseConfigModel[VariablesConfigModel], IVariablesConfig):
    def get_deployment_stages(self) -> List[str]:
        return list(self._config.model_extra.keys())
class DynamicScriptingConfigModel(DynamicBaseConfigModel[ScriptConfigModel], IScriptConfig):
    @property
    def source(self) -> str:
        return self.get_dynamic(self._config.source)


class DynamicScriptBaseConfigModel(DynamicBaseConfigModel[ScriptBaseConfigModel], IScriptBaseConfig):    
    @property    
    def base_dir(self) -> str:
        return self.get_dynamic(self._config.base_dir)
    
    @property    
    def scripts(self) -> Dict[str, IScriptConfig]:
        # handles default case of empty scripts
        if self._config == {}:
            return {}
        return {key: DynamicScriptingConfigModel(value, self._registry) for key, value in self._config.scripts.items()}

class DynamicMethodActionConfigModel(DynamicBaseConfigModel[MethodActionConfigModel], IMethodActionConfig):    
    @property    
    def resource(self) -> Optional[str]:
        return self.get_dynamic(self._config.resource)
    
    @property    
    def command(self) -> str:
        return self.get_dynamic(self._config.command)
    
    @property    
    def inputs(self) -> List[str]:
        return [self.get_dynamic(x) for x in self._config.inputs]
    
    @property    
    def outputs(self) -> Optional[List[str]]:
        if self._config.outputs is None:
            return None
        return [self.get_dynamic(x) for x in self._config.outputs]
    
    @property
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}

class DynamicMethodConfigModel(DynamicBaseConfigModel[MethodConfigModel], IMethodConfig):    
    @property    
    def script(self) -> List[str]:
        return [self.get_dynamic(x) for x in self._config.script]
    
    @property    
    def on_init(self) -> List[str]:
        return [self.get_dynamic(x) for x in self._config.on_init]
    
    @property    
    def actions(self) -> List[Dict[str, IMethodActionConfig]]:
        actions: List[Dict[str, IMethodActionConfig]] = []
        for action in self._config.actions:
            actions.append({key: DynamicMethodActionConfigModel(value, self._registry) for key, value in action.items()})
        return actions
    
    @property
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}

class DynamicThreadStepConfigModel(DynamicBaseConfigModel[ThreadStepConfigModel], IThreadStepConfig):
    @property    
    def method(self) -> str:
        return self.get_dynamic(self._config.method)
    
    @property    
    def spawn(self) -> List[str]:
        return [self.get_dynamic(s) for s in self._config.spawn]

class DynamicLabwareThreadConfigModel(DynamicBaseConfigModel[LabwareThreadConfigModel], ILabwareThreadConfig):
    @property    
    def labware(self) -> str:
        return self.get_dynamic(self._config.labware)

    @property    
    def start(self) -> str:
        return self.get_dynamic(self._config.start)

    @property    
    def end(self) -> str:
        return self.get_dynamic(self._config.end)

    @property    
    def type(self) -> str:
        return self.get_dynamic(self._config.type)

    @property    
    def scripts(self) -> List[str]:
        return [self.get_dynamic(s) for s in self._config.scripts]

    @property    
    def steps(self) -> List[Union[str, IThreadStepConfig]]:
        steps: List[Union[str, IThreadStepConfig]] = []
        for step in self._config.steps:
            if isinstance(step, str):
                # ignore any step matching the parttern ${0}, ${1}, etc
                # this is handled later in the resolution of the steps
                if re.match(r"\$\{\d+\}", step):
                    steps.append(step)
                else:
                    steps.append(self.get_dynamic(step))
            else:
                steps.append(DynamicThreadStepConfigModel(step, self._registry))
        return steps

class DynamicSystemSettingsConfigModel(DynamicBaseConfigModel[SystemSettingsConfigModel], ISystemSettingsConfig):
    @property    
    def name(self) -> str:
        return self.get_dynamic(self._config.name)

    @property    
    def version(self) -> str:
        return self.get_dynamic(self._config.version)

    @property    
    def description(self) -> str:
        return self.get_dynamic(self._config.description)

class DynamicLocationConfigModel(DynamicBaseConfigModel[LocationConfigModel], ILocationConfig):
    @property    
    def teachpoint_name(self) -> str:
        return self.get_dynamic(self._config.teachpoint_name)

    @property
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}

class DynamicLabwareConfigModel(DynamicBaseConfigModel[LabwareConfigModel], ILabwareConfig):
    @property    
    def type(self) -> str:
        return self.get_dynamic(self._config.type)

    @property    
    def static(self) -> bool:
        return self.get_dynamic(self._config.static)
    
    @property
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}

class DynamicResourcePoolConfigModel(DynamicBaseConfigModel[ResourcePoolConfigModel], IResourcePoolConfig):
    @property    
    def type(self) -> str:
        return self.get_dynamic(self._config.type)

    @property    
    def resources(self) -> List[str]:
        return self.get_dynamic(self._config.resources)

class DynamicResourceConfigModel(DynamicBaseConfigModel[ResourceConfigModel], IResourceConfig):
    @property    
    def type(self) -> str:
        return self.get_dynamic(self._config.type)

    @property    
    def com(self) -> Optional[str]:
        return self.get_dynamic(self._config.com)

    @property    
    def ip(self) -> Optional[str]:
        return self.get_dynamic(self._config.ip)

    @property    
    def plate_pad(self) -> Optional[str]:
        return self.get_dynamic(self._config.plate_pad)
    
    @property
    def sim(self) -> Optional[bool]:
        return self.get_dynamic(self._config.sim)
    
    @property
    def base_type(self) -> Optional[str]:
        return self.get_dynamic(self._config.base_type)
    
    @property
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}

class DynamicWorkflowConfigModel(DynamicBaseConfigModel[WorkflowConfigModel], IWorkflowConfig):

    @property    
    def threads(self) -> Dict[str, ILabwareThreadConfig]:
        return {key: DynamicLabwareThreadConfigModel(value, self._registry) for key, value in self._config.threads.items()}

class DynamicSystemConfigModel(DynamicBaseConfigModel[SystemConfigModel], ISystemConfig):
    @property
    def deployment_stage(self) -> str:
        return self._stage
    @deployment_stage.setter
    def deployment_stage(self, value: str) -> None:
        self._stage = value

    def set_options(self, options: Dict[str, Any]) -> None:
        self._config.options = SystemOptionsConfigModel.model_validate(options)

    @property    
    def system(self) -> ISystemSettingsConfig:
        return DynamicSystemSettingsConfigModel(self._config.system, self._registry)

    @property    
    def labwares(self) -> Dict[str, ILabwareConfig]:
        return {key: DynamicLabwareConfigModel(value, self._registry) for key, value in self._config.labwares.items()}

    @property    
    def config(self) -> IVariablesConfig:
        return DynamicVariablesConfigModel(self._config.config, self._registry)

    @property    
    def locations(self) -> Dict[str, ILocationConfig]:
        return {key: DynamicLocationConfigModel(value, self._registry) for key, value in self._config.locations.items()}

    @property    
    def resources(self) -> Dict[str, Union[IResourceConfig, IResourcePoolConfig]]:
        resources_dict: Dict[str, Union[IResourceConfig, IResourcePoolConfig]] = {}
        for key, value in self._config.resources.items():
            if isinstance(value, ResourceConfigModel):
                resources_dict[key] = DynamicResourceConfigModel(value, self._registry) 
            elif isinstance(value, ResourcePoolConfigModel):
                resources_dict[key] = DynamicResourcePoolConfigModel(value, self._registry)
            else:
                raise ValueError(f"Invalid resource type: {type(value)}")
        return resources_dict

    @property    
    def methods(self) -> Dict[str, IMethodConfig]:
        return {key: DynamicMethodConfigModel(value, self._registry) for key, value in self._config.methods.items()}


    @property    
    def workflows(self) -> Dict[str, IWorkflowConfig]:
        return {key: DynamicWorkflowConfigModel(value, self._registry) for key, value in self._config.workflows.items()}

    @property    
    def scripting(self) -> IScriptBaseConfig | None:
        if self._config.scripting is None:
            return None
        return DynamicScriptBaseConfigModel(self._config.scripting, self._registry)

    @property
    def options(self) -> DynamicSystemOptionsConfigModel:
        return DynamicSystemOptionsConfigModel(self._config.options, self._registry)