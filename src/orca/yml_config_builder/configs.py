
import os
from typing import Any, Dict, Optional, List, Union, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from orca.config_interfaces import IVariablesConfig


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra='allow')

    @property
    def model_extra(self) -> Dict[str, Any]:
        model_extra = super().model_extra
        if model_extra is None:
            return {}
        return model_extra
    
class SystemOptionsConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')

    def get_deployment_stages(self) -> List[str]:
        return list(self.model_extra.keys())

class VariablesConfigModel(ConfigModel, IVariablesConfig):
    model_config = ConfigDict(extra='allow')
    
    def get_deployment_stages(self) -> List[str]:
        return list(self.model_extra.keys())
    
class ScriptConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    source: str

class ScriptBaseConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    base_dir: str = Field(alias='base-dir', default='')
    scripts: Dict[str, ScriptConfigModel] = Field(default={})

class MethodActionConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    resource: Optional[str] = None
    command: str
    inputs: List[str] = []
    outputs: List[str] | None = None

class MethodConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    script: List[str] = []
    on_init: List[str] = Field([], alias='on-init')
    actions: List[Dict[str, MethodActionConfigModel]]

class ThreadStepConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    method: str
    spawn: List[str] = []

class LabwareThreadConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    labware: str
    start: str
    end: str
    type: Literal['start', 'wrapper']
    scripts: List[str] = Field([])
    steps: List[Union[str, ThreadStepConfigModel]]


class SystemSettingsConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    name: str
    version: str = 'latest'
    description: str = ''

class LocationConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    teachpoint_name: str = Field(alias='teachpoint-name')

class LabwareConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    type: str
    static: bool = False

class ResourcePoolConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    type: str
    resources: List[str]

class ResourceConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    type: str
    com: Optional[str] = None
    ip: Optional[str] = None
    sim: Optional[bool] = None
    base_type: Optional[str] = Field(None, alias='base-type')
    plate_pad: Optional[str] = Field(None, alias='plate-pad')

class WorkflowConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    threads: Dict[str, LabwareThreadConfigModel]

class SystemConfigModel(ConfigModel):
    model_config = ConfigDict(extra='allow')
    system: SystemSettingsConfigModel
    options: SystemOptionsConfigModel = SystemOptionsConfigModel()
    labwares: Dict[str, LabwareConfigModel]
    config: VariablesConfigModel
    locations: Dict[str, LocationConfigModel] = {}
    resources: Dict[str, Union[ResourceConfigModel, ResourcePoolConfigModel]]
    methods: Dict[str, MethodConfigModel]
    workflows: Dict[str, WorkflowConfigModel]
    scripting: ScriptBaseConfigModel | None = Field(default=None)

    @model_validator(mode='before')
    def check_resource_type(cls, values: Dict[str, Any]):
        resources = values.get('resources', {})
        for key, resource in resources.items():
            if resource['type'] == 'pool':
                # Validate as ResourcePoolConfig
                resources[key] = ResourcePoolConfigModel(**resource)
            else:
                # Validate as ResourceConfig
                resources[key] = ResourceConfigModel(**resource)
        values['resources'] = resources
        return values


