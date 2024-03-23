from typing import Dict, List, Optional, Union
from resource_models.labware import AnyLabware, AnyLabwareTemplate, Labware, LabwareTemplate
from resource_models.location import Location
from system import System
from workflow_models.workflow import LabwareThread
from workflow_models.workflow_templates import MethodTemplate


class MethodExecutor:
    def __init__(self, template: MethodTemplate, system: System, start_map: Dict[str, Location], end_map: Dict[str, Location], selected_any_labware: Optional[LabwareTemplate] = None) -> None:
        self._system = system
        self._template = template
        self._start_map = start_map
        self._end_map = end_map
        self._threads: List[LabwareThread] = []
               
        labware_inputs: List[Union[LabwareTemplate, AnyLabwareTemplate]] = self._template.inputs
        labware_outputs: List[LabwareTemplate] = self._template.outputs

        # validate that each labware in the expected inputs is in the start_map
        for labware_template in labware_inputs:
            if labware_template.name not in self._start_map:
                raise ValueError(f"Labware {labware_template.name} is expected as an input but its starting location is not in the start_map")
        
        # validate that each labware in the expected outputs is in the end_map
        for labware_template in labware_outputs:
            if labware_template.name not in self._end_map:
                raise ValueError(f"Labware {labware_template.name} is expected as an output but its ending location is not in the end_map")
            
        for labware_template in labware_inputs:
            # TODO: re-examine the logic for this $any labware wildcard once the workflow executor is written
            labware = labware_template.create_instance()
            if isinstance(labware, AnyLabware):
                if selected_any_labware is None:
                    raise ValueError(f"Labware is of type AnyLabware, the selected_any_labware parameter must be provided.")
                labware = selected_any_labware.create_instance()
            else:
                self._system.add_labware(labware)

            thread = LabwareThread(labware.name,
                                    labware,
                                    self._start_map[labware.name],
                                    self._end_map[labware.name],
                                    self._system.system_graph)
            thread.initialize_labware()
            self._threads.append(thread)

        self._method = self._template.get_instance(self._system)

    def execute(self):
        
        for thread in self._threads:
            current_location = thread.current_location
            next_action = self._method.resolve_next_action(current_location, self._system.system_graph)
            thread.execute_action(next_action)
            
            