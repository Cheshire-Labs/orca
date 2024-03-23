from routing.router import Route
from routing.system_graph import SystemGraph
from workflow_models.workflow import LabwareThread


# class RouteBuilder:
#     def __init__(self, thread: LabwareThread, system: SystemGraph) -> None:
#         self._system = system
#         self._thread = thread

#     def _get_base_route(self) -> Route:
#         route = Route(self._thread.start_location, self._thread.end_location)
#         for name, method in self._thread.methods.items():
#             for action in method.steps:
#                 previous_location = route.path[-1]

#                 resource = action.resource
#                 location = self._system.get_resource_location(action.resource.name)
#                 route.add_stop(location, action)
#         return route

#     def get_route(self) -> Route:
#         route = self._get_base_route()
#         route.extend_to_location(self._system)
#         return route