from typing import List

from orca.resource_models.labware import LabwareTemplate
from orca.system.move_handler import MoveHandler
from orca.system.registries import LabwareRegistry, TemplateRegistry
from orca.system.reservation_manager import ReservationManager
from orca.system.resource_registry import ResourceRegistry
from orca.system.system import System, SystemInfo
from orca.system.system_map import ILocationRegistry, SystemMap
from orca.system.thread_manager import IThreadManager, ThreadFactory, ThreadManager, ThreadRegistry
from orca.system.workflow_registry import WorkflowRegistry
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate


class SdkToSystemBuilder:
    """
    This class is responsible for converting the SDK representation of a system into a system representation.
    """

    def __init__(self,
                 name: str,
                description: str,
                 labwares: List[LabwareTemplate],
                 resources_registry: ResourceRegistry,
                 system_map: SystemMap,
                 methods: List[MethodTemplate],
                 workflows: List[WorkflowTemplate],
                 threads: List[ThreadTemplate],
                 ) -> None:
        self._system_info: SystemInfo = SystemInfo(name, description=description, version="0.1.0", model_extra={})
        self._resource_reg: ResourceRegistry = resources_registry
        self._labware_registry: LabwareRegistry = self._get_labware_registry(labwares)
        self._system_map: SystemMap = system_map
        self._template_registry: TemplateRegistry  = self._get_template_registry(methods, workflows, threads, self._system_map)
        
        self._thread_manager: IThreadManager = self._get_thread_manager(self._labware_registry, self._system_map)
        self._workflow_registry = WorkflowRegistry(self._thread_manager, self._labware_registry, self._system_map)

    def _get_thread_manager(self, labware_registry: LabwareRegistry, system_map: SystemMap) -> ThreadManager:
        reservation_manager = ReservationManager(system_map)

        move_handler = MoveHandler(reservation_manager, labware_registry, system_map)
        thread_factory = ThreadFactory(labware_registry, move_handler, reservation_manager, system_map)
        
        
        thread_reg = ThreadRegistry(self._labware_registry, thread_factory)
        manager = ThreadManager(thread_reg, system_map, move_handler)
        return manager
            

    def _get_labware_registry(self, labwares: List[LabwareTemplate]) -> LabwareRegistry:
        reg = LabwareRegistry()
        for l in labwares:
            if isinstance(l, LabwareTemplate):
                reg.add_labware_template(l)
        return reg
    
    def _get_template_registry(self, methods: List[MethodTemplate], workflows: List[WorkflowTemplate], threads: List[ThreadTemplate], location_reg: ILocationRegistry) -> TemplateRegistry:
        reg = TemplateRegistry()
 
        for m in methods:
            reg.add_method_template(m)
        for w in workflows:
            reg.add_workflow_template(w)
        for t in threads:
            reg.add_labware_thread_template(t)
        return reg



    def get_system(self):

        
        system = System(self._system_info, 
                self._system_map, 
                self._resource_reg, 
                self._template_registry, 
                self._labware_registry, 
                self._thread_manager, 
                self._workflow_registry)
        
        
        return system