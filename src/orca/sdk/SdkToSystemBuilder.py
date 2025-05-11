from typing import List, Optional

from orca.sdk import sdk
from orca.system.registries import LabwareRegistry, TemplateRegistry
from orca.system.resource_registry import ResourceRegistry
from orca.system.system import System, SystemInfo
from orca.system.system_map import SystemMap
from orca.system.thread_manager import IThreadManager, ThreadManager
from orca.system.workflow_registry import IWorkflowRegistry, WorkflowRegistry
from orca.workflow_models.workflow_templates import MethodActionTemplate, MethodTemplate, ThreadTemplate, WorkflowTemplate



class SdkToSystemBuilder:
    """
    This class is responsible for converting the SDK representation of a system into a system representation.
    """

    def __init__(self) -> None:
        self._system_info: Optional[SystemInfo] = None
        self._resource_reg: ResourceRegistry = ResourceRegistry()
        self._template_registry: Optional[TemplateRegistry]  = TemplateRegistry()
        self._labware_registry: Optional[LabwareRegistry] = None
        self._thread_manager: Optional[IThreadManager] = None


    def set_system_info(self, name: str, version: str, description: str, model_extra = {}) -> None:
        """
        Set the system information.
        :param name: The name of the system.
        :param version: The version of the system.
        :param description: The description of the system.
        :param model_extra: Any extra information about the system model.
        """
        self._system_info = SystemInfo(name, version, description, model_extra)


    def add_resources(self, resources: List[sdk.Equipment]) -> None:
        for r in resources:
            system_equipment = convert_sdk_equipment_to_system_equipment(r)
            self._resource_reg.add_resource(system_equipment)

    def add_methods(self, methods: List[sdk.Method]) -> None:
        

    def add_workflow(self, workflow: sdk.Workflow) -> None:
        """
        Add a workflow to the system.
        :param workflow: The workflow to add.
        """
        workflow_template = WorkflowTemplate(
            name=workflow.name,
            description=workflow.description,
            steps=workflow.steps,
            template=workflow_template
        )
        self._workflow_registry.add_workflow(workflow)

    def get_system(self):
        system_map = SystemMap(self._resource_reg)
        workflow_registry = WorkflowRegistry(self._thread_manager, self._labware_registry, system_map)
        system = System(self._system_info, 
                system_map, 
                self._resource_reg, 
                self._template_registry, 
                self._labware_registry, 
                self._thread_manager, 
                workflow_registry)
        return system