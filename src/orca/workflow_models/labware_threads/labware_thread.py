from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Callable, List
from orca.resource_models.location import Location
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.events.execution_context import ExecutionContext
from orca.workflow_models.interfaces import ILabwareThread, IMethod
from orca.workflow_models.method import MethodInstance
from orca.workflow_models.thread_template import ThreadFunc, ThreadTemplate

from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.status_enums import ActionStatus
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission_modes import BatchMode

orca_logger = logging.getLogger("orca")


class LabwareThreadInstance(ILabwareThread):
    def __init__(self,
                 labware: LabwareInstance,
                 start_location: Location,
                 end_locations: list[Location],
                 run_mode: WorkflowRunMode,
                 group_id: str | None = None,
                 submission_id: str | None = None,
                 batch_mode: BatchMode = BatchMode.STANDALONE,
                 ) -> None:
        self._labware: LabwareInstance = labware
        self._start_location: Location = start_location
        self._end_locations: list[Location] = end_locations
        self._method_sequence: List[IMethod] = []
        self._yield_func: ThreadFunc | None = None
        self._labware_template: LabwareTemplate | None = None
        self._thread_template: ThreadTemplate | None = None
        self._shared_executing_method: ExecutingMethod | None = None
        self._group_id: str | None = group_id
        self._submission_id: str | None = submission_id
        self._batch_mode: BatchMode = batch_mode
        self._run_mode: WorkflowRunMode = run_mode
        # Set by `ExecutingLabwareThread.__init__` to its own
        # `_stop_event` so LIVE-mode spawn strategies can poll for
        # cooperative stop (review item L1).
        # Stays None for value-object construction outside an execution.
        self._stop_event: asyncio.Event | None = None

    @property
    def group_id(self) -> str | None:
        """Identifies which LabwareGroup this thread serves, or None if it
        is a cross-group (SHARED_ACROSS_GROUPS) thread or a pre-T6 thread."""
        return self._group_id

    def set_group_id(self, group_id: str | None) -> None:
        """Auto-spawned threads inherit group_id from the firing contributor.
        Set post-construction by the spawn callback."""
        self._group_id = group_id

    @property
    def submission_id(self) -> str | None:
        """Identifies the Submission that produced this thread. None for
        pre-T6 threads (e.g. no-submission paths)."""
        return self._submission_id

    def set_submission_id(self, submission_id: str | None) -> None:
        """Auto-spawned threads inherit submission_id from the firing contributor."""
        self._submission_id = submission_id

    @property
    def batch_mode(self) -> BatchMode:
        """Operator's BatchMode preference from the originating Submission.

        Controls whether BATCHABLE receivers this thread spawns via auto-spawn
        discover an existing cross-submission slot (JOIN_EXISTING) or get a
        submission-isolated slot (STANDALONE).
        """
        return self._batch_mode

    def set_batch_mode(self, batch_mode: BatchMode) -> None:
        """Auto-spawned threads inherit batch_mode from the firing contributor."""
        self._batch_mode = batch_mode

    @property
    def run_mode(self) -> WorkflowRunMode:
        """Resolved WorkflowRunMode the spawn-action seam reads to decide between
        sim-mode auto-fulfill and LIVE-mode operator wait.

        Stamped at construction from `Submission.run_mode` (entry threads) or
        inherited from the firing contributor via `set_run_mode` (auto-spawned
        threads).
        """
        return self._run_mode

    def set_run_mode(self, run_mode: WorkflowRunMode) -> None:
        """Auto-spawned threads inherit run_mode from the firing contributor."""
        self._run_mode = run_mode


    @property
    def id(self) -> str:
        return self._labware.id

    @property
    def template_name(self) -> str:
        return self._labware.template_name

    @property
    def name(self) -> str:
        # The labware instance name IS the thread's display name: one name
        # links orca, PLR and the driver deck, so a second shape would desync.
        return self._labware.name

    @property
    def start_location(self) -> Location:
        return self._start_location

    @start_location.setter
    def start_location(self, location: Location) -> None:
        self._start_location = location

    @property
    def end_locations(self) -> list[Location]:
        return list(self._end_locations)

    @end_locations.setter
    def end_locations(self, locations: list[Location]) -> None:
        self._end_locations = list(locations)

    @property
    def labware(self) -> LabwareInstance:
        return self._labware

    @property
    def stop_event(self) -> asyncio.Event | None:
        """Cooperative-stop signal, set by `ExecutingLabwareThread`.

        LIVE-mode spawn strategies (`ManualPlaceSpawn._acquire_live`,
        `ManualRemoveSpawn._dispose_live`) poll this alongside their
        slot/labware checks so `thread.stop()` actually halts a parked
        operator-wait. Without this seam the thread's main-loop stop
        check at `executing_labware_thread.py:710` is unreachable while
        the strategy is in its sleep loop, so `thread.stop()` would
        only take effect after the operator finally placed labware (or
        never, if the operator did not).

        None outside an executing context: value-object construction
        and pure unit tests do not need the cooperative stop seam.
        """
        return self._stop_event

    def set_stop_event(self, event: asyncio.Event) -> None:
        """Bind the cooperative-stop event from the owning
        `ExecutingLabwareThread`. Called once at execution start so
        the event reference outlives any single strategy call.
        """
        self._stop_event = event

    @property
    def methods(self) -> List[IMethod]:
        return list(self._method_sequence)

    def append_method_sequence(self, method: IMethod) -> None:
        self._method_sequence.append(method)

    @property
    def yield_func(self) -> ThreadFunc | None:
        return self._yield_func

    def set_yield_func(self, func: ThreadFunc) -> None:
        self._yield_func = func

    @property
    def labware_template(self) -> LabwareTemplate | None:
        return self._labware_template

    def set_labware_template(self, template: LabwareTemplate) -> None:
        self._labware_template = template

    @property
    def thread_template(self) -> ThreadTemplate | None:
        return self._thread_template

    def set_thread_template(self, template: ThreadTemplate) -> None:
        self._thread_template = template

    @property
    def shared_executing_method(self) -> ExecutingMethod | None:
        return self._shared_executing_method

    def set_shared_executing_method(self, method: ExecutingMethod) -> None:
        self._shared_executing_method = method


class IThreadObserver(ABC):
    @abstractmethod
    def thread_notify(self, event: str, thread: LabwareThreadInstance) -> None:
        raise NotImplementedError

class ThreadObserver(IThreadObserver):
    def __init__(self, callback: Callable[[str, LabwareThreadInstance], None]) -> None:
        self._callback = callback

    def thread_notify(self, event: str, thread: LabwareThreadInstance) -> None:
        self._callback(event, thread)

class IMethodObserver(ABC):
    @abstractmethod
    def method_notify(self, event: str, method: MethodInstance) -> None:
        raise NotImplementedError

class MethodObserver(IMethodObserver):
    def __init__(self, callback: Callable[[str, MethodInstance], None]) -> None:
        super().__init__()
        self._callback = callback

    def method_notify(self, event: str, method: MethodInstance) -> None:
        self._callback(event, method)
