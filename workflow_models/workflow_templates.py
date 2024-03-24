
from abc import ABC
from typing import Any, Dict, List, Optional, Set, Union
from resource_models.base_resource import Equipment
from resource_models.location import Location

from resource_models.labware import AnyLabware, AnyLabwareTemplate, Labware, LabwareTemplate
from resource_models.resource_pool import EquipmentResourcePool

from system.system_map import SystemMap
from system.registry_interfaces import ILabwareRegistry
from workflow_models.method_action import DynamicResourceAction
from workflow_models.workflow import LabwareThread, Method, Workflow


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

class MethodActionBuilder:

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
    


class IMethodResolver(ABC):
    def get_instance(self, labware_reg: ILabwareRegistry) -> Method:
        raise NotImplementedError("MethodResolver must implement get_instance method")

class MethodTemplate(IMethodResolver):

    def __init__(self, name: str, options: Dict[str, Any] = {}):
        self._name = name
        self._actions: List[MethodActionTemplate] = []
        self._thread_names_to_spawn: List[str] = []
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

    def set_spawn(self, thread_names_to_spawn: List[str]) -> None:
        self._thread_names_to_spawn = thread_names_to_spawn

    def get_instance(self, labware_reg: ILabwareRegistry) -> Method:
        method = Method(self.name)
        for action_template in self.actions:
            builder = MethodActionBuilder(action_template, labware_reg)
            action = builder.create_instance()
            method.append_step(action)
        method.set_children_threads(self._thread_names_to_spawn)
        return method
    
class JunctionMethodTemplate(IMethodResolver):
    def __init__(self) -> None:
        self._method: Method | None = None
    
    def set_method(self, method: Method) -> None:
        self._method = method

    def get_instance(self, labware_reg: ILabwareRegistry) -> Method:
        if self._method is None:
            raise NotImplementedError("Method has not been set")
        return self._method
    
class LabwareThreadTemplate:

    def __init__(self, labware_template: LabwareTemplate, start: Location, end: Location) -> None:
        self._labware_template: LabwareTemplate = labware_template
        self._start: Location = start
        self._end: Location = end
        self._methods: List[IMethodResolver] = []
        self._spawning_method: Method | None = None

    @property
    def name(self) -> str:
        return self._labware_template.name

    @property
    def labware_template(self) -> LabwareTemplate:
        return self._labware_template
    
    @property
    def start(self) -> Location:
        return self._start
    
    @property
    def end(self) -> Location:
        return self._end
    
    @property
    def method_resolvers(self) -> List[IMethodResolver]:
        return self._methods
    
    def set_spawning_method(self, spawning_method: Method) -> None:
        self._spawning_method
        for m in self._methods:
            if isinstance(m, JunctionMethodTemplate):
                m.set_method(spawning_method)  
    
    def add_method(self, method: IMethodResolver) -> None:
        self._methods.append(method)


class LabwareThreadBuilder:
    def __init__(self, template: LabwareThreadTemplate, labware_registry: ILabwareRegistry, system_map: SystemMap) -> None:
        self._template: LabwareThreadTemplate = template
        self._labware_registry: ILabwareRegistry = labware_registry
        self._system_map: SystemMap = system_map

    def create_instance(self) -> LabwareThread:
        
        # Instantiate labware
        labware_instance = self._template.labware_template.create_instance()
        self._labware_registry.add_labware(labware_instance)

        # Build the method sequence
        method_seq: List[Method] = []
        for method_resolver in self._template.method_resolvers:
            method = method_resolver.get_instance(self._labware_registry)
            method_seq.append(method)
        
        # create the thread
        thread = LabwareThread(labware_instance.name,
                                labware_instance, 
                                self._template.start,
                                self._template.end,
                                self._system_map)

        return thread


class WorkflowTemplate:
    
    def __init__(self, name: str) -> None:
        self._name = name
        self._labware_thread: Dict[str, LabwareThreadTemplate] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def labware_threads(self) -> Dict[str, LabwareThreadTemplate]:
        return self._labware_thread


class WorkflowBuilder:
    def __init__(self, template: WorkflowTemplate, labware_registry: ILabwareRegistry, system_map: SystemMap) -> None:
        self._template: WorkflowTemplate = template
        self._labware_registry: ILabwareRegistry = labware_registry
        self._system_map: SystemMap = system_map
    
    def create_instance(self) -> Workflow:
        threads: Dict[str, LabwareThread] = {}
        for thread_name, thread_template in self._template.labware_threads.items():
            builder = LabwareThreadBuilder(thread_template, self._labware_registry, self._system_map)
            thread = builder.create_instance()
            threads[thread_name] = thread
        return Workflow(self._template.name, threads)

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
