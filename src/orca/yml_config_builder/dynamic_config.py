import re
from typing import Any, Dict, Generic, List, Optional, TypeVar, Union
from orca.config_interfaces import ISystemOptionsConfig, ISystemConfig, ISystemSettingsConfig, ILabwareConfig, IVariablesConfig, ILocationConfig, IResourceConfig, IResourcePoolConfig, IWorkflowConfig, ILabwareThreadConfig, IThreadStepConfig, IMethodConfig, IMethodActionConfig, IScriptBaseConfig, IScriptConfig
from orca.yml_config_builder.configs import SystemOptionsConfig, ConfigModel, LabwareConfig, LabwareThreadConfig, LocationConfig, MethodActionConfig, MethodConfig, ResourceConfig, ResourcePoolConfig, ScriptBaseConfig, ScriptConfig, SystemConfig, SystemSettingsConfig, ThreadStepConfig, VariablesConfig, WorkflowConfig
from orca.yml_config_builder.variable_resolution import VariablesRegistry


T = TypeVar('T', bound=ConfigModel)


class DynamicBaseConfig(Generic[T]):
    def __init__(self, config: T, registry: VariablesRegistry) -> None:
        self._config: T = config
        self._registry = registry

    def get_dynamic(self, value: Any) -> Any:
        return self._registry.get(value)

class DynamicSystemOptionsConfig(DynamicBaseConfig[SystemOptionsConfig], ISystemOptionsConfig):
    pass

class DynamicVariablesConfig(DynamicBaseConfig[VariablesConfig], IVariablesConfig):
    
    pass

class DynamicScriptingConfig(DynamicBaseConfig[ScriptConfig], IScriptConfig):
    @property
    def source(self) -> str:
        return self.get_dynamic(self._config.source)


class DynamicScriptBaseConfig(DynamicBaseConfig[ScriptBaseConfig], IScriptBaseConfig):    
    @property    
    def base_dir(self) -> str:
        return self.get_dynamic(self._config.base_dir)
    
    @property    
    def scripts(self) -> Dict[str, IScriptConfig]:
        return {key: DynamicScriptingConfig(value, self._registry) for key, value in self._config.scripts.items()}

class DynamicMethodActionConfig(DynamicBaseConfig[MethodActionConfig], IMethodActionConfig):    
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

class DynamicMethodConfig(DynamicBaseConfig[MethodConfig], IMethodConfig):    
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
            actions.append({key: DynamicMethodActionConfig(value, self._registry) for key, value in action.items()})
        return actions
    
    @property
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}

class DynamicThreadStepConfig(DynamicBaseConfig[ThreadStepConfig], IThreadStepConfig):
    @property    
    def method(self) -> str:
        return self.get_dynamic(self._config.method)
    
    @property    
    def spawn(self) -> List[str]:
        return [self.get_dynamic(s) for s in self._config.spawn]

class DynamicLabwareThreadConfig(DynamicBaseConfig[LabwareThreadConfig], ILabwareThreadConfig):
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
                steps.append(DynamicThreadStepConfig(step, self._registry))
        return steps

class DynamicSystemSettingsConfig(DynamicBaseConfig[SystemSettingsConfig], ISystemSettingsConfig):
    @property    
    def name(self) -> str:
        return self.get_dynamic(self._config.name)

    @property    
    def version(self) -> str:
        return self.get_dynamic(self._config.version)

    @property    
    def description(self) -> str:
        return self.get_dynamic(self._config.description)

class DynamicLocationConfig(DynamicBaseConfig[LocationConfig], ILocationConfig):
    @property    
    def teachpoint_name(self) -> str:
        return self.get_dynamic(self._config.teachpoint_name)

    @property
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}

class DynamicLabwareConfig(DynamicBaseConfig[LabwareConfig], ILabwareConfig):
    @property    
    def type(self) -> str:
        return self.get_dynamic(self._config.type)

    @property    
    def static(self) -> bool:
        return self.get_dynamic(self._config.static)
    
    @property
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}

class DynamicResourcePoolConfig(DynamicBaseConfig[ResourcePoolConfig], IResourcePoolConfig):
    @property    
    def type(self) -> str:
        return self.get_dynamic(self._config.type)

    @property    
    def resources(self) -> List[str]:
        return self.get_dynamic(self._config.resources)

class DynamicResourceConfig(DynamicBaseConfig[ResourceConfig], IResourceConfig):
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
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}

class DynamicWorkflowConfig(DynamicBaseConfig[WorkflowConfig], IWorkflowConfig):

    @property    
    def threads(self) -> Dict[str, ILabwareThreadConfig]:
        return {key: DynamicLabwareThreadConfig(value, self._registry) for key, value in self._config.threads.items()}

class DynamicSystemConfig(DynamicBaseConfig[SystemConfig], ISystemConfig):
    @property    
    def system(self) -> ISystemSettingsConfig:
        return DynamicSystemSettingsConfig(self._config.system, self._registry)

    @property    
    def labwares(self) -> Dict[str, ILabwareConfig]:
        return {key: DynamicLabwareConfig(value, self._registry) for key, value in self._config.labwares.items()}

    @property    
    def config(self) -> IVariablesConfig:
        return DynamicVariablesConfig(self._config.config, self._registry)

    @property    
    def locations(self) -> Dict[str, ILocationConfig]:
        return {key: DynamicLocationConfig(value, self._registry) for key, value in self._config.locations.items()}

    @property    
    def resources(self) -> Dict[str, Union[IResourceConfig, IResourcePoolConfig]]:
        resources_dict: Dict[str, Union[IResourceConfig, IResourcePoolConfig]] = {}
        for key, value in self._config.resources.items():
            if isinstance(value, ResourceConfig):
                resources_dict[key] = DynamicResourceConfig(value, self._registry) 
            elif isinstance(value, ResourcePoolConfig):
                resources_dict[key] = DynamicResourcePoolConfig(value, self._registry)
            else:
                raise ValueError(f"Invalid resource type: {type(value)}")
        return resources_dict

    @property    
    def methods(self) -> Dict[str, IMethodConfig]:
        return {key: DynamicMethodConfig(value, self._registry) for key, value in self._config.methods.items()}


    @property    
    def workflows(self) -> Dict[str, IWorkflowConfig]:
        return {key: DynamicWorkflowConfig(value, self._registry) for key, value in self._config.workflows.items()}

    @property    
    def scripting(self) -> IScriptBaseConfig:
        return DynamicScriptBaseConfig(self._config.scripting, self._registry)

    @property
    def options(self) -> Dict[str, Any]:
        return {key: self.get_dynamic(value) for key, value in self._config.model_extra.items()}