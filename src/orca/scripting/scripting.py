from abc import ABC, abstractmethod

from typing import Dict
import importlib.util
import sys
from pathlib import Path
from orca.helper import FilepathReconciler
from orca.system.system import ISystem
from orca.workflow_models.labware_thread import LabwareThread
from orca.workflow_models.labware_thread import IThreadObserver
from orca.yml_config_builder.configs import ScriptBaseConfigModel


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
    
    @abstractmethod
    def clear(self) -> None:
        raise NotImplementedError

class IScriptFactory(ABC):
    
    def create_script(self, filepath: str, class_name: str) -> ThreadScript:
        raise NotImplementedError

    def set_system(self, system: ISystem) -> None:
        raise NotImplementedError


class NullScriptFactory(IScriptFactory):

    def create_script(self, filepath: str, class_name: str) -> ThreadScript:
        raise NotImplementedError

    def set_system(self, system: ISystem) -> None:
        self._system = system


class ScriptFactory(IScriptFactory):

    def __init__(self, scripting_config: ScriptBaseConfigModel | None, filepath_reconciler: FilepathReconciler) -> None:
        self._scripting_config = scripting_config
        self._filepath_reconciler = filepath_reconciler
        if self._scripting_config is not None:
            self._filepath_reconciler.set_base_dir(self._scripting_config.base_dir)

    def set_system(self, system: ISystem) -> None:
        self._system = system

    def create_script(self, filepath: str, class_name: str) -> ThreadScript:
        filepath = self._filepath_reconciler.reconcile_filepath(filepath)     

        # get just the filename without the extension
        module_name= Path(filepath).stem
        
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec is None:
                raise ValueError(f"spec not found for {filepath}")

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
    
    def clear(self) -> None:
        self._scripts.clear()
       