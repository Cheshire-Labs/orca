from types import MappingProxyType
from typing import Dict, Any, List, Optional
from resource_models.location import Location
from resource_models.resource_pool import EquipmentResourcePool
from resource_models.transporter_resource import TransporterResource
from resource_models.base_resource import Equipment, IResource

from resource_models.labware import Labware, LabwareTemplate
from system.registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry, ILabwareThreadRegisty, IMethodRegistry, IResourceRegistry, IWorkflowRegistry
from system.system_map import ILocationRegistry, SystemMap
from system.registries import InstanceRegistry, LabwareRegistry, TemplateRegistry
from system.template_registry_interfaces import ILabwareThreadTemplateRegistry, IMethodTemplateRegistry, IWorkflowTemplateRegistry
from workflow_models.workflow import LabwareThread, Method, Workflow
from workflow_models.workflow_templates import LabwareThreadTemplate, MethodTemplate, WorkflowTemplate


class SystemInfo:
    def __init__(self, name: str, version: str, description: str, model_extra: Dict[str, str]) -> None:
        self._name = name
        self._version = version
        self._description = description
        self._model_extra = model_extra

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
    


class System(IResourceRegistry, ILabwareRegistry, ILabwareTemplateRegistry, IWorkflowTemplateRegistry, IMethodTemplateRegistry, ILabwareThreadTemplateRegistry, ILocationRegistry, IWorkflowRegistry, ILabwareThreadRegisty, IMethodRegistry):
    def __init__(self, info: SystemInfo, system_map: SystemMap, resource_registry: IResourceRegistry, template_registry: TemplateRegistry, labware_registry: LabwareRegistry, instance_registry: InstanceRegistry) -> None:
        self._info = info
        self._resources = resource_registry
        self._system_map = system_map
        self._templates = template_registry
        self._labwares = labware_registry
        self._instances = instance_registry

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
    def labwares(self) -> List[Labware]:
        return self._labwares.labwares
    
    @property
    def resources(self) -> List[IResource]:
        return self._resources.resources

    @property
    def equipments(self) -> List[Equipment]:
        return self._resources.equipments
    
    @property
    def transporters(self) -> List[TransporterResource]:
        return self._resources.transporters
    
    @property
    def resource_pools(self) -> List[EquipmentResourcePool]:
        return self._resources.resource_pools
    
    def get_resource(self, name: str) -> IResource:
        return self._resources.get_resource(name)

    def get_equipment(self, name: str) -> Equipment:
        return self._resources.get_equipment(name)

    def get_transporter(self, name: str) -> TransporterResource:
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
    
    def get_labware_thread_template(self, name: str) -> LabwareThreadTemplate:
        return self._templates.get_labware_thread_template(name)
    
    def add_labware_thread_template(self, thread: LabwareThreadTemplate) -> None:
        self._templates.add_labware_thread_template(thread)

    def get_method_template(self, name: str) -> MethodTemplate:
        return self._templates.get_method_template(name)
    
    def add_method_template(self, method: MethodTemplate) -> None:
        self._templates.add_method_template(method)

    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        return self._templates.get_workflow_template(name)
    
    def add_workflow_template(self, workflow: WorkflowTemplate) -> None:
        self._templates.add_workflow_template(workflow)
    

    def get_workflow(self, name: str) -> Workflow:
        return self._instances.get_workflow(name)
    
    def add_workflow(self, workflow: Workflow) -> None:
        self._instances.add_workflow(workflow)

    def get_labware_thread(self, name: str) -> LabwareThread:
        return self._instances.get_labware_thread(name)

    def add_labware_thread(self, labware_thread: LabwareThread) -> None:
        self._instances.add_labware_thread(labware_thread)

    def get_method(self, name: str) -> Method:
        return self._instances.get_method(name)
    
    def add_method(self, method: Method) -> None:
        self._instances.add_method(method)


