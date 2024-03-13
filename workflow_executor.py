from typing import Dict, Iterator, List
from routing.route_builder import RouteBuilder
from routing.router import Route, RouteStep
from routing.system_graph import SystemGraph
from workflow_models.workflow import Workflow


class RouteManager:
    def __init__(self, workflow: Workflow, system: SystemGraph) -> None:
        self._workflow = workflow
        self._system_graph = system

    def _get_routes(self) -> List[Route]:
        routes: List[Route] = []
        for _, thread in self._workflow.labware_threads.items():
            builder = RouteBuilder(thread, self._system_graph)
            route = builder.get_route()
            routes.append(route)
        return routes

    def generate_next_step(self) -> Iterator[RouteStep]:
        # TODO: add logic to determine the next step
        routes = self._get_routes()
        yield routes[0].actions[0]




class WorkflowExecuter:
    def __init__(self, workflow: Workflow, system_graph: SystemGraph) -> None:
        self._workflow = workflow
        self._system_graph = system_graph

    def execute(self) -> None:
        route_manager = RouteManager(self._workflow, self._system_graph)
        for step in route_manager.generate_next_step():
            step.execute()