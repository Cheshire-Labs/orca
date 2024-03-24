import itertools
from resource_models.base_resource import Equipment, IResource
from resource_models.labware import Labware, LabwareTemplate
from resource_models.resource_pool import EquipmentResourcePool
from resource_models.transporter_resource import TransporterResource
from system.registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry, ILabwareThreadRegisty, IMethodRegistry, IResourceRegistry, IWorkflowRegistry
from system.system_map import SystemMap
from system.template_registry_interfaces import ILabwareThreadTemplateRegistry, IMethodTemplateRegistry, IWorkflowTemplateRegistry
from workflow_models.workflow import LabwareThread, Method, Workflow
from workflow_models.workflow_templates import LabwareThreadTemplate, MethodTemplate, WorkflowTemplate


from typing import List, Dict


class LabwareRegistry(ILabwareRegistry, ILabwareTemplateRegistry):
    def __init__(self) -> None:
        self._labwares: Dict[str, Labware] = {}
        self._labware_templates: Dict[str, LabwareTemplate] = {}

    @property
    def labwares(self) -> List[Labware]:
        return list(self._labwares.values())

    def get_labware(self, name: str) -> Labware:
        return self._labwares[name]
    
    def add_labware(self, labware: Labware) -> None:
        self._labwares[labware.name] = labware

    def get_labware_template(self, name: str) -> LabwareTemplate:
        return self._labware_templates[name]

    def add_labware_template(self, labware: LabwareTemplate) -> None:
        self._labware_templates[labware.name] = labware


class TemplateRegistry(ILabwareThreadTemplateRegistry, IWorkflowTemplateRegistry, IMethodTemplateRegistry):
    def __init__(self) -> None:
        self._labware_thread_templates: Dict[str, LabwareThreadTemplate] = {}
        self._method_templates: Dict[str, MethodTemplate] = {}
        self._workflow_templates: Dict[str, WorkflowTemplate] = {}

    def get_labware_thread_template(self, name: str) -> LabwareThreadTemplate:
        return self._labware_thread_templates[name]

    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        return self._workflow_templates[name]
    
    def get_method_template(self, name: str) -> MethodTemplate:
        return self._method_templates[name]

    def add_labware_thread_template(self, thread_template: LabwareThreadTemplate) -> None:
        name = thread_template.name
        if name in self._labware_thread_templates.keys():
            raise KeyError(f"Labware {name} is already defined in the system.  Each labware must have a unique name")
        self._labware_thread_templates[name] = thread_template

    def add_method_template(self, method: MethodTemplate) -> None:
        name = method.name
        if name in self._method_templates.keys():
            raise KeyError(f"Method {name} is already defined in the system.  Each method must have a unique name")
        self._method_templates[name] = method

    def add_workflow_template(self, workflow: WorkflowTemplate) -> None:
        name = workflow.name
        if name in self._workflow_templates.keys():
            raise KeyError(f"Workflow {name} is already defined in the system.  Each workflow must have a unique name")
        self._workflow_templates[name] = workflow


class InstanceRegistry(ILabwareThreadRegisty, IWorkflowRegistry, IMethodRegistry):
    def __init__(self) -> None:
        self._labware_threads: Dict[str, LabwareThread] = {}
        self._workflows: Dict[str, Workflow] = {}
        self._methods: Dict[str, Method] = {}
        
    def get_labware_thread(self, name: str) -> LabwareThread:
        return self._labware_threads[name]
    
    def add_labware_thread(self, labware_thread: LabwareThread) -> None:
        if labware_thread.name in self._labware_threads.keys():
            raise KeyError(f"Labware Thread {labware_thread.name} is already defined in the system.  Each labware thread must have a unique name")
        self._labware_threads[labware_thread.name] = labware_thread

    def get_workflow(self, name: str) -> Workflow:
        return self._workflows[name]
    
    def add_workflow(self, workflow: Workflow) -> None:
        if workflow.name in self._workflows.keys():
            raise KeyError(f"Workflow {workflow.name} is already defined in the system.  Each workflow must have a unique name")
        self._workflows[workflow.name] = workflow
  
    def get_method(self, name: str) -> Method:
        return self._methods[name]
    
    def add_method(self, method: Method) -> None:
        if method.name in self._methods.keys():
            raise KeyError(f"Method {method.name} is already defined in the system.  Each method must have a unique name")
        self._methods[method.name] = method


class ResourceRegistry(IResourceRegistry):
    def __init__(self, system_map: SystemMap) -> None:
        self._system_map = system_map
        self._resources: Dict[str, IResource] = {}
        self._resource_pools: Dict[str, EquipmentResourcePool] = {}

    @property
    def resources(self) -> List[IResource]:
        return list(self._resources.values())

    @property
    def equipments(self) -> List[Equipment]:
        return [r for r in self._resources.values() if isinstance(r, Equipment)]

    @property
    def transporters(self) -> List[TransporterResource]:
        return [r for r in self._resources.values() if isinstance(r, TransporterResource)]

    @property
    def resource_pools(self) -> List[EquipmentResourcePool]:
        return list(self._resource_pools.values())
    
    def get_resource(self, name: str) -> IResource:
        return self._resources[name]
    
    def get_equipment(self, name: str) -> Equipment:
        resource = self.get_resource(name)
        if not isinstance(resource, Equipment):
            raise ValueError(f"Resource {name} is not an Equipment resource")
        return resource
    
    def get_transporter(self, name: str) -> TransporterResource:
        resource = self.get_resource(name)
        if not isinstance(resource, TransporterResource):
            raise ValueError(f"Resource {name} is not an Equipment resource")
        return resource

    def add_resource(self, resource: IResource) -> None:
        name = resource.name
        if name in self._resources.keys():
            raise KeyError(f"Resource {name} is already defined in the system.  Each resource must have a unique name")
        if isinstance(resource, TransporterResource):
            taught_locations = resource.get_taught_positions()
            for edge in itertools.combinations(taught_locations, 2):
                self._system_map.add_edge(edge[0], edge[1], resource)
        self._resources[name] = resource

    def get_resource_pool(self, name: str) -> EquipmentResourcePool:
        return self._resource_pools[name]
    
    def add_resource_pool(self, resource_pool: EquipmentResourcePool) -> None:
        name = resource_pool.name
        if name in self._resource_pools.keys():
            raise KeyError(f"Resource Pool {name} is already defined in the system.  Each resource pool must have a unique name")
        self._resource_pools[name] = resource_pool