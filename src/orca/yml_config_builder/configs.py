
import os
from typing import Any, Dict, Optional, List, Union, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra='allow')

    @property
    def model_extra(self) -> Dict[str, Any]:
        model_extra = super().model_extra
        if model_extra is None:
            return {}
        return model_extra
    
class SystemOptionsConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    stage: str = 'dev'

class VariablesConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    
class ScriptConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    source: str

class ScriptBaseConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    base_dir: str = Field(alias='base-dir', default='')
    scripts: Dict[str, ScriptConfig] = Field(default={})

class MethodActionConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    resource: Optional[str] = None
    command: str
    inputs: List[str] = []
    outputs: List[str] | None = None

class MethodConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    script: List[str] = []
    on_init: List[str] = Field([], alias='on-init')
    actions: List[Dict[str, MethodActionConfig]]

class ThreadStepConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    method: str
    spawn: List[str] = []

class LabwareThreadConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    labware: str
    start: str
    end: str
    type: Literal['start', 'wrapper']
    scripts: List[str] = Field([])
    steps: List[Union[str, ThreadStepConfig]]


class SystemSettingsConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    name: str
    version: str = 'latest'
    description: str = ''

class LocationConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    teachpoint_name: str = Field(alias='teachpoint-name')

class LabwareConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    type: str
    static: bool = False

class ResourcePoolConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    type: str
    resources: List[str]

class ResourceConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    type: str
    com: Optional[str] = None
    ip: Optional[str] = None
    sim: Optional[bool] = None
    plate_pad: Optional[str] = Field(None, alias='plate-pad')

class WorkflowConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    threads: Dict[str, LabwareThreadConfig]

class SystemConfig(ConfigModel):
    model_config = ConfigDict(extra='allow')
    system: SystemSettingsConfig
    options: SystemOptionsConfig = SystemOptionsConfig()
    labwares: Dict[str, LabwareConfig]
    config: VariablesConfig
    locations: Dict[str, LocationConfig] = {}
    resources: Dict[str, Union[ResourceConfig, ResourcePoolConfig]]
    methods: Dict[str, MethodConfig]
    workflows: Dict[str, WorkflowConfig]
    scripting: ScriptBaseConfig = Field(default={})

    @model_validator(mode='before')
    def check_resource_type(cls, values: Dict[str, Any]):
        resources = values.get('resources', {})
        for key, resource in resources.items():
            if resource['type'] == 'pool':
                # Validate as ResourcePoolConfig
                resources[key] = ResourcePoolConfig(**resource)
            else:
                # Validate as ResourceConfig
                resources[key] = ResourceConfig(**resource)
        values['resources'] = resources
        return values


