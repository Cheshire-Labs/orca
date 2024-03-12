from routing.router import Route
from routing.system_graph import SystemGraph
from workflow_models.workflow import LabwareThread


class RouteBuilder:
    def __init__(self, thread: LabwareThread, system: SystemGraph) -> None:
        self._system = system
        self._thread = thread

    def _get_base_route(self) -> Route:
        route = Route(self._thread.start_location, self._thread.end_location)
        for method in self._thread.methods:
            for action in method.actions:
                route.add_stop(action.location, action)
        return route

    def get_route(self) -> Route:
        route = self._get_base_route()
        route.build_route(self._system)
        return route