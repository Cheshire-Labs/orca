from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from resource_models.location import Location
from resource_models.labware import Labware
from workflow_models.action import ActionStatus, BaseAction
from workflow_models.method_status import MethodStatus
from workflow_models.workflow_templates import LabwareThreadTemplate, MethodActionTemplate, MethodTemplate, WorkflowTemplate


class MethodAction(BaseAction):
    @staticmethod
    def from_template(action_template: MethodActionTemplate, labwares: Dict[str, Labware]) -> MethodAction:
        """
        Builds a MethodActionInstance object based on the provided template and labware instance inputs.
        If the output labware is also an input labware in the template, the output will be set to the corresponding input LabwareInstance.
        Otherwise, a new LabwareInstance will be created.
        """
        
        labware_instance_inputs: List[Labware] = [labwares[input_template.name] for input_template in action_template.inputs]
        labware_instance_outputs: List[Labware] = []
        for template_output in action_template.outputs:
            output = next((labware for labware in labware_instance_inputs if labware.name == template_output.name), Labware(template_output.name, template_output.labware_type))
            labware_instance_outputs.append(output)
        
        instance = MethodAction(action_template.location, action_template.command, labware_instance_inputs, labware_instance_outputs, action_template.options)
        return instance

    def __init__(self, 
                 location: Location,
                 command: str, 
                 labware_instance_inputs: List[Labware], 
                 labware_instance_outputs: List[Labware], 
                 options: Dict[str, Any] = {}) -> None:
        self._location: Location = location
        self._command: str = command
        self._options: Dict[str, Any] = options
        self._awaiting_labware_inputs: List[Labware] = labware_instance_inputs
        self._loaded_labware_inputs: List[Labware] = []
        self._outputs: List[Labware] = labware_instance_outputs
        self._status: ActionStatus = ActionStatus.CREATED
    
    @property
    def location(self) -> Location:
        return self._location
    
    @property
    def awaiting_labware_inputs(self) -> List[Labware]:
        return self._awaiting_labware_inputs
    
    def load_labware(self, labware: Labware) -> None:
        if labware not in self._awaiting_labware_inputs:
            if labware in self._loaded_labware_inputs:
                raise ValueError(f"Labware is not pending. Labware {labware} has already been assigned to this method")
            raise ValueError(f"Labware is not pending. Labware {labware} is not accepted by this method")
        self._awaiting_labware_inputs.remove(labware)
        self._loaded_labware_inputs.append(labware)
        if self._awaiting_labware_inputs == []:
            self._status = ActionStatus.READY
        self._location.prepare_for_place(labware)

    def unload_labware(self, labware: Labware) -> None:
        if labware not in self._outputs:
            raise ValueError(f"Labware is not an output of this method. Labware {labware} is not available to be unloaded")
        self._location.prepare_for_pick(labware)
        self._outputs.remove(labware)

    def _perform_action(self) -> None:
        if self._location.resource is None:
            raise ValueError(f"Location {self._location} does not have a resource assigned")
        self._location.resource.set_command(self._command)
        self._location.resource.set_command_options(self._options)
        self._status = ActionStatus.IN_PROGRESS
        self._location.resource.execute()
        self._status = ActionStatus.COMPLETED



class Method:
    @staticmethod
    def from_template(template: MethodTemplate, labwares: Dict[str, Labware]) -> Method:
        actions = [MethodAction.from_template(action_template, labwares) for action_template in template.actions]
        return Method(template.name, actions)

    def __init__(self, name: str, actions: List[MethodAction]) -> None:
        self._name = name
        self._actions: List[MethodAction] = actions
        self._status = MethodStatus.CREATED
        self._parent_plate_thread_id: Optional[str] = None

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def actions(self) -> List[MethodAction]:
        return self._actions
    
    @property
    def status(self) -> MethodStatus:
        if all(action.status == MethodStatus.CANCELED for action in self._actions):
            return MethodStatus.CANCELED
        elif all(action.status in [MethodStatus.COMPLETED, MethodStatus.CANCELED] for action in self._actions):
            return MethodStatus.COMPLETED
        elif any(action.status == MethodStatus.ERRORED for action in self._actions):
            return MethodStatus.ERRORED
        elif any(action.status == MethodStatus.PAUSED for action in self._actions):
            return MethodStatus.PAUSED
        elif any(action.status == MethodStatus.AWAITING_RESOURCES for action in self._actions):
            return MethodStatus.AWAITING_RESOURCES
        elif any(action.status == MethodStatus.RUNNING for action in self._actions):
            return MethodStatus.RUNNING
        elif all(action.status == MethodStatus.READY for action in self._actions):
            return MethodStatus.READY
        elif all(action.status == MethodStatus.QUEUED for action in self._actions):
            return MethodStatus.QUEUED
        else:
            return MethodStatus.CREATED

    def append_action(self, action: MethodAction):
        self._actions.append(action)
    
    def get_next_action(self) -> Optional[MethodAction]:
        return next((action 
                     for action in self._actions 
                     if action.status in [MethodStatus.AWAITING_RESOURCES, MethodStatus.READY, MethodStatus.QUEUED]), None)


class LabwareThread:
    @staticmethod
    def from_template(template: LabwareThreadTemplate, labwares: Dict[str, Labware]) -> LabwareThread:
        method_seq = [Method.from_template(method, labwares) for method in template.methods]
        labware_name = template.labware.name
        thread = LabwareThread(labware_name, labwares[labware_name], method_sequence=method_seq, start_location=template.start, end_location=template.end)
        thread.set_start_location(template.start)
        thread.set_end_location(template.end)
        return thread

    def __init__(self, name: str, labware: Labware, method_sequence: List[Method], start_location: Location, end_location: Location) -> None:
        self._name: str = name
        self._labware: Labware = labware
        self._method_seq: List[Method] = method_sequence
        self._start_location: Location = start_location
        self._current_location: Location = self._start_location
        self._end_location: Location = end_location
        self._status: MethodStatus = MethodStatus.CREATED

    @property
    def name(self) -> str:
        return self._name

    @property
    def start_location(self) -> Location:
        return self._start_location
    
    def set_start_location(self, location: Location) -> None:
        if self._status in [MethodStatus.CREATED]:
            self._current_location = self._start_location
        self._start_location = location
    
    @property
    def end_location(self) -> Location:
        if self._end_location is None:
            raise ValueError("End location has not been set")
        return self._end_location
    
    def set_end_location(self, location: Location) -> None:
        self._end_location = location
    
    @property
    def current_location(self) -> Location:
        return self._current_location

    @property
    def methods(self) -> List[Method]:
        return self._method_seq
    
    @property
    def completed_methods(self) -> List[Method]:
        return [method for method in self._method_seq if method.status == MethodStatus.COMPLETED]
    
    @property
    def current_method(self) -> Method:
        return next((step for step in self._method_seq if step.status in [MethodStatus.RUNNING]))

    @property
    def queued_methods(self) -> List[Method]:
        return [method for method in self._method_seq if method.status == MethodStatus.QUEUED]
    
    @property
    def labware(self) -> Labware:
        return self._labware
    
    @property
    def completed_actions(self) -> List[BaseAction]:
        raise NotImplementedError

    @property
    def current_action(self) -> BaseAction:
        raise NotImplementedError

    @property
    def queued_actions(self) -> List[BaseAction]:
        raise NotImplementedError
    
    def _build_actions(self, method: MethodTemplate) -> List[BaseAction]:
        raise NotImplementedError

    def is_completed(self) -> bool:
        return all(method.status in [MethodStatus.COMPLETED, MethodStatus.CANCELED] for method in self._method_seq)

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def get_next_action(self) -> BaseAction:
        raise NotImplementedError

    


class Workflow:
    @staticmethod
    def from_template(template: WorkflowTemplate, labwares: Dict[str, Labware]) -> Workflow:
        threads = {thread_name: LabwareThread.from_template(thread_template, labwares) for thread_name, thread_template in template.labware_threads.items()}
        workflow = Workflow(template.name, threads)
        return workflow

    def __init__(self, name:str, threads: Dict[str, LabwareThread]) -> None:
        self._name = name
        self._labware_threads:  Dict[str, LabwareThread] = threads

    @property
    def labware_threads(self) ->  Dict[str, LabwareThread]:
        return self._labware_threads



