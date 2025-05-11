from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from orca_driver_interface.driver_interfaces import IDriver

class Labware:
    def __init__(self, name: str, type: str) -> None:
        self.name = name
        self.type = type


class Equipment:
    def __init__(self, name: str, driver: IDriver) -> None:
        self.name = name
        self.driver = driver

class EquipmentPool:
    def __init__(self, name: str, resources: List[Equipment] = []) -> None:
        self.name = name
        self.resources = resources

class Location:
    def __init__(self, name: str, resource: Optional[Equipment]) -> None:
        self.name = name
        self.resource = resource


class Action:
    def __init__(self, resource: Equipment | EquipmentPool, command: str, inputs: List[Labware], outputs: Optional[List[Labware]] = None, options: Optional[Dict[str, Any]] = None) -> None:
        self.resource = resource
        self.command = command
        self.inputs = inputs
        self.outputs = outputs if outputs is not None else []
        self.options = options if options is not None else {}

class Method:
    def __init__(self, name: str, actions: Optional[List[Action]] = None) -> None:
        self.name = name
        self.actions = actions if actions is not None else []

class Thread:
    def __init__(self, labware: Labware) -> None:
        self.labware = labware
        self.start: str
        self.end: str
        self.steps: List[Method] = []

    def add_step(self, step: Method) -> None:
        self.steps.append(step)

    def add_steps(self, steps: List[Method]) -> None:
        for step in steps:
            self.add_step(step)

    def set_start(self, start: str) -> None:
        self.start = start

    def set_end(self, end: str) -> None:
        self.end = end


class Workflow:
    def __init__(self, name: str, threads: List[Thread] = []) -> None:
        self.name = name
        self.threads: List[Thread] = threads
    
    def add_thread(self, thread: Thread) -> None:
        self.threads.append(thread)
    
    def add_threads(self, threads: List[Thread]) -> None:
        for thread in threads:
            self.add_thread(thread)
    
    def get_method(self, name: str) -> Method:
        raise NotImplementedError("Method not implemented")

    def join(self, primary: Thread, participants: List[Thread]) -> None:
        pass


class LabwareRegistry:
    def __init__(self, labwares: Dict[str, Labware] = {}) -> None:
        self._labwares: Dict[str, Labware] = labwares

    def add_labware(self, name:str, labware: Labware) -> None:
        self._labwares[name] = labware

    def get_labware(self, name: str) -> Labware:
        return self._labwares[name]
    
    def add_labwares(self, labwares: Dict[str, Labware]) -> None:
        for name, labware in labwares.items():
            self.add_labware(name, labware)


class ResourceRegistry:
    def __init__(self, resources: Dict[str, Equipment] = {}) -> None:
        self._resources: Dict[str, Equipment] = resources

    def add_resource(self, name:str, resource: Equipment) -> None:
        self._resources[name] = resource

    def get_resource(self, name: str) -> Equipment:
        return self._resources[name]
    
    def add_resources(self, resources: Dict[str, Equipment]) -> None:
        for name, resource in resources.items():
            self.add_resource(name, resource)


class LocationRegistry:
    def __init__(self, locations: Dict[str, Location] = {}) -> None:
        self._locations: Dict[str, Location] = locations

    def get_location(self, name: str) -> Location:
        return self._locations[name]
    
    def build_from_transporters(self, transportes) -> None:
        pass

    def assign_resources(self, resources: Dict[str, Equipment]) -> None:
        pass




class MethodRegistry:
    def __init__(self) -> None:
        self._methods: Dict[str, Method] = {}

    def add_method(self, name: str, method: Method) -> None:
        self._methods[name] = method

    def get_method(self, name: str) -> Method:
        return self._methods[name]

    def add_methods(self, methods: Dict[str, Method]) -> None:
        for name, method in methods.items():
            self.add_method(name, method)


class WorkflowRegistry:
    def __init__(self) -> None:
        self._workflows: Dict[str, Workflow] = {}

    def add_workflow(self, name: str, workflow: Workflow) -> None:
        self._workflows[name] = workflow

    def get_workflow(self, name: str) -> Workflow:
        return self._workflows[name]

    def add_workflows(self, workflows: Dict[str, Workflow]) -> None:
        for name, workflow in workflows.items():
            self.add_workflow(name, workflow)