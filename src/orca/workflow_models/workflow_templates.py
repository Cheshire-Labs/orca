
from dataclasses import dataclass
from typing import Dict, List

from orca.events.event_bus_interface import EventHandlerType
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate


@dataclass
class SpawnInfo:
    spawn_thread: ThreadTemplate
    parent_thread: ThreadTemplate
    parent_method: MethodTemplate
    join: bool = False

@dataclass
class EventHookInfo:
    event_name: str
    handler: EventHandlerType

class WorkflowTemplate:
    
    def __init__(self, name: str) -> None:
        self._name = name
        self._start_threads: Dict[str, ThreadTemplate] = {}
        self._threads: Dict[str, ThreadTemplate] = {}
        self._spawns: List[SpawnInfo] = []
        self._event_hooks: List[EventHookInfo] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def thread_templates(self) -> List[ThreadTemplate]:
        return list(self._threads.values())

    @property
    def entry_thread_templates(self) -> List[ThreadTemplate]:
        return list(self._start_threads.values())

    @property
    def event_hooks(self) -> List[EventHookInfo]:
        return self._event_hooks
    
    @property
    def spawns(self) -> List[SpawnInfo]:
        return self._spawns
    
    def add_thread(self, thread: ThreadTemplate, is_start: bool = False) -> None:
        self._threads[thread.name] = thread
        if is_start:
            self._start_threads[thread.name] = thread

    
    def set_spawn_point(self, spawn_thread: ThreadTemplate, from_thread: ThreadTemplate, at: MethodTemplate, join: bool = False) -> None:
        if spawn_thread.name not in self._threads:
            raise ValueError(f"Thread {spawn_thread.name} not found in workflow")
        if from_thread.name not in self._threads:
            raise ValueError(f"Thread {from_thread.name} not found in workflow")
        self._spawns.append(SpawnInfo(spawn_thread, from_thread, at, join))

    def add_event_hook(self, event_name: str, handler: EventHandlerType) -> None:
        self._event_hooks.append(EventHookInfo(event_name, handler))