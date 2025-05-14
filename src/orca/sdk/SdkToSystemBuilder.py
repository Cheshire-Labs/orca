from typing import List, Optional

from orca.resource_models.base_resource import Equipment
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.sdk import sdk
from orca.sdk.sdk_converters import convert_sdk_equipment_pool_to_system_equipment_pool, convert_sdk_equipment_to_system_equipment, convert_sdk_labware_to_system_labware, convert_sdk_location_to_system_location, convert_sdk_method_to_system_method, convert_sdk_thread_to_system_thread, convert_sdk_workflow_to_system_workflow
from orca.system.move_handler import MoveHandler
from orca.system.registries import LabwareRegistry, TemplateRegistry
from orca.system.reservation_manager import ReservationManager
from orca.system.resource_registry import ResourceRegistry
from orca.system.system import System, SystemInfo
from orca.system.system_map import SystemMap
from orca.system.thread_manager import IThreadManager, ThreadFactory, ThreadManager, ThreadRegistry
from orca.system.workflow_registry import WorkflowRegistry



class SdkToSystemBuilder:
    """
    This class is responsible for converting the SDK representation of a system into a system representation.
    """

  


    def __init__(self,
                 system_info: SystemInfo,
                 labwares: List[sdk.Labware],
                 resources: List[sdk.Equipment | sdk.EquipmentPool],
                 location_registry: sdk.LocationRegistry,
                 methods: List[sdk.Method],
                 workflows: List[sdk.Workflow],
                 threads: List[sdk.Thread]
                 ) -> None:
        self._system_info: Optional[SystemInfo] = None
        self._resource_reg: ResourceRegistry = self._get_resource_registry(resources)
        self._labware_registry: LabwareRegistry = self._get_labware_registry(labwares)
        self._template_registry: TemplateRegistry  = self._get_template_registry(methods, workflows, threads, location_registry)
        self._system_map: SystemMap = self._get_system_map(location_registry)
        self._thread_manager: IThreadManager = self._get_thread_manager(self._labware_registry, threads, self._system_map)
        self._system_map = SystemMap(self._resource_reg)

    def _get_system_map(self, location_registry: sdk.LocationRegistry) -> SystemMap:
        system_map = SystemMap(self._resource_reg)
        locations = location_registry.get_locations()
        for loc in locations:
            system_loc = convert_sdk_location_to_system_location(loc)
            self._system_map.add_location(system_loc)   
        return system_map

    def _get_thread_manager(self, labware_registry: LabwareRegistry, threads: List[sdk.Thread], system_map: SystemMap) -> ThreadManager:
        reservation_manager = ReservationManager(system_map)

        move_handler = MoveHandler(reservation_manager, labware_registry, system_map)
        thread_factory = ThreadFactory(labware_registry, move_handler, reservation_manager, system_map)
        
        
        thread_reg = ThreadRegistry(self._labware_registry, thread_factory)
        for t in threads:
            system_thread = convert_sdk_thread_to_system_thread(t, system_map)
            thread_reg.add_thread(system_thread)
        manager = ThreadManager(thread_reg, system_map, move_handler)
        return manager

    def _get_resource_registry(self, resources: List[sdk.Equipment | sdk.EquipmentPool]) -> ResourceRegistry:
        r = ResourceRegistry()
        for res in resources:
            system_res: Equipment | EquipmentResourcePool
            if isinstance(res, sdk.Equipment):
                system_res = convert_sdk_equipment_to_system_equipment(res)
                r.add_resource(system_res)
            elif isinstance(res, sdk.EquipmentPool):
                system_res = convert_sdk_equipment_pool_to_system_equipment_pool(res)
                r.add_resource_pool(system_res)
            else:
                raise TypeError(f"{res.name} is not of type {Equipment} or {EquipmentResourcePool}")
        return r
            

    def _get_labware_registry(self, labwares: List[sdk.Labware]) -> LabwareRegistry:
        reg = LabwareRegistry()
        for l in labwares:
            system_labware = convert_sdk_labware_to_system_labware(l)
            reg.add_labware_template(system_labware)
        return reg
    
    def _get_template_registry(self, methods: List[sdk.Method], workflows: List[sdk.Workflow], threads: List[sdk.Thread], location_reg: sdk.LocationRegistry) -> TemplateRegistry:
        reg = TemplateRegistry()
 
        for m in methods:
            reg.add_method_template(convert_sdk_method_to_system_method(m))
        for w in workflows:
            reg.add_workflow_template(convert_sdk_workflow_to_system_workflow(w, location_reg))
        for t in threads:
            reg.add_labware_thread_template(convert_sdk_thread_to_system_thread(t, location_reg))
        return reg


    def get_system(self):
        
        workflow_registry = WorkflowRegistry(self._thread_manager, self._labware_registry, system_map)
        system = System(self._system_info, 
                system_map, 
                self._resource_reg, 
                self._template_registry, 
                self._labware_registry, 
                self._thread_manager, 
                workflow_registry)
        return system