from routing.router import Route
from routing.system_graph import SystemGraph
from workflow_models.workflow import LabwareThread


class RouteBuilder:
    def __init__(self, thread: LabwareThread, system: SystemGraph) -> None:
        self._system = system
        self._thread = thread

    def _get_base_route(self) -> Route:
        route = Route(self._thread.start_location, self._thread.end_location)
        for name, method in self._thread.methods.items():
            for resolver in method.action_resolvers:
                previous_location = route.path[-1]
                action = resolver.get_best_action(previous_location, self._system)
                location = self._system.get_resource_location(action.resource.name)
                route.add_stop(location, action)
        return route

    def get_route(self) -> Route:
        route = self._get_base_route()
        route.build_route(self._system)
        return route