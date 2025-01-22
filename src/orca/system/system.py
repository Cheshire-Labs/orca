from abc import ABC, abstractmethod
from types import MappingProxyType
from typing import Dict, List
import uuid
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.resource_models.transporter_resource import TransporterEquipment
from orca.resource_models.base_resource import Equipment, IResource

from orca.resource_models.labware import Labware, LabwareTemplate
from orca.system.labware_registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry
from orca.system.workflow_registry import IWorkflowRegistry
from orca.system.resource_registry import IResourceRegistry, IResourceRegistryObesrver
from orca.system.system_map import ILocationRegistry, SystemMap
from orca.system.registries import LabwareRegistry, TemplateRegistry
from orca.system.template_registry_interfaces import IThreadTemplateRegistry, IMethodTemplateRegistry, IWorkflowTemplateRegistry
from orca.workflow_models.labware_thread import LabwareThread, Method
from orca.workflow_models.workflow import Workflow
from orca.workflow_models.workflow_templates import ThreadTemplate, MethodTemplate, WorkflowTemplate
from orca.system.thread_manager import IThreadManager

class ISystemInfo(ABC):
    @property
    @abstractmethod
    def id(self) -> str:
        raise NotImplementedError
    
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

class SystemInfo(ISystemInfo):
    def __init__(self, name: str, version: str, description: str, model_extra: Dict[str, str]) -> None:
        self._id = str(uuid.uuid4())
        self._name = name
        self._version = version
        self._description = description
        self._model_extra = model_extra
    
    @property
    def id(self) -> str:
        return self._id
    
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
    def model_extra(self) -> Dict[str, str]:
        return self._model_extra
    

class ISystem(ISystemInfo,
              IResourceRegistry, 
              ILabwareRegistry, 
              ILabwareTemplateRegistry, 
              IWorkflowTemplateRegistry, 
              IMethodTemplateRegistry, 
              IThreadTemplateRegistry, 
              ILocationRegistry, 
              IWorkflowRegistry, 
              IThreadManager,
              ABC):

    @property
    @abstractmethod
    def system_map(self) -> SystemMap:
        raise NotImplementedError

class System(ISystem):
    def __init__(self, 
                 info: SystemInfo, 
                 system_map: SystemMap, 
                 resource_registry: IResourceRegistry, 
                 template_registry: TemplateRegistry, 
                 labware_registry: LabwareRegistry, 
                 thread_manager: IThreadManager,
                 workflow_registry: IWorkflowRegistry) -> None:
        self._info = info
        self._resources = resource_registry
        self._system_map = system_map
        self._templates = template_registry
        self._labwares = labware_registry
        self._workflow_registry = workflow_registry
        self._thread_manager = thread_manager

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
    def labwares(self) -> List[Labware]:
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
    def threads(self) -> List[LabwareThread]:
        return self._thread_manager.threads

    def get_resource(self, name: str) -> IResource:
        return self._resources.get_resource(name)

    def get_equipment(self, name: str) -> Equipment:
        return self._resources.get_equipment(name)

    def get_transporter(self, name: str) -> TransporterEquipment:
        return self._resources.get_transporter(name)

    def get_resource_pool(self, name: str) -> EquipmentResourcePool:
        return self._resources.get_resource_pool(name)

    def get_location(self, name: str) -> Location:
        return self._system_map.get_location(name)

    def get_labware(self, name: str) -> Labware:
        return self._labwares.get_labware(name)

    def add_resource(self, resource: IResource) -> None:
        self._resources.add_resource(resource)

    def add_location(self, location: Location) -> None:
        self._system_map.add_location(location)

    def add_resource_pool(self, resource_pool: EquipmentResourcePool) -> None:
        self._resources.add_resource_pool(resource_pool)

    def add_labware(self, labware: Labware) -> None:
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

    def get_workflow(self, id: str) -> Workflow:
        return self._workflow_registry.get_workflow(id)
    
    def add_workflow(self, workflow: Workflow) -> None:
        self._workflow_registry.add_workflow(workflow)

    def get_thread(self, id: str) -> LabwareThread:
        return self._thread_manager.get_thread(id)
    
    def get_thread_by_labware(self, labware_id: str) -> LabwareThread:
        return self._thread_manager.get_thread_by_labware(labware_id)

    def add_thread(self, labware_thread: LabwareThread) -> None:
        self._thread_manager.add_thread(labware_thread)

    def get_method(self, id: str) -> Method:
        return self._workflow_registry.get_method(id)
    
    def add_method(self, method: Method) -> None:
        self._workflow_registry.add_method(method)

    def add_observer(self, observer: IResourceRegistryObesrver) -> None:
        return self._resources.add_observer(observer)
    
    def create_method_instance(self, template: MethodTemplate) -> Method:
        return self._workflow_registry.create_method_instance(template)
    
    def create_thread_instance(self, template: ThreadTemplate) -> LabwareThread:
        return self._thread_manager.create_thread_instance(template)
    
    def create_workflow_instance(self, template: WorkflowTemplate) -> Workflow:
        return self._workflow_registry.create_workflow_instance(template)
    
    async def start_all_threads(self) -> None:
        return await self._thread_manager.start_all_threads()

    def stop_all_threads(self) -> None:
        return self._thread_manager.stop_all_threads()
    
    async def initialize_all(self) -> None:
        return await self._resources.initialize_all()

    def clear_resources(self) -> None:
        return self._resources.clear_resources()