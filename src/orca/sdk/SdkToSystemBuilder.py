import asyncio
from typing import List
import typing

from orca.resource_models.labware import LabwareTemplate
from orca.sdk.events.event_bus_interface import IEventBus
from orca.sdk.events.event_bus import SystemBoundEventBus
from orca.system.system_info import SystemInfo
from orca.system.thread_manager_interface import IThreadManager
from orca.system.reservation_manager.move_handler import MoveHandler
from orca.system.registries import LabwareRegistry, TemplateRegistry
from orca.system.reservation_manager.reservation_manager import LocationReservationManager, ThreadReservationCoordinator
from orca.system.resource_registry import ResourceRegistry
from orca.system.system import System
from orca.system.system_map import ILocationRegistry, SystemMap
from orca.system.thread_manager import ThreadManager
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingThreadFactory, ExecutingThreadRegistry
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.workflows.workflow_factories import ThreadFactory
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflowFactory, ExecutingWorkflowRegistry
from orca.workflow_models.workflows.workflow_registry import MethodRegistry, ThreadRegistry, WorkflowRegistry
from orca.workflow_models.workflows.workflow_factories import MethodFactory, WorkflowFactory
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
                 event_bus: IEventBus,
                 ) -> None:
        self._system_info: SystemInfo = SystemInfo(name, description=description, version="0.1.0", model_extra={})
        self._resource_reg: ResourceRegistry = resources_registry
        self._labware_registry: LabwareRegistry = self._get_labware_registry(labwares)
        self._system_map: SystemMap = system_map
        self._event_bus = SystemBoundEventBus(event_bus)
        self._template_registry: TemplateRegistry = self._get_template_registry(methods, workflows, threads, self._system_map)
        self._method_factory = MethodFactory()
        self._thread_factory = ThreadFactory(self._method_factory)
        self._method_registry = MethodRegistry(self._method_factory)
        self._thread_registry = ThreadRegistry(self._thread_factory, 
                                               self._method_registry, 
                                               self._labware_registry)

        workflow_factory = WorkflowFactory(self._thread_factory)
        self._workflow_registry = WorkflowRegistry(workflow_factory, self._thread_registry)

        self._reservation_manager = LocationReservationManager(self._system_map)
        self._status_manager = StatusManager(self._event_bus)

        self._thread_reservation_coordinator = ThreadReservationCoordinator(self._system_map,
                                                                            self._thread_registry)
        self._move_hander = MoveHandler(self._thread_reservation_coordinator, self._system_map)
        self._executing_thread_factory = ExecutingThreadFactory(self._event_bus,
                                                                self._move_hander,
                                                                self._status_manager, 
                                                                self._thread_reservation_coordinator,
                                                                self._system_map)
        self._executing_thread_registry = ExecutingThreadRegistry(self._thread_registry,
                                                                  self._executing_thread_factory)
        
        self._thread_manager: IThreadManager = ThreadManager(self._executing_thread_registry)
        executing_workflow_factory = ExecutingWorkflowFactory(self._thread_manager,
                                                              self._thread_reservation_coordinator,
                                                            self._event_bus, 
                                                            self._move_hander, 
                                                            self._status_manager, 
                                                            self._system_map)
        self._executing_workflow_registry = ExecutingWorkflowRegistry(self._workflow_registry, executing_workflow_factory)


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
                self._thread_registry,
                self._executing_thread_registry,
                self._thread_factory,
                self._thread_manager, 
                self._method_registry,
                self._workflow_registry,
                self._executing_workflow_registry
                )
        self._event_bus.bind_system(system)
        
        return system