
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
    """ Creates a template for a workflow. A workflow is a collection of threads and defines their interactions."""
    def __init__(self, name: str) -> None:
        self._name = name
        self._start_threads: Dict[str, ThreadTemplate] = {}
        self._threads: Dict[str, ThreadTemplate] = {}
        self._spawns: List[SpawnInfo] = []
        self._event_hooks: List[EventHookInfo] = []
        """ Initializes a WorkflowTemplate instance.
        Args:
            name (str): The name of the workflow template.
        """    

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
        """ Adds a thread to the workflow.
        Args:
            thread (ThreadTemplate): The thread template to add to the workflow.
            is_start (bool): If True, the thread will be started when the workflow starts. Defaults to False.
        """
        self._threads[thread.name] = thread
        if is_start:
            self._start_threads[thread.name] = thread

    
    def set_spawn_point(self, spawn_thread: ThreadTemplate, from_thread: ThreadTemplate, at: MethodTemplate, join: bool = False) -> None:
        """ Sets a spawn point in the workflow.
        Args:
            spawn_thread (ThreadTemplate): The thread that will be spawned.
            from_thread (ThreadTemplate): The thread from which the spawn occurs.
            at (MethodTemplate): The method at which the spawn occurs.  The spawn will occur when the parent thread reaches this method and when the method emits the CREATED event.
            join (bool): If True, the spawned thread will be joined to the parent thread at the spawned thread's JunctionMethod. Defaults to False.
        """
        if spawn_thread.name not in self._threads:
            raise ValueError(f"Thread {spawn_thread.name} not found in workflow")
        if from_thread.name not in self._threads:
            raise ValueError(f"Thread {from_thread.name} not found in workflow")
        self._spawns.append(SpawnInfo(spawn_thread, from_thread, at, join))

    def add_event_handler(self, event_name: str, handler: EventHandlerType) -> None:
        """ Adds an event hook to the workflow.
        Args:
            event_name (str): The name of the event to subscribe to.  
            handler (EventHandlerType): The handler function to call when the event occurs.
        """
        self._event_hooks.append(EventHookInfo(event_name, handler))