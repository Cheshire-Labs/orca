from typing import Dict
import uuid
from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.system.labware_registry_interfaces import ILabwareRegistry
from orca.system.thread_manager import IThreadManager
from orca.workflow_models.spawn_thread_action import SpawnThreadAction
from orca.workflow_models.workflow_templates import JunctionMethodTemplate, MethodTemplate, ThreadTemplate

class MethodExecutor:
    def __init__(self, 
                 template: MethodTemplate, 
                 labware_start_mapping: Dict[LabwareTemplate, Location], 
                 labware_end_mapping: Dict[LabwareTemplate, Location],
                 thread_manager: IThreadManager,
                 labware_registry: ILabwareRegistry) -> None: 
        self._id: str = str(uuid.uuid4())
        self._method_template = template

        # TODO:  Obviously an atrocious fix here to remove any spawn thread actions from the method observers
        # TODO: needs to be replaced after refactoring the registry to make a distinction between 
        # stand alone method template definitonss and method templates defined as part of workflows
        replacement_observers = [o for o in self._method_template._method_observers if not isinstance(o, SpawnThreadAction)]
        self._method_template._method_observers = replacement_observers

        self._start_mapping = labware_start_mapping
        self._end_mapping = labware_end_mapping
        self._thread_manager = thread_manager
        self._labware_registry = labware_registry
        self._validate_labware_location_mappings()
        self._create_labware_threads()

    def _validate_labware_location_mappings(self) -> None:
        # simple check that the AnyLabware wildcard is satisfied
        if len(self._start_mapping) != len(self._method_template.inputs):
            raise ValueError(f"Number of labware in the start_map does not match the number of expected inputs")
    
        if len(self._end_mapping) != len(self._method_template.outputs):
            raise ValueError(f"Number of labware in the end_map does not match the number of expected outputs")
        
        # validate that each concrete labware template is in the maps
        for labware_template in self._method_template.inputs:
            if isinstance(labware_template, LabwareTemplate) and labware_template not in self._start_mapping.keys():
                raise ValueError(f"Labware {labware_template.name} is expected as an input but its starting location is not in the start_map")
        
        for labware_template in self._method_template.outputs:
            if isinstance(labware_template, LabwareTemplate) and labware_template not in self._end_mapping.keys():
                raise ValueError(f"Labware {labware_template.name} is expected as an output but its ending location is not in the end_map")

    def _create_labware_threads(self) -> None:
        # TODO: mappings won't work here for labwares that end or start within a method action
        method = self._method_template.get_instance(self._labware_registry)
        
        for idx, labware_template in enumerate(self._start_mapping.keys()):
            thread_template = ThreadTemplate(labware_template, 
                                             self._start_mapping[labware_template],
                                             self._end_mapping[labware_template])
            thread_template.add_method(JunctionMethodTemplate())
            thread_template.set_wrapped_method(method)
            thread = self._thread_manager.create_thread_instance(thread_template)
            
    @property
    def id(self) -> str:
        return self._id
    
    async def start(self):
        await self._thread_manager.start_all_threads()



