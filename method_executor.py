from typing import Dict, List, Optional, Union
from resource_models.labware import AnyLabware, AnyLabwareTemplate, LabwareTemplate
from resource_models.location import Location
from routing.router import MoveAction
from system.system_map import SystemMap
from system.registry_interfaces import ILabwareRegistry
from workflow_models.method_action import LocationAction
from workflow_models.workflow import LabwareThread
from workflow_models.workflow_templates import MethodTemplate


class MethodExecutor:
    def __init__(self, 
                 template: MethodTemplate, 
                 labware_reg: ILabwareRegistry, 
                 labware_start_mapping: Dict[str, Location], 
                 labware_end_mapping: Dict[str, Location],
                 system_map: SystemMap, 
                 selected_any_labware: Optional[LabwareTemplate] = None) -> None:

        self._template = template
        self._labware_registry = labware_reg
        self._start_mapping = labware_start_mapping
        self._end_mappping = labware_end_mapping
        self._system_map = system_map
        self._threads: List[LabwareThread] = []
               
        labware_inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = self._template.inputs
        labware_outputs: List[LabwareTemplate] = self._template.outputs

        # validate that each labware in the expected inputs is in the start_map
        for labware_template in labware_inputs:
            if labware_template.name not in self._start_mapping:
                raise ValueError(f"Labware {labware_template.name} is expected as an input but its starting location is not in the start_map")
        
        # validate that each labware in the expected outputs is in the end_map
        for labware_template in labware_outputs:
            if labware_template.name not in self._end_mappping:
                raise ValueError(f"Labware {labware_template.name} is expected as an output but its ending location is not in the end_map")
            
        for labware_template in labware_inputs:
            # TODO: re-examine the logic for this $any labware wildcard once the workflow executor is written
            labware = labware_template.create_instance()
            if isinstance(labware, AnyLabware):
                if selected_any_labware is None:
                    raise ValueError(f"Labware is of type AnyLabware, the selected_any_labware parameter must be provided.")
                labware = selected_any_labware.create_instance()
            else:
                self._labware_registry.add_labware(labware)

            thread = LabwareThread(labware.name,
                                    labware,
                                    self._start_mapping[labware.name],
                                    self._end_mappping[labware.name],
                                    self._system_map)
            thread.initialize_labware()
            self._threads.append(thread)

        self._method = self._template.get_instance(self._labware_registry)

    def execute(self):
        
        for thread in self._threads:
            current_location = thread.current_location
            while not self._method.has_completed():
                next_action = self._method.resolve_next_action(current_location, self._system_map)
                thread.execute_action(next_action)
            # send the labware to end location
            thread.send_to_end_location()
            
            