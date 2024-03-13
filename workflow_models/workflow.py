from __future__ import annotations

from typing import Any, Dict, List, Optional
from resource_models.base_resource import Equipment
from resource_models.location import Location
from resource_models.labware import Labware
from resource_models.resource_pool import EquipmentResourcePool
from routing.system_graph import SystemGraph
from workflow_models.action import ActionStatus, BaseAction
from workflow_models.method_status import MethodStatus


class MethodAction(BaseAction):

    def __init__(self, 
                 resource: Equipment,
                 command: str, 
                 labware_instance_inputs: List[Labware], 
                 labware_instance_outputs: List[Labware], 
                 options: Dict[str, Any] = {}) -> None:
        self._resource: Equipment = resource
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._awaiting_labware_inputs: List[Labware] = labware_instance_inputs
        self._loaded_labware_inputs: List[Labware] = []
        self._outputs: List[Labware] = labware_instance_outputs
        self._status: ActionStatus = ActionStatus.CREATED
    
    @property
    def resource(self) -> Equipment:
        return self._resource
    
    @property
    def awaiting_labware_inputs(self) -> List[Labware]:
        return self._awaiting_labware_inputs

    def _perform_action(self) -> None:
        self._resource.set_command(self._command)
        self._resource.set_command_options(self._options)
        self._status = ActionStatus.IN_PROGRESS
        self._resource.execute()
        self._status = ActionStatus.COMPLETED

    def __str__(self) -> str:
        return f"Method Action: {self._resource.name} - {self._command}"
    
class MethodActionResolver(BaseAction):
    def __init__(self, 
                 resource_pool: EquipmentResourcePool,
                 command: str, 
                 labware_instance_inputs: List[Labware], 
                 labware_instance_outputs: List[Labware], 
                 options: Dict[str, Any] = {}) -> None:
        self._resource_pool: EquipmentResourcePool = resource_pool
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._awaiting_labware_inputs: List[Labware] = labware_instance_inputs
        self._loaded_labware_inputs: List[Labware] = []
        self._outputs: List[Labware] = labware_instance_outputs
    
    @property
    def resource_pool(self) -> EquipmentResourcePool:
        return self._resource_pool
    
    def get_best_action(self, sourcing_location: Location, system: SystemGraph) -> MethodAction:
        available_resources = [resource for resource in self._resource_pool.resources if resource.is_available]
        ordered_resources = sorted(available_resources, key=lambda x: system.get_distance(sourcing_location.teachpoint_name, system.get_resource_location(x.name).teachpoint_name))
        if not ordered_resources:
            raise ValueError(f"{self}: No available resources")
        return MethodAction(ordered_resources[0], self._command, self._awaiting_labware_inputs, self._outputs, self._options)      
        
    def __str__(self) -> str:
        return f"Method Action Resolver: {self._resource_pool.name} - {self._command}"


class Method:

    def __init__(self, name: str, action_resolvers: List[MethodActionResolver]) -> None:
        self._name = name
        self._action_resolvers: List[MethodActionResolver] = action_resolvers
        self._status = MethodStatus.CREATED
        self._parent_plate_thread_id: Optional[str] = None

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def action_resolvers(self) -> List[MethodActionResolver]:
        return self._action_resolvers
    
    @property
    def status(self) -> MethodStatus:
        if all(action.status == MethodStatus.CANCELED for action in self._action_resolvers):
            return MethodStatus.CANCELED
        elif all(action.status in [MethodStatus.COMPLETED, MethodStatus.CANCELED] for action in self._action_resolvers):
            return MethodStatus.COMPLETED
        elif any(action.status == MethodStatus.ERRORED for action in self._action_resolvers):
            return MethodStatus.ERRORED
        elif any(action.status == MethodStatus.PAUSED for action in self._action_resolvers):
            return MethodStatus.PAUSED
        elif any(action.status == MethodStatus.AWAITING_RESOURCES for action in self._action_resolvers):
            return MethodStatus.AWAITING_RESOURCES
        elif any(action.status == MethodStatus.RUNNING for action in self._action_resolvers):
            return MethodStatus.RUNNING
        elif all(action.status == MethodStatus.READY for action in self._action_resolvers):
            return MethodStatus.READY
        elif all(action.status == MethodStatus.QUEUED for action in self._action_resolvers):
            return MethodStatus.QUEUED
        else:
            return MethodStatus.CREATED

    def append_action_resolver(self, action: MethodActionResolver):
        self._action_resolvers.append(action)
    
    def get_next_action_resolver(self) -> Optional[MethodActionResolver]:
        return next((action 
                     for action in self._action_resolvers 
                     if action.status in [MethodStatus.AWAITING_RESOURCES, MethodStatus.READY, MethodStatus.QUEUED]), None)


class LabwareThread:

    def __init__(self, name: str, labware: Labware, method_sequence: List[Method], start_location: Location, end_location: Location) -> None:
        self._name: str = name
        self._labware: Labware = labware
        self._method_seq: List[Method] = method_sequence
        self._start_location: Location = start_location
        self._current_location: Location = self._start_location
        self._end_location: Location = end_location
        self._status: MethodStatus = MethodStatus.CREATED

    @property
    def name(self) -> str:
        return self._name

    @property
    def start_location(self) -> Location:
        return self._start_location
    
    def set_start_location(self, location: Location) -> None:
        if self._status in [MethodStatus.CREATED]:
            self._current_location = self._start_location
        self._start_location = location
    
    @property
    def end_location(self) -> Location:
        if self._end_location is None:
            raise ValueError("End location has not been set")
        return self._end_location
    
    def set_end_location(self, location: Location) -> None:
        self._end_location = location
    
    @property
    def current_location(self) -> Location:
        return self._current_location

    @property
    def labware(self) -> Labware:
        return self._labware
    
    @property
    def methods(self) -> List[Method]:
        return self._method_seq
    
    @property
    def completed_methods(self) -> List[Method]:
        return [method for method in self._method_seq if method.status == MethodStatus.COMPLETED]
    
    @property
    def current_method(self) -> Method:
        return next((step for step in self._method_seq if step.status in [MethodStatus.RUNNING]))

    @property
    def queued_methods(self) -> List[Method]:
        return [method for method in self._method_seq if method.status == MethodStatus.QUEUED]

    def is_completed(self) -> bool:
        return all(method.status in [MethodStatus.COMPLETED, MethodStatus.CANCELED] for method in self._method_seq)

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def get_next_action(self) -> BaseAction:
        raise NotImplementedError



class Workflow:
    # @staticmethod
    # def from_template(template: WorkflowTemplate, labwares: Dict[str, Labware]) -> Workflow:
    #     threads = {thread_name: LabwareThread.from_template(thread_template, labwares) for thread_name, thread_template in template.labware_threads.items()}
    #     workflow = Workflow(template.name, threads)
    #     return workflow

    def __init__(self, name:str, threads: Dict[str, LabwareThread]) -> None:
        self._name = name
        self._labware_threads:  Dict[str, LabwareThread] = threads

    @property
    def labware_threads(self) ->  Dict[str, LabwareThread]:
        return self._labware_threads

