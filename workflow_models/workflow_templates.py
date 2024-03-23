
from abc import ABC
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Set, Union
from resource_models.base_resource import Equipment, IResource
from resource_models.location import Location

from resource_models.labware import AnyLabware, AnyLabwareTemplate, Labware, LabwareTemplate
from resource_models.resource_pool import EquipmentResourcePool
from resource_models.transporter_resource import TransporterResource
from system import System
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

    def __init__(self, template: MethodActionTemplate, system: System) -> None:
        self._template: MethodActionTemplate = template
        self._system: System = system

    def create_instance(self) -> DynamicResourceAction:        
        
        # TODO: this will need to find the actually labware that should be going into the method the specific workflow
        inputs: List[Union[Labware, AnyLabware]] = [] 
        for input_template in self._template.inputs:
            if isinstance(input_template, AnyLabwareTemplate):
                inputs.append(AnyLabware())
            else:
                inputs.append(self._system.labwares[input_template.name])

        outputs: List[Labware] = [self._system.labwares[output.name] for output in self._template.outputs]

        instance = DynamicResourceAction(self._template.resource_pool, 
                                self._template.command, 
                                inputs,
                                outputs,
                                self._template.options)
        return instance
    


class IMethodResolver(ABC):
    def get_instance(self, system: System) -> Method:
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

    def get_instance(self, system: System) -> Method:
        method = Method(self.name)
        for action_template in self.actions:
            builder = MethodActionBuilder(action_template, system)
            action = builder.create_instance()
            method.append_step(action)
        method.set_children_threads(self._thread_names_to_spawn)
        return method
    
class JunctionMethodTemplate(IMethodResolver):
    def __init__(self) -> None:
        self._method: Method | None = None
    
    def set_method(self, method: Method) -> None:
        self._method = method

    def get_instance(self, system: System) -> Method:
        if self._method is None:
            raise NotImplementedError("Method has not been set")
        return self._method
    
class LabwareThreadTemplate:

    def __init__(self, labware: LabwareTemplate, start: Location, end: Location) -> None:
        self._labware: LabwareTemplate = labware
        self._start: Location = start
        self._end: Location = end
        self._methods: List[IMethodResolver] = []
        self._spawning_method: Method | None = None

    @property
    def labware(self) -> LabwareTemplate:
        return self._labware
    
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
    def __init__(self, template: LabwareThreadTemplate, system: System) -> None:
        self._template: LabwareThreadTemplate = template
        self._system: System = system

    def create_instance(self) -> LabwareThread:
        
        # Instantiate labware
        labware_instance = self._template.labware.create_instance()
        self._system.add_labware(labware_instance)

        # Build the method sequence
        method_seq: List[Method] = []
        for method_resolver in self._template.method_resolvers:
            method = method_resolver.get_instance(self._system)
            method_seq.append(method)
        
        # create the thread
        thread = LabwareThread(labware_instance.name,
                                labware_instance, 
                                self._template.start,
                                self._template.end,
                                self._system.system_graph)

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
    def __init__(self, template: WorkflowTemplate, system: System) -> None:
        self._template: WorkflowTemplate = template
        self._system: System = system
    
    def create_instance(self) -> Workflow:
        threads: Dict[str, LabwareThread] = {}
        for thread_name, thread_template in self._template.labware_threads.items():
            builder = LabwareThreadBuilder(thread_template, self._system)
            thread = builder.create_instance()
            threads[thread_name] = thread
        return Workflow(self._template.name, threads)


class SystemTemplate:
    def __init__(self, name: str, version: str, description: str, options: Dict[str, Any] = {}) -> None:
        self._name = name
        self._version = version
        self._description = description
        self._options = options
        self._labware_templates: Dict[str, LabwareTemplate] = {}
        self._resources: Dict[str, IResource] = {}
        self._resource_pools: Dict[str, EquipmentResourcePool] = {}
        self._locations: Dict[str, Location] = {}
        self._methods: Dict[str, MethodTemplate] = {}
        self._workflows: Dict[str, WorkflowTemplate] = {}        

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def description(self) -> str:
        return self._description

    @property
    def resources(self) -> Dict[str, IResource]:
        return self._resources
    
    @property
    def equipment(self) -> MappingProxyType[str, Equipment]:
        equipment = {name: r for name, r in self._resources.items() if isinstance(r, Equipment)}
        return MappingProxyType(equipment)
    
    @property
    def labware_transporters(self) -> MappingProxyType[str, TransporterResource]:
        transporters = {name: r for name, r in self._resources.items() if isinstance(r, TransporterResource)}
        return MappingProxyType(transporters)
    
    @property
    def resource_pools(self) -> Dict[str, EquipmentResourcePool]:
        return self._resource_pools
    
    @property
    def locations(self) -> Dict[str, Location]:
        return self._locations

    @property
    def labware_templates(self) -> Dict[str, LabwareTemplate]:
        return self._labware_templates
    
    @property
    def method_templates(self) -> Dict[str, MethodTemplate]:
        return self._methods

    @property
    def workflow_templates(self) -> Dict[str, WorkflowTemplate]:
        return self._workflows


    def add_labware(self, name: str, labware: LabwareTemplate) -> None:
        if name in self._labware_templates.keys():
            raise KeyError(f"Labware {name} is already defined in the system.  Each labware must have a unique name")
        self._labware_templates[name] = labware

    def add_resource(self, name: str, resource: IResource) -> None:
        if name in self._resources.keys():
            raise KeyError(f"Resource {name} is already defined in the system.  Each resource must have a unique name")
        self._resources[name] = resource

    def add_resource_pool(self, name: str, resource_pool: EquipmentResourcePool) -> None:
        if name in self._resource_pools.keys():
            raise KeyError(f"Resource Pool {name} is already defined in the system.  Each resource pool must have a unique name")
        self._resource_pools[name] = resource_pool

    def add_method_template(self, name: str, method: MethodTemplate) -> None:
        if name in self._methods.keys():
            raise KeyError(f"Method {name} is already defined in the system.  Each method must have a unique name")
        self._methods[name] = method

    def add_workflow_template(self, name: str, workflow: WorkflowTemplate) -> None:
        if name in self._workflows.keys():
            raise KeyError(f"Workflow {name} is already defined in the system.  Each workflow must have a unique name")
        self._workflows[name] = workflow

    def add_location(self, location: Location) -> None:
        if location.teachpoint_name in self._locations.keys():
            raise KeyError(f"Location {location.teachpoint_name} is already defined in the system.  Each location must have a unique name")
        self._locations[location.teachpoint_name] = location

    def get_resource_location(self, resource_name: str) -> Location:
        # TODO: better design
        for location in self._locations.values():
            if location.resource is not None and location.resource.name == resource_name:
                return location
        raise ValueError(f"Resource {resource_name} not found in locations")

    def create_system_instance(self) -> System:

        system = System(name=self._name,
                      description=self._description,
                      version=self._version,
                      options=self._options,
                      resources=self._resources,
                      locations=self._locations)
        # workflow_builders = {name: WorkflowBuilder(template, system) for name, template in self._workflows.items()}
        # workflows = {name: builder.create_instance() for name, builder in workflow_builders.items()}
        
        # system.workflows = workflows
        return system
