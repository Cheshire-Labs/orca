
from dataclasses import dataclass
from typing import Dict, List

from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate


@dataclass
class SpawnInfo:
    spawn_thread: ThreadTemplate
    parent_thread: ThreadTemplate
    parent_method: MethodTemplate

@dataclass
class JoinInfo:
    parent_thread: ThreadTemplate
    attaching_thread: ThreadTemplate
    parent_method: MethodTemplate

class WorkflowTemplate:
    
    def __init__(self, name: str) -> None:
        self._name = name
        self._start_threads: Dict[str, ThreadTemplate] = {}
        self._threads: Dict[str, ThreadTemplate] = {}
        self._joints: List[JoinInfo] = []
        self._spawns: List[SpawnInfo] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def thread_templates(self) -> List[ThreadTemplate]:
        return list(self._threads.values())

    @property
    def start_thread_templates(self) -> List[ThreadTemplate]:
        return list(self._start_threads.values())
    
    @property
    def joints(self) -> List[JoinInfo]:
        return self._joints
    
    @property
    def spawns(self) -> List[SpawnInfo]:
        return self._spawns
    
    def add_thread(self, thread: ThreadTemplate, is_start: bool = False) -> None:
        self._threads[thread.name] = thread
        if is_start:
            self._start_threads[thread.name] = thread

    def join(self, thread: ThreadTemplate, to: ThreadTemplate, at: MethodTemplate) -> None:
        if thread.name not in self._threads:
            raise ValueError(f"Thread {thread.name} not found in workflow")
        if to.name not in self._threads:
            raise ValueError(f"Thread {to.name} not found in workflow")
        self._joints.append(JoinInfo(to, thread, at))
    
    def set_spawn_point(self, spawn_thread: ThreadTemplate, from_thread: ThreadTemplate, at: MethodTemplate) -> None:
        if spawn_thread.name not in self._threads:
            raise ValueError(f"Thread {spawn_thread.name} not found in workflow")
        if from_thread.name not in self._threads:
            raise ValueError(f"Thread {from_thread.name} not found in workflow")
        self._spawns.append(SpawnInfo(spawn_thread, from_thread, at))