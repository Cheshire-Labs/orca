from typing import Any, Dict, List, Optional
from resource_models.transporter_resource import TransporterResource
from resource_models.location import Location
from resource_models.labware import Labware
from routing.system_graph import SystemGraph
from workflow_models.action import BaseAction


class MoveAction(BaseAction):
    def __init__(self, 
                 source: Location, 
                 target: Location, 
                 transporter: TransporterResource) -> None:
        super().__init__()
        self._source = source
        self._target = target
        self._transporter: TransporterResource = transporter
        # self._post_load_actions: List[ResourceAction] = []
        self._labware: Optional[Labware] = None

    @property
    def source(self) -> Location:
        return self._source
    
    # @source.setter
    # def source(self, source:Location) -> None:
    #     self._source = source

    @property
    def target(self) -> Location:
        return self._target
    
    # @target.setter
    # def target(self, target: Location) -> None:
    #     self._target = target

    # def set_actions(self, actions: List[ResourceAction]) -> None:
    #     self._post_load_actions = actions

    def set_labware(self, labware: Labware) -> None:
        self._labware = labware
    
    def _perform_action(self) -> None:
        if self._labware is None:
            raise ValueError("Labware must be set before getting actions")
        # move the labware
        self._source.prepare_for_pick(self._labware)
        self._transporter.pick(self._source)
        self._source.notify_picked(self._labware)
        self._target.prepare_for_place(self._labware)
        self._transporter.place(self._target)
        self._target.notify_placed(self._labware)

        # # perform the action
        # for action in self._post_load_actions:
        #     action.execute()

    


class Route:
    def __init__(self, start: Location, system_graph: SystemGraph, end: Optional[Location] = None) -> None:
        self._start = start
        self._end = start
        self._edges: List[MoveAction] = []
        self._system_graph = system_graph
        if end is not None:
            self.extend_to_location(end)
        # self._core_actions: Dict[str, List[ResourceAction]] = {}
        
    @property
    def start(self) -> Location:
        return self._start

    @property
    def path(self) -> List[Location]:
        path = [self._start]
        for edge in self._edges:
            path.append(edge.target) 
        return path
    
    # @property
    # def actions(self) -> List[MoveAction]:
    #     return self._edges

    # def add_stop(self, location: Location, action: ResourceAction) -> None:
    #     if location not in self._core_actions.keys():
    #         self._core_actions[location.teachpoint_name] = []
    #     self._core_actions[location.teachpoint_name].append(action)

   

    def extend_to_location(self, end_location: Location) -> None:
        """
        Extends the route to the specified end location by adding move actions to the route.

        Args:
            end_location (Location): The end location to extend the route to.
            system (SystemGraph): The system graph containing the locations and transporters.

        Returns:
            None
        """
        previous_end = self._end
        new_end = end_location
        extended_path = self._system_graph.get_shortest_available_path(previous_end.teachpoint_name, new_end.teachpoint_name)

        end_path_src_loc = previous_end.teachpoint_name      
        for stop in extended_path:
            source_location = self._system_graph.locations[end_path_src_loc]
            target_location = self._system_graph.locations[stop]
            transporter = self._system_graph.get_transporter_between(end_path_src_loc, stop)
            self._edges.append(MoveAction(source_location, target_location, transporter))
            end_path_src_loc = stop
        self._end = end_location
    
    def __iter__(self):
        return iter(self._edges)
 