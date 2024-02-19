from __future__ import annotations
from typing import List, Optional
from Action import IAction

from method import Method
from system import System
from workflow import Workflow

class CandidateActionCurator:
    """Replaces actions and sorts them according to determine what should be completed first"""
    def __init__(self, system: System) -> None:
        self._system = system

    def _sort(self, actions: List[IAction]) -> List[IAction]:
        # TODO: implement sorting algorithms
        return actions
    
    def _replace(self, actions: List[IAction]) -> List[IAction]:
        # TODO: set algorithms to replace actions with move actions when the plate isn't at the location yet
        return actions

    def get_curated_actions(self, actions: List[IAction]) -> List[IAction]:
        actions = self._replace(actions)
        actions = self._sort(actions)
        return actions
    
class Router:
    def __init__(self, system: System):
        self._system: System = system
        self._workflow: Optional[Workflow] = None
        self._curator = CandidateActionCurator(self._system)

    def set_method(self, method: Method) -> None:
        self._method = method
        candidate_next_action = method.get_next_action()
        self._candidate_actions = [candidate_next_action] if candidate_next_action is not None else []

    def set_workflow(self, workflow: Workflow) -> None:
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



    
