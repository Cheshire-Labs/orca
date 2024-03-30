
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Set, Union
from resource_models.base_resource import Equipment
from resource_models.location import Location

from resource_models.labware import AnyLabware, AnyLabwareTemplate, Labware, LabwareTemplate
from resource_models.resource_pool import EquipmentResourcePool

from system.labware_registry_interfaces import ILabwareRegistry
from workflow_models.location_action import DynamicResourceAction
from workflow_models.workflow import IMethodObserver, Method


class MethodActionTemplate:
    def __init__(self, resource: Equipment | EquipmentResourcePool,
                 command: str,
                 inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]],
                 outputs: Optional[List[LabwareTemplate]] = None,
                 options: Optional[Dict[str, Any]] = None):
        if isinstance(resource, Equipment):
            self._resource_pool: EquipmentResourcePool = EquipmentResourcePool(resource.name, [resource])
        else:
            self._resource_pool = resource
        self._command = command
        self._options: Dict[str, Any] = {} if options is None else options
        self._inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = inputs
        self._outputs: List[LabwareTemplate] = outputs if outputs is not None else []

    @property
    def resource_pool(self) -> EquipmentResourcePool:
        return self._resource_pool

    @property
    def inputs(self) -> List[Union[LabwareTemplate, AnyLabwareTemplate]]:
        return self._inputs
    
    @property
    def outputs(self) -> List[LabwareTemplate]:
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

        # TODO: this will need to find the actually labware that should be going into the method the specific workflow
        inputs: List[Union[Labware, AnyLabware]] = []
        for input_template in self._template.inputs:
            if isinstance(input_template, AnyLabwareTemplate):
                inputs.append(AnyLabware())
            else:
                inputs.append(self._labware_reg.get_labware(input_template.name))

        outputs: List[Labware] = [self._labware_reg.get_labware(output.name) for output in self._template.outputs]

        instance = DynamicResourceAction(self._template.resource_pool,
                                self._template.command,
                                inputs,
                                outputs,
                                self._template.options)
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
    def outputs(self) -> List[LabwareTemplate]:
        outputs: Set[LabwareTemplate] = set()
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
    
# TODO:  Commented, doesn't seem to be used
# class MethodFactory:
#     def __init__(self, labware_registry: ILabwareRegistry) -> None:
#         self._labware_registry: ILabwareRegistry = labware_registry

#     def create_instance(self, template: IMethodTemplate) -> Method:
#         method = template.get_instance(self._labware_registry)
#         return method

class ThreadTemplate:

    def __init__(self, labware_template: LabwareTemplate, start: Location, end: Location) -> None:
        self._labware_template: LabwareTemplate = labware_template
        self._start: Location = start
        self._end: Location = end
        self._methods: List[IMethodTemplate] = []
        self._wrapped_method: Method | None = None

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
    def labware_threads(self) -> List[ThreadTemplate]:
        return list(self._threads.values())

    @property
    def start_threads(self) -> List[ThreadTemplate]:
        return list(self._start_threads.values())
    
    def add_thread(self, thread: ThreadTemplate, is_start: bool = False) -> None:
        self._threads[thread.name] = thread
        if is_start:
            self._start_threads[thread.name] = thread

    # def create_system_instance(self) -> System:

    #     system = System(name=self._name,
    #                   description=self._description,
    #                   version=self._version,
    #                   options=self._options,
    #                   resources=self._resources,
    #                   locations=self._locations)
    #     # workflow_builders = {name: WorkflowBuilder(template, system) for name, template in self._workflows.items()}
    #     # workflows = {name: builder.create_instance() for name, builder in workflow_builders.items()}
        
    #     # system.workflows = workflows
    #     return system
