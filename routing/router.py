from __future__ import annotations
from typing import Any, List, Optional
from workflow_models.action import IAction, MethodAction
from resource_models.labware import Labware

from workflow_models.method import MethodInstance
from system import System
from workflow_models.workflow import WorkflowTemplate

class CandidateActionCurator:
    """Replaces actions and sorts them according to determine what should be completed first"""
    def __init__(self, system: System) -> None:
        self._system = system

    def _sort(self, actions: List[IAction]) -> List[IAction]:
        # TODO: implement sorting algorithms
        return actions
    
    def _replace(self, actions: List[IAction]) -> List[IAction]:
        # TODO: set algorithms to replace actions with move actions when the plate isn't at the location yet
        ## ** Maybe do this TODO else where
        # if labware is not at location, replace with move action
        return replaced_actions

    def get_curated_actions(self, actions: List[IAction]) -> List[IAction]:
        actions = self._replace(actions)
        actions = self._sort(actions)
        return actions




class ActionManager:
    """Responsible for keeping track of actions and their status"""
    def __init__(self, system: System):
        self._system: System = system
        self._workflow: Optional[WorkflowInstance] = None
        self._curator = CandidateActionCurator(self._system)

    def set_method(self, method: MethodInstance) -> None:
        self._method = method
        candidate_next_action = method.get_next_action()
        self._candidate_actions = [candidate_next_action] if candidate_next_action is not None else []

    def set_workflow(self, workflow: WorkflowInstance) -> None:
        self._workflow = workflow

    def _get_candidate_actions(self) -> List[IAction]:
        if self._workflow is None:
            raise ValueError("A workflow has not been set.  A workflow must be set before Router can determine the next action")
        candidate_actions: List[IAction] = []
        for _, labware_thread in self._workflow.labware_threads.items():
            candidate_next_method = labware_thread.get_next_method()
            if candidate_next_method is not None:
                method_action = candidate_next_method.get_next_action()
                if method_action is not None:
                    candidate_actions.append(method_action)
        return candidate_actions

    def get_next_action(self) -> IAction | None:
        curated_actions = self._curator.get_curated_actions(self._get_candidate_actions())

        if len(curated_actions) == 0:
            return None
        return curated_actions[0]


class LabwareManager:
    """Responsible for keeping track of labware locations and their status"""
    def __init__(self) -> None:
        self._labwares: List[Labware] = []
    

class RoutePlanner:
    """Responsible for planning routes between locations and actions"""


class ActionSet:
    def __init__(self) -> None:
        self._actions: List[IAction] = []

    def set_method_action(self, method_action: IAction) -> None:
        self._actions.append(action)

    def resources_available(self) -> bool:
        for action in self._actions:
            if action.resource.in_use:
                return False
        return True
    
    
    def execute(self) -> None:
        for action in self._actions:
            action.execute()
        
    def _release_resources(self) -> None:
        for action in self._actions:
            action.resource.release()

class SystemRouter:
    def __init__(self) -> None:
        self._labware_manager = LabwareManager()
        self._action_manager = ActionManager()
        self._route_planner = RoutePlanner()

    def get_next_action(self) -> IAction | None:

        # get the next action
        next_method_action = self._action_manager.get_next_action()
    
        # if next action resource is not available, get the estimated time until it is available
        if next_method_action.resource.in_use:
            wait_time = next_method_action.resource.get_estimated_time_until_available()

        # if the plate is not already there, get a route to the location
        shortest_route = graph.get_shortest_available_route(next_method_action.location, labware_in_use.location)
        if shortest_route is None:
            # get blocking locations
            blocking_locations = graph.get_blocking_locations(next_method_action.location, labware_in_use.location)
            # get the estimated time until the blocking locations are available
            wait_time = blocking_locations[0].get_estimated_time_until_available()


        # get one move on the shortest route and move the labware one step closer
        next_action = shortest_route.get_next_action()

        # if the resource are available, place a reserve on them all
        next_method_action.resource.reserve()

        # perform the action
        next_method_action.execute()

        # release the resources
        next_method_action.resource.release()


        # TODO: PREVIOUS CODE... select parts and move up

        labware_in_use = next_method_action.labware
        next_actions: List[IAction] = []
        if next_method_action.location != labware_in_use.location:
            next_actions = [
                PickAction(labware_in_use.location),
                PlaceAction(next_action.location),
                next_method_action
                ]
        else:
            next_actions = next_method_action
        # check all the resources are available from the path to the next method action
        for action in next_actions:
            # if the resources are not available, then get the estimated time until they are available
            if action.resource.in_use:
                wait_time = action.resource.get_estimated_time_until_available()

        # if the resource are available, place a reserve on them all
        [action.resource.reserve() for action in next_actions]

        # perform the actions
        [action.execute() for action in next_actions]

        # release the resources
        [action.resource.release() for action in next_actions]
            
        pass
    


    