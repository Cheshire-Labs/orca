from typing import Dict, Optional
from resource_models.labware import AnyLabware, Labware, LabwareTemplate
from resource_models.location import Location
from system.registries import ThreadManager
from system.system_map import SystemMap
from system.labware_registry_interfaces import ILabwareRegistry
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
        self._thread_manager = ThreadManager()

        self._validate_labware_location_mappings()
        labware_dict = self._create_input_labware_instance(selected_any_labware)
        self._create_labware_threads(labware_dict)
        self._method = self._template.get_instance(self._labware_registry)
        self._apply_method_to_labware_threads()

    def _validate_labware_location_mappings(self) -> None:
        # validate that each labware in the expected inputs is in the start_map
        for labware_template in self._template.inputs:
            if labware_template.name not in self._start_mapping:
                raise ValueError(f"Labware {labware_template.name} is expected as an input but its starting location is not in the start_map")
        
        # validate that each labware in the expected outputs is in the end_map
        for labware_template in self._template.outputs:
            if labware_template.name not in self._end_mappping:
                raise ValueError(f"Labware {labware_template.name} is expected as an output but its ending location is not in the end_map")
    
    def _create_input_labware_instance(self, selected_any_labware: Optional[LabwareTemplate] = None) -> Dict[str, Labware]:
        labware_mapping = {}
        for labware_template in self._template.inputs:
            # TODO: re-examine the logic for this $any labware wildcard once the workflow executor is written
            labware = labware_template.create_instance()
            if isinstance(labware, AnyLabware):
                if selected_any_labware is None:
                    raise ValueError(f"Labware is of type AnyLabware, the selected_any_labware parameter must be provided.")
                labware = selected_any_labware.create_instance()
            else:
                self._labware_registry.add_labware(labware)
            labware_mapping[labware_template.name] = labware
        return labware_mapping

    def _create_labware_threads(self, labware_mapping: Dict[str, Labware]) -> None:
        for labware_template in self._template.inputs:
            labware = labware_mapping[labware_template.name]
            thread = LabwareThread(labware.name,
                                    labware,
                                    self._start_mapping[labware.name],
                                    self._end_mappping[labware.name],
                                    self._system_map)
            thread.initialize_labware()
            self._thread_manager.add_thread(thread)

    def _apply_method_to_labware_threads(self):
        for thread in self._thread_manager.threads:
            thread.append_method_sequence(self._method)

    def execute(self):
        self._thread_manager.execute()



