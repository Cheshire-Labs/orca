
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Set, Union
from orca.resource_models.base_resource import Equipment
from orca.resource_models.location import Location

from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.resource_pool import EquipmentResourcePool

from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.workflow_models.dynamic_resource_action import DynamicResourceAction
from orca.workflow_models.labware_thread import IMethodObserver, IThreadObserver, Method


class MethodActionTemplate:
    def __init__(self, resource: Equipment | EquipmentResourcePool,
                 command: str,
                 inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 outputs: Optional[List[Union[LabwareTemplate, AnyLabwareTemplate]]] = None,
                 options: Optional[Dict[str, Any]] = None):
        if isinstance(resource, Equipment):
            self._resource_pool: EquipmentResourcePool = EquipmentResourcePool(resource.name, [resource])
        else:
            self._resource_pool = resource
        self._command = command
        self._options: Dict[str, Any] = {} if options is None else options
        self._inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = inputs
        self._outputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = outputs if outputs is not None else []

    @property
    def resource_pool(self) -> EquipmentResourcePool:
        return self._resource_pool

    @property
    def inputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._inputs
    
    @property
    def outputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._outputs

    @property
    def command(self) -> str:
        return self._command

    @property
    def options(self) -> Dict[str, Any]:
        return self._options

class IMethodTemplate(ABC):
    @abstractmethod
    def get_instance(self, labware_reg: ILabwareRegistry) -> Method:
        raise NotImplementedError("IMethodTemplate must implement get_instance method")


class MethodActionFactory:

    def __init__(self, template: MethodActionTemplate, labware_reg: ILabwareRegistry) -> None:
        self._template: MethodActionTemplate = template
        self._labware_reg = labware_reg

    def create_instance(self) -> DynamicResourceAction:

        # TODO: since refactoring, this MethodActionTemplate and DynamicResourceAction are very similar

        instance = DynamicResourceAction(self._labware_reg,
                                        self._template.resource_pool,
                                        self._template.command,
                                        self._template.inputs,
                                        self._template.outputs,
                                        self._template.options,
                                        )
        return instance

class MethodTemplate(IMethodTemplate):

    def __init__(self, name: str, options: Dict[str, Any] = {}):
        self._name = name
        self._actions: List[MethodActionTemplate] = []
        self._method_observers: List[IMethodObserver] = []
        self._options = options


    @property
    def name(self) -> str:
        return self._name

    @property
    def actions(self) -> List[MethodActionTemplate]:
        return self._actions
    
    @property
    def inputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        inputs: Set[Union[LabwareTemplate, AnyLabwareTemplate]] = set()
        for action in self._actions:
            inputs.update(action.inputs)
        return list(inputs)
    
    @property
    def outputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        outputs: Set[Union[LabwareTemplate, AnyLabwareTemplate]] = set()
        for action in self._actions:
            outputs.update(action.outputs)
        return list(outputs)

    def append_action(self, action: MethodActionTemplate):
        self._actions.append(action)

    def add_method_observer(self, observer: IMethodObserver) -> None:
        self._method_observers.append(observer)

    def get_instance(self, labware_reg: ILabwareRegistry) -> Method:
        method = Method(self.name)
        for action_template in self.actions:
            factory = MethodActionFactory(action_template, labware_reg)
            action = factory.create_instance()
            method.append_step(action)
            for o in self._method_observers:
                method.add_observer(o)

        return method
    
class JunctionMethodTemplate(IMethodTemplate):
    def __init__(self) -> None:
        self._method: Method | None = None
    
    def set_method(self, method: Method) -> None:
        self._method = method

    def get_instance(self, labware_reg: ILabwareRegistry) -> Method:
        if self._method is None:
            raise NotImplementedError("Method has not been set")
        return self._method
    
class ThreadTemplate:

    def __init__(self, labware_template: LabwareTemplate, start: Location, end: Location, observers: List[IThreadObserver] = []) -> None:
        self._labware_template: LabwareTemplate = labware_template
        self._start: Location = start
        self._end: Location = end
        self._methods: List[IMethodTemplate] = []
        self._wrapped_method: Method | None = None
        self._observers: List[IThreadObserver] = observers

    @property
    def name(self) -> str:
        return self._labware_template.name

    @property
    def labware_template(self) -> LabwareTemplate:
        return self._labware_template
    
    @property
    def start_location(self) -> Location:
        return self._start
    
    @property
    def end_location(self) -> Location:
        return self._end
    
    @property
    def method_resolvers(self) -> List[IMethodTemplate]:
        return self._methods
    
    @property
    def observers(self) -> List[IThreadObserver]:
        return self._observers
    
    def set_wrapped_method(self, wrapped_method: Method) -> None:
        self._wrapped_method
        for m in self._methods:
            if isinstance(m, JunctionMethodTemplate):
                m.set_method(wrapped_method)  
    
    def add_method(self, method: IMethodTemplate) -> None:
        self._methods.append(method)


class WorkflowTemplate:
    
    def __init__(self, name: str) -> None:
        self._name = name
        self._start_threads: Dict[str, ThreadTemplate] = {}
        self._threads: Dict[str, ThreadTemplate] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def thread_templates(self) -> List[ThreadTemplate]:
        return list(self._threads.values())

    @property
    def start_thread_templates(self) -> List[ThreadTemplate]:
        return list(self._start_threads.values())
    
    def add_thread(self, thread: ThreadTemplate, is_start: bool = False) -> None:
        self._threads[thread.name] = thread
        if is_start:
            self._start_threads[thread.name] = thread
