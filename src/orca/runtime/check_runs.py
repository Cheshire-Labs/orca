"""A check run runs in a runtime of its own, over a store of its own.

The store is seeded with a copy of the plate record and the operation record at the
run's start, and kept after the run so it can be checked. Nothing is written back.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from orca.runtime.event_saves import EventSaves
from orca.gateway.in_process_client import InProcessClient
from orca.driver_management.device_factory_protocol import IDeviceDriverProvider
from orca.modes.device_modes import IDeviceModes
from orca.runtime.execution_record_service import IExecutionRecordService
from orca.runtime.incident_service import IIncidentService
from orca.events.emitter import IEventSink
from orca.resource_models.labware_service_interface import ILabwareService
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.errors import OtherRunModeActiveError
from orca.runtime.runtime_state import RuntimeState
from orca.runtime.server_events import ServerEvents
from orca.runtime.status_models import SubmissionSnapshot
from orca.runtime.stall_detector import SystemStallError
from orca.runtime.startup import (
    BuildStep,
    IActivityServices,
    RuntimeSettings,
    build_check_run_devices,
    start_runtime,
)
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.runtime.unsaved_record import IUnsavedRecordReporter
from orca.state.ops_store import IOpsHistoryStore, OpsHistorySearchQuery

logger = logging.getLogger(__name__)

# How long the end-of-run watcher waits before it looks again at a stalled check run.
_STALLED_RECHECK_SECONDS = 5.0

CHECK_RUNS_KEPT = 50


class ICheckRunStore(Protocol):
    """One check run's plates, incidents and operation record."""

    @property
    def labware(self) -> ILabwareService: ...

    @property
    def incidents(self) -> IIncidentService: ...

    @property
    def ops_history(self) -> IOpsHistoryStore: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class CheckRunEntry:
    run_id: str
    execution_ids: tuple[str, ...]


class ICheckRunStores(Protocol):
    """Where a server keeps its check runs' stores, one per check run."""

    async def create(self, run_id: str) -> ICheckRunStore:
        """A new, empty, opened store."""
        ...

    async def open(self, run_id: str) -> ICheckRunStore | None:
        """A kept store, opened for reading; None when there is none."""
        ...

    async def record_execution(self, run_id: str, execution_id: str) -> None:
        """Add an execution that ran on this store; adding one twice changes nothing."""
        ...

    async def entries(self) -> list[CheckRunEntry]:
        """Every kept check run, oldest first."""
        ...

    async def delete(self, run_id: str) -> None: ...


class ICheckRunBuildSteps(Protocol):
    def __call__(self, ops_history: IOpsHistoryStore, device_factory: IDeviceDriverProvider) -> BuildStep:
        """The server's package build, over `ops_history` and `device_factory` in place of its own."""
        ...


@dataclass(frozen=True)
class _CheckRunActivity:
    labware: ILabwareService
    incidents: IIncidentService
    execution_records: IExecutionRecordService
    ops_history: IOpsHistoryStore
    device_modes: IDeviceModes
    unsaved_records: IUnsavedRecordReporter
    event_saves: EventSaves
    """The server's: a check run's events go through the server's writers, so its end waits on them."""


@dataclass
class _Running:
    run_id: str
    runtime: ISystemRuntime
    store: ICheckRunStore
    client: InProcessClient
    execution_ids: list[str]


async def seed_check_run_store(real: IActivityServices, copy: ICheckRunStore) -> None:
    """Copy where every plate is and the operation record, which holds every volume."""
    for labware_id, position_id in await real.labware.list_active_locations():
        instance = await real.labware.get_by_id(labware_id)
        if instance is None:
            continue
        await copy.labware.register(instance)
        await copy.labware.update_location(labware_id, position_id)
    for execution_id, record in await real.ops_history.search(OpsHistorySearchQuery()):
        await copy.ops_history.append(execution_id, record)


class CheckRunSubmissions:
    """Every kept check run's submissions: the running one's read from its runtime, an ended one's as it ended.

    A check run's runtime is gone once it ends, so its submissions are kept here, in memory, until its store is
    pruned. A reload or restart builds new check runs and forgets them, as the system forgets its own submissions.
    """

    def __init__(self, running: Callable[[], ISystemRuntime | None]) -> None:
        self._running = running
        self._ended: dict[str, tuple[SubmissionSnapshot, ...]] = {}

    def keep(self, run_id: str, submissions: Sequence[SubmissionSnapshot]) -> None:
        self._ended[run_id] = tuple(submissions)

    def forget(self, run_id: str) -> None:
        self._ended.pop(run_id, None)

    def holds(self, submission_id: str) -> bool:
        return any(s.id == submission_id for s in self._all())

    def holds_execution(self, execution_id: str) -> bool:
        return any(s.execution_id == execution_id for s in self._all())

    def list_submissions(self, *, execution_id: str | None = None) -> list[SubmissionSnapshot]:
        if execution_id is not None and not self.holds_execution(execution_id):
            raise KeyError(f"No check run execution with id '{execution_id}'")
        return [s for s in self._all() if execution_id is None or s.execution_id == execution_id]

    def get_submission(self, submission_id: str) -> SubmissionSnapshot:
        for submission in self._all():
            if submission.id == submission_id:
                return submission
        raise KeyError(f"No check run submission with id '{submission_id}'")

    def _all(self) -> list[SubmissionSnapshot]:
        running = self._running()
        ended = [s for kept in self._ended.values() for s in kept]
        return ended if running is None else [*ended, *running.submissions.list_submissions()]


def _unfinished(runtime: ISystemRuntime) -> list[str]:
    return [r.id for r in runtime.list_executions() if not r.status.is_terminal()]


class CheckRuns:
    """Runs check runs in a runtime of their own and keeps the newest `kept` of their stores and submissions.

    A check-run submission made while one runs joins its runtime and store, as runs in one mode share a runtime.
    The check run's runtime has a gateway of its own, over an orca-client inside Orca that runs every device's
    simulator, so a check run contacts no device.
    """

    def __init__(
        self,
        *,
        system: ISystemRuntime,
        stores: ICheckRunStores,
        build_steps: ICheckRunBuildSteps,
        setup: IRuntimeStoreFactory,
        activity: IActivityServices,
        sinks: Sequence[IEventSink],
        settings: RuntimeSettings,
        kept: int = CHECK_RUNS_KEPT,
    ) -> None:
        self._system = system
        self._stores = stores
        self._build_steps = build_steps
        self._setup = setup
        self._activity = activity
        self._sinks = sinks
        self._settings = settings
        self._kept = kept
        self._running: _Running | None = None
        self._accepting: _Running | None = None
        self._accepted = False
        self._lock = asyncio.Lock()
        self._watch: asyncio.Task[None] | None = None
        self._submissions = CheckRunSubmissions(lambda: self.active)

    @property
    def active(self) -> ISystemRuntime | None:
        """The running check run's runtime, or None."""
        return self._running.runtime if self._running is not None else None

    @property
    def busy(self) -> bool:
        """A check run is being started or accepting a submission, or one of its executions has not finished.

        A check run whose executions have all finished is only winding down, and an unload ends that itself.
        """
        running = self._running
        if running is None:
            return self._lock.locked()
        return self._accepting is not None or bool(_unfinished(running.runtime))

    def runtime_for(self, execution_id: str) -> ISystemRuntime | None:
        """The running check run's runtime when it holds `execution_id`, else None."""
        running = self._running
        if running is None or execution_id not in {r.id for r in running.runtime.list_executions()}:
            return None
        return running.runtime

    def runtime_for_submission(self, submission_id: str) -> ISystemRuntime | None:
        """The running check run's runtime when it holds `submission_id`, else None."""
        running = self._running
        if running is None:
            return None
        held = {s.id for s in running.runtime.submissions.list_submissions()}
        return running.runtime if submission_id in held else None

    @property
    def submissions(self) -> CheckRunSubmissions:
        return self._submissions

    @property
    def stores(self) -> ICheckRunStores:
        return self._stores

    @asynccontextmanager
    async def submitting(self) -> AsyncIterator[ISystemRuntime]:
        """The runtime a check-run submission goes to: the running check run's, else a new one.

        `accepted` must name the submission's execution before this exits.
        """
        async with self._lock:
            fresh = self._running is None
            running = await self._start() if self._running is None else self._running
            self._accepting, self._accepted = running, False
            try:
                yield running.runtime
                if not self._accepted:
                    raise RuntimeError("a check-run submission was accepted without naming its execution")
            except BaseException:
                if fresh:
                    await self._discard(running)
                raise
            finally:
                self._accepting = None
            for execution_id in running.execution_ids:
                await self._stores.record_execution(running.run_id, execution_id)
            if fresh:
                await self._prune(self._kept)
                self._running = running
                self._watch = asyncio.create_task(self._end_when_done(running))

    @asynccontextmanager
    async def not_running(self, mode: WorkflowRunMode) -> AsyncIterator[None]:
        """Hold off any check run while a `mode` submission is made; refused while a check run runs or starts."""
        async with self._lock:
            running = self._running
            unfinished = [] if running is None else [
                r for r in running.runtime.list_executions() if not r.status.is_terminal()
            ]
            if unfinished:
                raise OtherRunModeActiveError(
                    blocking_execution_id=unfinished[0].id,
                    blocking_workflow_name=unfinished[0].workflow_name,
                    existing_run_mode=WorkflowRunMode.CHECK_RUN,
                    submitted_run_mode=mode,
                )
            yield

    def accepted(self, execution_id: str) -> None:
        running = self._accepting
        if running is None:
            raise RuntimeError("accepted outside a check-run submission")
        self._accepted = True
        if execution_id not in running.execution_ids:
            running.execution_ids.append(execution_id)

    async def open_store(self, execution_id: str) -> ICheckRunStore | None:
        """The kept store of the check run that ran `execution_id`, opened for reading."""
        for entry in await self._stores.entries():
            if execution_id in entry.execution_ids:
                return await self._stores.open(entry.run_id)
        return None

    async def check_run_executions(self) -> set[str]:
        """Every execution that ran as a kept check run."""
        return {execution_id for entry in await self._stores.entries() for execution_id in entry.execution_ids}

    async def aclose(self) -> None:
        """Stop a running check run, for server shutdown."""
        async with self._lock:
            running, watch = self._running, self._watch
        if running is not None:
            await running.runtime.shutdown(confirm=True)
        if watch is not None:
            await asyncio.gather(watch, return_exceptions=True)

    async def _start(self) -> _Running:
        self._refuse_while_the_system_runs()
        run_id = str(uuid.uuid4())
        store = await self._stores.create(run_id)
        held: list[ISystemRuntime] = []
        # Its own, holding none of the server's sinks: a check run's commands and labware writes never reach them.
        own = ServerEvents()
        devices = build_check_run_devices(self._setup, lambda: held[0] if held else None, own)
        try:
            await seed_check_run_store(self._activity, store)
            store.incidents.report_unsaved_to(self._activity.unsaved_records)
            started = await start_runtime(
                self._build_steps(store.ops_history, devices.device_factory),
                setup=self._setup,
                activity=_CheckRunActivity(
                    labware=store.labware,
                    incidents=store.incidents,
                    execution_records=self._activity.execution_records,
                    ops_history=store.ops_history,
                    device_modes=self._activity.device_modes,
                    unsaved_records=self._activity.unsaved_records,
                    event_saves=self._activity.event_saves,
                ),
                connections=devices.connections,
                sinks=self._sinks,
                labware_events=own,
                plugins=(),
                settings=self._settings,
                # The copy was made from the live record just now, so nothing went unwatched.
                after_reload=True,
            )
            held.append(started.runtime)
            await devices.client.run(started.build.device_assignment)
        except BaseException:
            if held:
                await held[0].shutdown(confirm=True)
            await devices.client.aclose()
            await store.aclose()
            await self._stores.delete(run_id)
            raise
        return _Running(run_id=run_id, runtime=started.runtime, store=store, client=devices.client, execution_ids=[])

    def _refuse_while_the_system_runs(self) -> None:
        running = next((r for r in self._system.list_executions() if not r.status.is_terminal()), None)
        mode = self._system.system_run_mode()
        if running is not None and mode is not None:
            raise OtherRunModeActiveError(
                blocking_execution_id=running.id,
                blocking_workflow_name=running.workflow_name,
                existing_run_mode=mode,
                submitted_run_mode=WorkflowRunMode.CHECK_RUN,
            )

    async def _prune(self, keep: int) -> None:
        entries = await self._stores.entries()
        for entry in entries[: max(0, len(entries) - keep)]:
            for execution_id in entry.execution_ids:
                await self._activity.execution_records.delete(execution_id)
            await self._stores.delete(entry.run_id)
            self._submissions.forget(entry.run_id)

    async def _end_when_done(self, running: _Running) -> None:
        try:
            while True:
                for execution_id in _unfinished(running.runtime):
                    try:
                        await running.runtime.wait(execution_id)
                    except SystemStallError:
                        # A stalled run waits for an operator; looking again at once would spin.
                        await asyncio.sleep(_STALLED_RECHECK_SECONDS)
                    except Exception:
                        logger.exception("check run %s ended with an error", execution_id)
                async with self._lock:
                    if running.runtime.state is not RuntimeState.RUNNING or not _unfinished(running.runtime):
                        self._leave(running)
                        break
        finally:
            self._leave(running)
            try:
                await self._end(running)
            finally:
                # Cleared only once the runtime is shut down, so aclose waits for the whole end.
                if self._watch is asyncio.current_task():
                    self._watch = None

    def _leave(self, running: _Running) -> None:
        if self._running is not running:
            return
        self._running = None
        try:
            self._submissions.keep(running.run_id, running.runtime.submissions.list_submissions())
        except Exception:
            logger.exception("the ended check run's submissions could not be kept")

    async def _discard(self, running: _Running) -> None:
        await self._end(running)
        await self._stores.delete(running.run_id)

    @staticmethod
    async def _end(running: _Running) -> None:
        try:
            await running.runtime.shutdown(confirm=True)
        finally:
            await running.client.aclose()
            await running.store.aclose()
