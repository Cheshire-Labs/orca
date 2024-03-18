
from abc import ABC
from types import MappingProxyType
from typing import Any, Dict, List, Optional
from resource_models.base_resource import Equipment, IResource
from resource_models.location import Location

from resource_models.labware import LabwareTemplate
from resource_models.resource_pool import EquipmentResourcePool
from resource_models.transporter_resource import TransporterResource
from system import System
from workflow_models.action import BaseAction
from workflow_models.method_action import ILabwareMatcher, LabwareInputManager, MethodActionResolver
from workflow_models.workflow import LabwareThread, Method, Workflow


class MethodActionTemplate(BaseAction, ABC):
    def __init__(self, resource: Equipment | EquipmentResourcePool,
                 command: str,
                 inputs: List[ILabwareMatcher],
                 outputs: Optional[List[LabwareTemplate]] = None,
                 options: Optional[Dict[str, Any]] = None):
        if isinstance(resource, Equipment):
            self._resource_pool: EquipmentResourcePool = EquipmentResourcePool(resource.name, [resource])
        else:
            self._resource_pool = resource
        self._command = command
        self._options: Dict[str, Any] = {} if options is None else options
        self._inputs: List[ILabwareMatcher] = inputs
        self._outputs: List[LabwareTemplate] = outputs if outputs is not None else []

    @property
    def resource_pool(self) -> EquipmentResourcePool:
        return self._resource_pool

    @property
    def inputs(self) -> List[ILabwareMatcher]:
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

    def create_instance(self) -> MethodActionResolver:        
        labware_input_manager: LabwareInputManager = LabwareInputManager(self._template.inputs)
        labware_instance_outputs: List[ILabwareMatcher] = []
        
        instance = MethodActionResolver(self._template.resource_pool, 
                                self._template.command, 
                                labware_input_manager,
                                labware_instance_outputs,
                                self._template.options)
        return instance
    
class MethodTemplate:

    def __init__(self, name: str, options: Dict[str, Any] = {}):
        self._name = name
        self._actions: List[MethodActionTemplate] = []
        self._options = options

    @property
    def name(self) -> str:
        return self._name

    @property
    def actions(self) -> List[MethodActionTemplate]:
        return self._actions

    def append_action(self, action: MethodActionTemplate):
        self._actions.append(action)



class MethodBuilder:
    def __init__(self, template: MethodTemplate, system: System) -> None:
        self._template: MethodTemplate = template
        self._system: System = system

    def create_instance(self) -> Method:
        actions: List[MethodActionResolver] = []
        for action_template in self._template.actions:
            builder = MethodActionBuilder(action_template, self._system)
            action = builder.create_instance()
            actions.append(action)
        return Method(self._template.name, actions)
    

class LabwareThreadTemplate:

    def __init__(self, labware: LabwareTemplate, start: Location, end: Location) -> None:
        self._labware: LabwareTemplate = labware
        self._start: Location = start
        self._end: Location = end
        self._methods: List[MethodTemplate] = []

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
    def methods(self) -> List[MethodTemplate]:
        return self._methods
    
    def add_method(self, method: MethodTemplate, method_step_options: Optional[Dict[str, Any]] = None) -> None:
        # TODO: may add option to update method options at the Workflow level
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
        for method_template in self._template.methods:
            method_builder = MethodBuilder(method_template, self._system)
            method = method_builder.create_instance()
            method_seq.append(method)
        
        # create the thread
        thread = LabwareThread(labware_instance.name, labware_instance, method_sequence=method_seq, start_location=self._template.start, end_location=self._template.end)
        thread.set_start_location(self._template.start)
        thread.set_end_location(self._template.end)
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
    def labwares(self) -> Dict[str, LabwareTemplate]:
        return self._labware_templates
    
    @property
    def methods(self) -> Dict[str, MethodTemplate]:
        return self._methods

    @property
    def workflows(self) -> Dict[str, WorkflowTemplate]:
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
        workflow_builders = {name: WorkflowBuilder(template, system) for name, template in self._workflows.items()}
        workflows = {name: builder.create_instance() for name, builder in workflow_builders.items()}
        
        system.workflows = workflows
        return system
