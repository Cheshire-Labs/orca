from types import MappingProxyType
from typing import List
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.resource_models.transporter_resource import TransporterEquipment
from orca.resource_models.base_resource import Equipment, IResource

from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.events.execution_context import WorkflowExecutionContext
from orca.system.system_info import SystemInfo
from orca.system.system_interface import ISystem
from orca.system.interfaces import IMethodRegistry
from orca.system.interfaces import IWorkflowRegistry
from orca.system.resource_registry import IResourceRegistry, IResourceRegistryObesrver
from orca.system.system_map import SystemMap
from orca.system.registries import LabwareRegistry, TemplateRegistry
from orca.workflow_models.interfaces import IMethod
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread, IExecutingThreadRegistry
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow, IExecutingWorkflowRegistry
from orca.workflow_models.workflows.workflow_factories import ThreadFactory
from orca.system.thread_registry_interface import IThreadRegistry
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.workflows.workflow import WorkflowInstance
from orca.workflow_models.workflow_templates import WorkflowTemplate
from orca.system.thread_manager_interface import IThreadManager
from orca.workflow_models.workflows.workflow_registry import IExecutingMethodRegistry

class System(ISystem):
    def __init__(self, 
                 info: SystemInfo, 
                 system_map: SystemMap, 
                 resource_registry: IResourceRegistry, 
                 template_registry: TemplateRegistry, 
                 labware_registry: LabwareRegistry,
                 thread_registry: IThreadRegistry,
                 executing_method_registry: IExecutingMethodRegistry,
                 executing_thread_registry: IExecutingThreadRegistry, 
                 thread_factory: ThreadFactory,
                 thread_manager: IThreadManager,
                 method_registry: IMethodRegistry,
                 workflow_registry: IWorkflowRegistry,
                 executing_workflow_registry: IExecutingWorkflowRegistry) -> None:
        self._info = info
        self._resources = resource_registry
        self._system_map = system_map
        self._templates = template_registry
        self._labwares = labware_registry
        self._method_registry = method_registry
        self._workflow_registry = workflow_registry
        self._thread_manager = thread_manager
        self._thread_registry = thread_registry
        self._thread_factory = thread_factory
        self._executing_method_registry = executing_method_registry
        self._executing_thread_registry = executing_thread_registry
        self._executing_workflow_registry = executing_workflow_registry

    @property
    def id(self) -> str:
        return self._info.id

        
    @property
    def name(self) -> str:
        return self._info.name

    @property
    def version(self) -> str:
        return self._info.version

    @property
    def description(self) -> str:
        return self._info.description
    
    @property
    def system_map(self) -> SystemMap:
        return self._system_map
    
    @property
    def locations(self) -> List[Location]:
        return self._system_map.locations

    @property
    def labwares(self) -> List[LabwareInstance]:
        return self._labwares.labwares
    
    @property
    def resources(self) -> List[IResource]:
        return self._resources.resources

    @property
    def equipments(self) -> List[Equipment]:
        return self._resources.equipments
    
    @property
    def transporters(self) -> List[TransporterEquipment]:
        return self._resources.transporters
    
    @property
    def resource_pools(self) -> List[EquipmentResourcePool]:
        return self._resources.resource_pools
    
    @property
    def threads(self) -> List[LabwareThreadInstance]:
        return self._thread_registry.threads

    def get_resource(self, name: str) -> IResource:
        return self._resources.get_resource(name)

    def get_equipment(self, name: str) -> Equipment:
        return self._resources.get_equipment(name)

    def get_transporter(self, name: str) -> TransporterEquipment:
        return self._resources.get_transporter(name)

    def get_resource_pool(self, name: str) -> EquipmentResourcePool:
        return self._resources.get_resource_pool(name)
    
    def set_simulating(self, simulating: bool) -> None:
        return self._resources.set_simulating(simulating)

    def get_location(self, name: str) -> Location:
        return self._system_map.get_location(name)

    def get_labware(self, name: str) -> LabwareInstance:
        return self._labwares.get_labware(name)

    def add_resource(self, resource: IResource) -> None:
        self._resources.add_resource(resource)

    def add_location(self, location: Location) -> None:
        self._system_map.add_location(location)

    def add_resource_pool(self, resource_pool: EquipmentResourcePool) -> None:
        self._resources.add_resource_pool(resource_pool)

    def add_labware(self, labware: LabwareInstance) -> None:
        self._labwares.add_labware(labware)

    def get_labware_template(self, name: str) -> LabwareTemplate:
        return self._labwares.get_labware_template(name)
    
    def add_labware_template(self, labware: LabwareTemplate) -> None:
        self._labwares.add_labware_template(labware)
    
    def get_labware_thread_template(self, name: str) -> ThreadTemplate:
        return self._templates.get_labware_thread_template(name)
    
    def add_labware_thread_template(self, thread: ThreadTemplate) -> None:
        self._templates.add_labware_thread_template(thread)

    def get_method_templates(self) -> MappingProxyType[str, MethodTemplate]:
        return self._templates.get_method_templates()

    def get_method_template(self, name: str) -> MethodTemplate:
        return self._templates.get_method_template(name)
    
    def add_method_template(self, method: MethodTemplate) -> None:
        self._templates.add_method_template(method)

    def get_workflow_templates(self) -> MappingProxyType[str, WorkflowTemplate]:
        return self._templates.get_workflow_templates()

    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        return self._templates.get_workflow_template(name)
    
    def add_workflow_template(self, workflow: WorkflowTemplate) -> None:
        self._templates.add_workflow_template(workflow)

    def get_workflow(self, id: str) -> WorkflowInstance:
        return self._workflow_registry.get_workflow(id)
    
    def add_workflow(self, workflow: WorkflowInstance) -> None:
        self._workflow_registry.add_workflow(workflow)

    def get_executing_workflow(self, workflow_id: str) -> ExecutingWorkflow:
        return self._executing_workflow_registry.get_executing_workflow(workflow_id)

    def get_thread(self, id: str) -> LabwareThreadInstance:
        return self._thread_registry.get_thread(id)
    
    def get_thread_by_labware(self, labware_id: str) -> LabwareThreadInstance:
        return self._thread_registry.get_thread_by_labware(labware_id)

    def add_thread(self, labware_thread: LabwareThreadInstance) -> None:
        self._thread_registry.add_thread(labware_thread)

    def create_and_register_thread_instance(self, template: ThreadTemplate) -> LabwareThreadInstance:
        thread = self._thread_factory.create_instance(template)
        self._thread_registry.add_thread(thread)
        return thread
    
    def get_executing_method(self, id: str) -> ExecutingMethod:
        return self._executing_method_registry.get_executing_method(id)
    
    def create_executing_method(self, method_id: str, context: WorkflowExecutionContext) -> ExecutingMethod:
        return self._executing_method_registry.create_executing_method(method_id, context)

    def create_executing_thread(self, thread_id: str, context: WorkflowExecutionContext) -> ExecutingLabwareThread:
        return self._executing_thread_registry.create_executing_thread(thread_id, context)
    
    def get_executing_thread(self, thread_id: str) -> ExecutingLabwareThread:
        return self._executing_thread_registry.get_executing_thread(thread_id)

    def get_method(self, id: str) -> IMethod:
        return self._method_registry.get_method(id)
    
    def add_method(self, method: IMethod) -> None:
        self._method_registry.add_method(method)

    def add_observer(self, observer: IResourceRegistryObesrver) -> None:
        return self._resources.add_observer(observer)
    
    def create_and_register_method_instance(self, template: MethodTemplate) -> IMethod:
        return self._method_registry.create_and_register_method_instance(template)
        
    def create_and_register_workflow_instance(self, template: WorkflowTemplate) -> WorkflowInstance:
        return self._workflow_registry.create_and_register_workflow_instance(template)
    
    async def start_all_threads(self) -> None:
        return await self._thread_manager.start_all_threads()

    def stop_all_threads(self) -> None:
        return self._thread_manager.stop_all_threads()
    
    async def initialize_all(self) -> None:
        """ Initializes all resources in the system. This is typically called at the start of a workflow or method execution."""
        return await self._resources.initialize_all()

    def clear_resources(self) -> None:
        return self._resources.clear_resources()