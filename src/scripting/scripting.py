from abc import ABC, abstractmethod

from typing import Dict
import importlib.util
import sys
from pathlib import Path
from system.system import ISystem
from workflow_models.labware_thread import LabwareThread
from workflow_models.labware_thread import IThreadObserver


class IThreadScript(IThreadObserver, ABC):
    @abstractmethod
    def thread_notify(self, event: str, thread: LabwareThread) -> None:
        raise NotImplementedError
    

class ThreadScript(IThreadScript, ABC):

    def __init__(self, system: ISystem) -> None:
        super().__init__()
        self._system: ISystem = system

    @property
    def system(self) -> ISystem:
        return self._system

    @abstractmethod
    def thread_notify(self, event: str, thread: LabwareThread) -> None:
        raise NotImplementedError

class IScriptRegistry(ABC):
    @abstractmethod
    def set_system(self, system: ISystem) -> None:
        raise NotImplementedError
        
    @abstractmethod
    def get_script(self, script_name: str) -> ThreadScript:
        raise NotImplementedError

    @abstractmethod
    def add_script(self, name: str, script: ThreadScript) -> None:
        raise NotImplementedError 

    @abstractmethod
    def create_script(self, filepath: str, class_name: str) -> ThreadScript:
        raise NotImplementedError

class IScriptFactory(ABC):
    
    def create_script(self, filepath: str, class_name: str) -> ThreadScript:
        raise NotImplementedError

    def set_system(self, system: ISystem) -> None:
        raise NotImplementedError

class ScriptFactory(IScriptFactory):

    def set_system(self, system: ISystem) -> None:
        self._system = system

    def create_script(self, filepath: str, class_name: str) -> ThreadScript:
        # get just the filename without the extension
        module_name= Path(filepath).stem

        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec is None:
            raise ValueError(f"File {filepath} not found")
        module_type = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module_type
        if spec.loader is None:
            raise ValueError(f"Loader not found for {filepath}")
        spec.loader.exec_module(module_type)
        try:
            ScriptClass = getattr(module_type, class_name)
        except AttributeError:
            raise ValueError(f"Class {class_name} not found in {filepath}")
        if not issubclass(ScriptClass, ThreadScript):
            raise ValueError(f"Class {class_name} is not a subclass of ThreadScript")
        script_instance: ThreadScript = ScriptClass(self._system)
        return script_instance



class ScriptRegistry(IScriptRegistry):
    def __init__(self, script_factory: IScriptFactory) -> None:
        self._factory = script_factory
        self._scripts: Dict[str, ThreadScript] = {}

    def set_system(self, system: ISystem) -> None:
        self._factory.set_system(system)

    def get_script(self, script_name: str) -> ThreadScript:
        return self._scripts[script_name]
    
    def add_script(self, name: str, script: ThreadScript) -> None:
        if name in self._scripts.keys():
            raise KeyError(f"Script {name} is already defined in the system.  Each script must have a unique name")
        self._scripts[name] = script

    def create_script(self, filepath: str, class_name: str) -> ThreadScript:
        return self._factory.create_script(filepath, class_name)
       