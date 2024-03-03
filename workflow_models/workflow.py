from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional
from resource_models.base_resource import EquipmentResource, IUseable
from resource_models.labware import Labware
from resource_models.location import Location
from routing.system_graph import Route, SystemGraph
from workflow_models.method_status import MethodStatus
from workflow_models.wrokflow_templates import LabwareThreadTemplate, MethodActionTemplate, MethodTemplate, WorkflowTemplate

class MethodAction:
    @staticmethod
    def from_template(template: MethodActionTemplate, labware_instance_inputs: List[Labware]) -> MethodAction:
        """
        Builds a MethodActionInstance object based on the provided template and labware instance inputs.
        If the output labware is also an input labware in the template, the output will be set to the corresponding input LabwareInstance.
        Otherwise, a new LabwareInstance will be created.
        """

        labware_instance_outputs: List[Labware] = []
        for template_output in template.outputs:
            output = next((labware for labware in labware_instance_inputs if labware.name == template_output.name), Labware(template_output.name, template_output.labware_type))
            labware_instance_outputs.append(output)
        instance = MethodAction(template.resource, template.command, labware_instance_inputs, labware_instance_outputs, template.options)
        return instance


    def __init__(self, 
                 resource: EquipmentResource, 
                 command: str, 
                 labware_instance_inputs: List[Labware], 
                 labware_instance_outputs: List[Labware], 
                 options: Dict[str, Any] = {}) -> None:
        self._resource: EquipmentResource = resource
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._awaiting_labware_inputs: List[Labware] = labware_instance_inputs
        self._loaded_labware_inputs: List[Labware] = []
        self._outputs: List[Labware] = labware_instance_outputs
        self._status: MethodStatus = MethodStatus.CREATED

    @property
    def resource(self) -> EquipmentResource:
        return self._resource
    
    @property
    def status(self) -> MethodStatus:
        return self._status
    
    @property
    def awaiting_labware_inputs(self) -> List[Labware]:
        return self._awaiting_labware_inputs
    
    def load_labware(self, labware: Labware) -> None:
        if labware not in self._awaiting_labware_inputs:
            if labware in self._loaded_labware_inputs:
                raise ValueError(f"Labware is not pending. Labware {labware} has already been assigned to this method")
            raise ValueError(f"Labware is not pending. Labware {labware} is not accepted by this method")
        self._awaiting_labware_inputs.remove(labware)
        self._loaded_labware_inputs.append(labware)
        if self._awaiting_labware_inputs == []:
            self._status = MethodStatus.READY
        self._resource.load_labware(labware)

    def unload_lawbare(self, labware: Labware) -> None:
        if labware not in self._outputs.values():
            raise ValueError(f"Labware is not an output of this method. Labware {labware} is not available to be unloaded")
        self._resource.unload_labware(labware)
        self._outputs[labware.name] = None

    def execute(self) -> None:
        self._resource.set_command(self._command)
        self._resource.set_command_options(self._options)
        self._status = MethodStatus.RUNNING
        self._resource.execute()
        self._status = MethodStatus.COMPLETED



class Method:
    @staticmethod
    def from_template(labware_instance_inputs: List[Labware], template: MethodTemplate) -> Method:
        actions = [MethodAction.from_template(action_template, labware_instance_inputs) for action_template in template.actions]
        return Method(template.name, actions)

    def __init__(self, name: str, actions: List[MethodAction]) -> None:
        self._name = name
        self._actions: List[MethodAction] = actions
        self._status = MethodStatus.CREATED
        self._parent_plate_thread_id: Optional[str] = None

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def actions(self) -> List[MethodAction]:
        return self._actions
    
    @property
    def status(self) -> MethodStatus:
        if all(action.status == MethodStatus.CANCELED for action in self._actions):
            return MethodStatus.CANCELED
        elif all(action.status in [MethodStatus.COMPLETED, MethodStatus.CANCELED] for action in self._actions):
            return MethodStatus.COMPLETED
        elif any(action.status == MethodStatus.ERRORED for action in self._actions):
            return MethodStatus.ERRORED
        elif any(action.status == MethodStatus.PAUSED for action in self._actions):
            return MethodStatus.PAUSED
        elif any(action.status == MethodStatus.AWAITING_RESOURCES for action in self._actions):
            return MethodStatus.AWAITING_RESOURCES
        elif any(action.status == MethodStatus.RUNNING for action in self._actions):
            return MethodStatus.RUNNING
        elif all(action.status == MethodStatus.READY for action in self._actions):
            return MethodStatus.READY
        elif all(action.status == MethodStatus.QUEUED for action in self._actions):
            return MethodStatus.QUEUED
        else:
            return MethodStatus.CREATED

    def append_action(self, action: MethodAction):
        self._actions.append(action)
    
    def get_next_action(self) -> Optional[MethodAction]:
        return next((action 
                     for action in self._actions 
                     if action.status in [MethodStatus.AWAITING_RESOURCES, MethodStatus.READY, MethodStatus.QUEUED]), None)


class LabwareThread:
    @staticmethod
    def from_template(template: LabwareThreadTemplate, labware: Optional[Labware] = None) -> LabwareThread:
        labware = Labware.from_template(template.labware) if labware is None else labware
        method_seq = [Method.from_method(method) for method in template.methods]
        thread = LabwareThread(labware.name, labware, method_sequence=method_seq, start_location=template.start, end_location=template.end)
        thread.set_start_location(template.start)
        thread.set_end_location(template.end)
        return thread

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
    
    @property
    def labware(self) -> Labware:
        return self._labware
    
    @property
    def completed_actions(self) -> List[BaseActions]:
        raise NotImplementedError

    @property
    def current_action(self) -> BaseAction:
        raise NotImplementedError

    @property
    def queued_actions(self) -> List[BaseAction]:
        raise NotImplementedError
    
    def _build_actions(self, method: MethodTemplate) -> List[BaseAction]:
        raise NotImplementedError

    def is_completed(self) -> bool:
        return all(method.status in [MethodStatus.COMPLETED, MethodStatus.CANCELED] for method in self._method_seq)
    
    def get_completed_route(self) -> Route:
        steps: List[RouteSingleStep] = []
        for action in self.complete_actions if type(action) == PickAction or type(action) == PlaceAction:
            step = RouteSingleStep(source=action.source, target=action.target, weight=action.time_to_complete, action=action)
            steps.append(step)
        route = Route(steps)
        return route
        
    def build_route(self, system_graph: SystemGraph) -> Route:
        """Builds a route from the current location through all pending actions to the end location

        Args:
            system_graph (SystemGraph): Graph of the system

        Returns:
            Route: Route to be taken
        """
        route = Route2()
        start_location = self.current_location
        for action in self.queued_actions:
            route.add_stop(action.resource.location, action)

            location = action.resource.location
            path = system_graph.get_shortest_available_path(start_location, location)
            
            for stop in next_route.steps:
                stop.set_action(action)
            next_route[location].set_action(action)
            route.append(next_route)
            start_location = location
        ending_route = system_graph.get_shortest_available_route(location, self.end_location)
        route.append(ending_route)
        return route

    def start():
        raise NotImplementedError

    def stop():
        raise NotImplementedError

    def get_next_action():
        raise NotImplementedError

    


class Workflow:
    @staticmethod
    def from_template(template: WorkflowTemplate) -> Workflow:
        threads = [LabwareThread.from_template(thread_template) for thread_name, thread_template in template.labware_threads.items()]
        workflow = Workflow(template.name, threads)
        return workflow

    def __init__(self, name:str, threads: List[LabwareThread]) -> None:
        self._name = name
        self._labware_threads: List[LabwareThread] = threads


    @property
    def labware_threads(self) -> List[LabwareThread]:
        return self._labware_threads
    
    def set_status_queued(self) -> None:
        for thread in self._labware_threads:
            for method in thread.methods:
                for action in method.actions:
                    action.set_status(MethodStatus.QUEUED)


class UnavailableWaitTask:
    def __init__(self, unavailable_resource: IUseable) -> None:
        self._resource: IUseable = unavailable_resource

    @property
    def resource(self) -> IUseable:
        return self._resource
    
    def subscribe_on_ready(self, callback: Callable[[IUseable], None]) -> None:
        self._resource.subscribe_on_available(callback)

    @property
    def estimated_time_to_ready(self) -> float:
        return self._resource.get_estimated_time_to_available()
                

class RouteBuilder:
    def __init__(self, graph: SystemGraph) -> None:
        self._graph: SystemGraph = graph
    
    def build_route(self, labware_thread: LabwareThread) -> Route:
        actions = [action for method in labware_thread.queued_methods for action in method.queued_actions]
        route = Route()
        start_location = labware_thread.current_location
        location_stops.append(start_location)
        for location in action_locations:
            next_path: List[str] = self._graph.get_shortest_available_path(start_location.name, location)
            location_stops.extend(next_path)
        
        for location in location_stops:
            system.locations[location]
            
            # TODO:  pretty much zip the actions and locations here... i think?
            
            route.append(next_route)
            start_location = location
        return route


        

class RouteManager:
    def __init__(self, system_graph: SystemGraph) -> None:
        self._system_graph: SystemGraph = system_graph
        self._route_planners: List[RoutePlanner] = []
    
    def set_workflow(self, workflow: Workflow) -> None:
        for thread in workflow.labware_threads:
            current_method = thread.get_current_method()
            next_action = current_method.get_next_action()
            # TODO: Working here
            self._route_planners.append(RoutePlanner(next_action.labware, next_action.resource, self._system_graph))

    def get_next_action(self) -> IAction:
        ready_route_planners = [planner for planner in self._route_planners if planner.is_ready()]
        sorted_route_planners = sorted(ready_route_planners, key=lambda planner: planner.time_pending)
        route = sorted_route_planners[0].get_route()
        route.get_next_action()