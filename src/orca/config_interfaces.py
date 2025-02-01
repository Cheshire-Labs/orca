from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union

class ISystemOptionsConfig(ABC):
    pass

class IVariablesConfig(ABC):
    @abstractmethod
    def get_deployment_stages(self) -> List[str]:
        raise NotImplementedError

class IScriptConfig(ABC):
    @property
    @abstractmethod
    def source(self) -> str:
        raise NotImplementedError

class IScriptBaseConfig(ABC):    
    @property
    @abstractmethod
    def base_dir(self) -> str:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def scripts(self) -> Dict[str, IScriptConfig]:
        raise NotImplementedError

class IMethodActionConfig(ABC):    
    @property
    @abstractmethod
    def resource(self) -> Optional[str]:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def command(self) -> str:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def inputs(self) -> List[str]:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def outputs(self) -> Optional[List[str]]:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def options(self) -> Dict[str, Any]:
        raise NotImplementedError

class IMethodConfig(ABC):    
    @property
    @abstractmethod
    def script(self) -> List[str]:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def on_init(self) -> List[str]:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def actions(self) -> List[Dict[str, IMethodActionConfig]]:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def options(self) -> Dict[str, Any]:
        raise NotImplementedError

class IThreadStepConfig(ABC):
    @property
    @abstractmethod
    def method(self) -> str:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def spawn(self) -> List[str]:
        raise NotImplementedError

class ILabwareThreadConfig(ABC):
    @property
    @abstractmethod
    def labware(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def start(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def end(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def type(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def scripts(self) -> List[str]:
        raise NotImplementedError

    @property
    @abstractmethod
    def steps(self) -> List[Union[str, IThreadStepConfig]]:
        raise NotImplementedError

class ISystemSettingsConfig(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def version(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def description(self) -> str:
        raise NotImplementedError

class ILocationConfig(ABC):
    @property
    @abstractmethod
    def teachpoint_name(self) -> str:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def options(self) -> Dict[str, Any]:
        raise NotImplementedError

class ILabwareConfig(ABC):
    @property
    @abstractmethod
    def type(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def static(self) -> bool:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def options(self) -> Dict[str, Any]:
        raise NotImplementedError

class IResourcePoolConfig(ABC):
    @property
    @abstractmethod
    def type(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def resources(self) -> List[str]:
        raise NotImplementedError

class IResourceConfig(ABC):
    @property
    @abstractmethod
    def type(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def com(self) -> Optional[str]:
        raise NotImplementedError

    @property
    @abstractmethod
    def ip(self) -> Optional[str]:
        raise NotImplementedError

    @property
    @abstractmethod
    def plate_pad(self) -> Optional[str]:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def sim(self) -> Optional[bool]:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def base_type(self) -> Optional[str]:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def options(self) -> Dict[str, Any]:
        raise NotImplementedError

class IWorkflowConfig(ABC):

    @property
    @abstractmethod
    def threads(self) -> Dict[str, ILabwareThreadConfig]:
        raise NotImplementedError

class ISystemConfig(ABC):
    @property
    @abstractmethod
    def system(self) -> ISystemSettingsConfig:
        raise NotImplementedError

    @property
    @abstractmethod
    def labwares(self) -> Dict[str, ILabwareConfig]:
        raise NotImplementedError

    @property
    @abstractmethod
    def config(self) -> IVariablesConfig:
        raise NotImplementedError

    @property
    @abstractmethod
    def locations(self) -> Dict[str, ILocationConfig]:
        raise NotImplementedError

    @property
    @abstractmethod
    def resources(self) -> Dict[str, Union[IResourceConfig, IResourcePoolConfig]]:
        raise NotImplementedError

    @property
    @abstractmethod
    def methods(self) -> Dict[str, IMethodConfig]:
        raise NotImplementedError

    @property
    @abstractmethod
    def workflows(self) -> Dict[str, IWorkflowConfig]:
        raise NotImplementedError

    @property
    @abstractmethod
    def scripting(self) -> IScriptBaseConfig | None:
        raise NotImplementedError
    
    @property
    @abstractmethod
    def options(self) -> ISystemOptionsConfig:
        raise NotImplementedError

