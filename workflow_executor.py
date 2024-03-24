from typing import Dict, Iterator, List
from routing.router import Route, MoveAction
from system.system_map import SystemMap
from workflow_models.workflow import Workflow
from workflow_models.workflow_templates import WorkflowTemplate


class RouteManager:
    def __init__(self, workflow: Workflow, system: SystemMap) -> None:
        self._workflow = workflow
        self._system_map = system

    def _get_routes(self) -> List[Route]:
        routes: List[Route] = []
        for _, thread in self._workflow.labware_threads.items():
            builder = RouteBuilder(thread, self._system_map)
            route = builder.get_route()
            routes.append(route)
        return routes

    def generate_next_step(self) -> Iterator[MoveAction]:
        # TODO: add logic to determine the next step
        routes = self._get_routes()
        yield routes[0].actions[0]




class WorkflowExecuter:
    def __init__(self, workflow: WorkflowTemplate, system_map: SystemMap) -> None:
        self._workflow = workflow
        self._system_map = system_map

    def execute(self) -> None:
        route_manager = RouteManager(self._workflow, self._system_map)
        for step in route_manager.generate_next_step():
            step.execute()