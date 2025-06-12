from dataclasses import dataclass
from typing import Optional, Union


@dataclass(frozen=True)
class WorkflowExecutionContext:
    workflow_id: str
    workflow_name: str


@dataclass(frozen=True)
class ThreadExecutionContext(WorkflowExecutionContext):
    thread_id: str
    thread_name: str


@dataclass(frozen=True)
class MethodExecutionContext(WorkflowExecutionContext):
    method_id: Optional[str]
    method_name: Optional[str]


@dataclass(frozen=True)
class LocationActionExecutionContext(MethodExecutionContext):
    action_id: str
    action_status: str
    action_name: Optional[str] = None

@dataclass(frozen=True)
class MoveActionExecutionContext(ThreadExecutionContext):
    action_id: str
    action_status: str
    action_name: Optional[str] = None
    


ExecutionContext = Union[
    WorkflowExecutionContext,
    ThreadExecutionContext,
    MethodExecutionContext,
    LocationActionExecutionContext
]