from types import MappingProxyType
from orca.resource_models.base_resource import IResource
from orca.resource_models.location import Location
from orca.system.labware_registry_interfaces import ILabwareRegistry, ILabwareTemplateRegistry
from orca.resource_models.labware import Labware, LabwareTemplate
from orca.system.resource_registry import IResourceRegistry
from orca.system.system_map import ILocationRegistry
from orca.system.template_registry_interfaces import IThreadTemplateRegistry, IMethodTemplateRegistry, IWorkflowTemplateRegistry
from orca.workflow_models.labware_thread import Method
from orca.workflow_models.workflow_templates import ThreadTemplate, MethodTemplate, WorkflowTemplate


from typing import List, Dict



    
# TODO: To be implemented
# TODO: LocationRegistry should probably just implement methods from this
class LabwareLocationManager:
    def __init__(self) -> None:
        self._labware_location: Dict[str, Location | None] = {}

    def get_labware_location(self, labware_id: str) -> Location | None:
        return self._labware_location.get(labware_id)
    
    def move_labware(self, labware_id: str, location: Location) -> None:
        self._labware_location[labware_id] = location

    def add_labware(self, labware_id: str) -> None:
        self._labware_location[labware_id] = None

    def get_labware_ids_at_location(self, location: Location) -> List[str]:
        return [labware_id for labware_id, loc in self._labware_location.items() if loc == location]
    
# TODO: To be implemented
class ResourceLocationManager:
    def __init__(self, 
                 resource_reg: IResourceRegistry, 
                 location_registry: ILocationRegistry) -> None:
        self._res_reg = resource_reg
        self._loc_reg = location_registry
        self._res_to_loc: Dict[str, Location] = {}
        self._loc_to_res: Dict[str, IResource] = {}
    
    def assign_resource_to_location(self, resource_name: str, location_name: str) -> None:
        resource = self._res_reg.get_resource(resource_name)
        location = self._loc_reg.get_location(location_name)
        self._res_to_loc[resource_name] = location
        self._loc_to_res[location_name] = resource

    def get_location_by_resource(self, resource_name: str) -> Location | None:
        return self._res_to_loc.get(resource_name, None)
    
    def get_resource_by_location(self, location_name: str) -> IResource | None:
        return self._loc_to_res.get(location_name, None)
    

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

    def clear(self) -> None:
        self._labwares.clear()
        self._labware_templates.clear()


class TemplateRegistry(IThreadTemplateRegistry, IWorkflowTemplateRegistry, IMethodTemplateRegistry):
    def __init__(self) -> None:
        self._labware_thread_templates: Dict[str, ThreadTemplate] = {}
        self._method_templates: Dict[str, MethodTemplate] = {}
        self._workflow_templates: Dict[str, WorkflowTemplate] = {}

    def get_labware_thread_template(self, name: str) -> ThreadTemplate:
        return self._labware_thread_templates[name]

    def get_workflow_templates(self) -> MappingProxyType[str, WorkflowTemplate]:
        return MappingProxyType(self._workflow_templates)

    def get_workflow_template(self, name: str) -> WorkflowTemplate:
        return self._workflow_templates[name]
    
    def get_method_templates(self) ->  MappingProxyType[str, MethodTemplate]:
        return MappingProxyType(self._method_templates)
    
    def get_method_template(self, name: str) -> MethodTemplate:
        return self._method_templates[name]

    def add_labware_thread_template(self, thread_template: ThreadTemplate) -> None:
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

    def clear(self) -> None:
        self._labware_thread_templates.clear()
        self._method_templates.clear()
        self._workflow_templates.clear()