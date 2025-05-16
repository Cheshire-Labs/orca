from abc import ABC
from typing import Any, Dict, List, Optional
from orca_driver_interface.driver_interfaces import IDriver

from orca import sdk
from orca.system.system import ISystem
from orca.resource_models.transporter_resource import ITransporterDriver
from orca.workflow_models.labware_thread import IThreadObserver

class IEventHandler(ABC):
    def set_system(self, system: ISystem) -> None:
        self.system = system


class ThreadEventHandler(IEventHandler, IThreadObserver):
    def set_system(self, system: ISystem) -> None:
        self.system = system

class Labware:
    def __init__(self, name: str, type: str) -> None:
        self.name = name
        self.type = type

class AnyLabware:
    def __init__(self) -> None:
        pass

class Equipment:
    def __init__(self, name: str, driver: IDriver) -> None:
        self.name = name
        self.driver = driver

class Transporter(Equipment):
    def __init__(self, name: str, driver: ITransporterDriver) -> None:
        super().__init__(name, driver)
        self.driver: ITransporterDriver = driver

class EquipmentPool:
    def __init__(self, name: str, resources: List[Equipment] = []) -> None:
        self.name = name
        self.resources = resources

class Location:
    def __init__(self, name: str, resource: Optional[Equipment]) -> None:
        self.name = name
        self.resource = resource


class Action:
    def __init__(self, resource: Equipment | EquipmentPool, command: str, inputs: List[Labware | AnyLabware], outputs: Optional[List[Labware | AnyLabware]] = None, options: Optional[Dict[str, Any]] = None) -> None:
        self.resource = resource
        self.command = command
        self.inputs = inputs
        self.outputs = outputs if outputs is not None else []
        self.options = options if options is not None else {}

class Method:
    def __init__(self, name: str, actions: Optional[List[Action]] = None) -> None:
        self.name = name
        self.actions = actions if actions is not None else []

class WrapperMethod:
    def __init__(self) -> None:
        pass

class Thread:
    def __init__(self, labware: Labware) -> None:
        self.labware = labware
        self.start: str
        self.end: str
        self.steps: List[Method | WrapperMethod] = []

    def add_step(self, step: Method | WrapperMethod) -> None:
        self.steps.append(step)

    def add_steps(self, steps: List[Method | WrapperMethod]) -> None:
        for step in steps:
            self.add_step(step)

    def set_start(self, start: str) -> None:
        self.start = start

    def set_end(self, end: str) -> None:
        self.end = end

class Joint:
    def __init__(self, parent: Thread, child: Thread, joint_method: Method) -> None:
        self.parent = parent
        self.child = child
        self.joint_method = joint_method

class SpawnPoint:
    def __init__(self, spawn_thread: Thread, parent: Thread, spawning_method: Method) -> None:
        self.spawn_thread = spawn_thread
        self.parent = parent
        self.spawning_method = spawning_method

class EventPoint:
    def __init__(self, thread: Thread, event_handler: ThreadEventHandler) -> None:
        self.thread = thread
        self.event_handler = event_handler

class Workflow:
    def __init__(self, name: str, threads: List[Thread] = []) -> None:
        self.name = name
        self.threads: List[Thread] = threads
        self._joints: List[Joint] = []
        self._spawn_points: List[SpawnPoint] = []
        self._event_handlers: List[EventPoint] = []
    
    def add_thread(self, thread: Thread) -> None:
        self.threads.append(thread)
    
    def add_threads(self, threads: List[Thread]) -> None:
        for thread in threads:
            self.add_thread(thread)
    
    def get_method(self, name: str) -> Method:
        raise NotImplementedError("Method not implemented")

    def join(self, thread: Thread, to: Thread, at: Method, set_spawn: bool = False) -> None:
        self._joints.append(Joint(to, thread, at))
        if set_spawn:
            self.set_spawn(thread, to, at)

    def set_spawn(self, spawn_thread: Thread, from_thread: Thread, at: Method) -> None:
        self._spawn_points.append(SpawnPoint(spawn_thread, from_thread, at))

    def set_thread_event_handler(self, attach_to: Thread, event_handler: ThreadEventHandler):
        self._event_handlers.append(EventPoint(attach_to, event_handler))


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
    
    def build_from_transporters(self, transporters: List[Transporter]) -> None:
        pass

    def assign_resources(self, resources: Dict[str, Equipment]) -> None:
        pass

    def get_locations(self) -> List[Location]:
        return [loc for loc in self._locations.values()]



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