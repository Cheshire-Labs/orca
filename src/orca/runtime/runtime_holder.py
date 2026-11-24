"""The one place a server keeps its running system: built, rebuilt and unloaded from its package folder."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Protocol

from orca.events.event_bus_interface import IEventBus
from orca.resource_models.labware_placement import IUnsavedPositions
from orca.resource_models.labware_service_interface import ILabwareService
from orca.runtime.check_runs import CHECK_RUNS_KEPT, CheckRuns, ICheckRunStores
from orca.runtime.danger import DangerLevel, dangerous
from orca.driver_management.device_factory_protocol import IDeviceDriverProvider
from orca.events.emitter import IEventEmitter, IEventSink
from orca.runtime.package_import import PackageImport, PackageLoadError
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.startup import (
    BuildStep,
    ConfiguredPlugins,
    DeviceConnections,
    IActivityServices,
    RuntimeSettings,
    StartedRuntime,
    start_runtime,
)
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import SystemBuild
from orca.sdk.package import build_system_from_package
from orca.state.ops_store import IOpsHistoryStore

logger = logging.getLogger(__name__)

CLIENT_BUILD_WAIT_S = 10.0
"""How long a build waits for each connected orca-client to rebuild its devices. A command sent mid-rebuild fails."""

AfterBuild = Callable[[StartedRuntime], Awaitable[None]]


class RuntimeNotReadyError(RuntimeError):
    """No runtime is built. `cause` is the last build failure, when there was one."""

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class RuntimeBusyError(RuntimeError):
    """A run or a check run is still active, so the runtime is not rebuilt."""


class IRuntimeHolder(Protocol):
    """What callers ask of the object that holds a server's running system."""

    @property
    def package_dir(self) -> Path: ...

    @property
    def runtime(self) -> ISystemRuntime:
        """Raises `RuntimeNotReadyError` when no runtime is built."""
        ...

    @property
    def built_runtime(self) -> ISystemRuntime | None: ...

    @property
    def check_runs(self) -> CheckRuns | None: ...

    @property
    def is_built(self) -> bool: ...

    @property
    def is_building(self) -> bool: ...

    @property
    def last_build_error(self) -> BaseException | None:
        """The last build failure. Cleared when a build succeeds."""
        ...

    def has_active_executions(self) -> bool: ...

    async def build(self) -> None: ...

    async def rebuild(self) -> None: ...

    async def reload(self, *, reason: str | None = None, confirm: bool = False) -> None: ...

    async def unload(self) -> None: ...

    def holding(self) -> AbstractAsyncContextManager[ISystemRuntime]: ...


class RuntimeHolder:
    """Builds, rebuilds and unloads one server's runtime from its package folder.

    Build, rebuild, unload and `holding()` take one lock, so they never overlap.
    """

    def __init__(
        self,
        *,
        package_dir: Path,
        setup: IRuntimeStoreFactory,
        activity: IActivityServices,
        connections: DeviceConnections,
        sinks: Sequence[IEventSink],
        labware_events: IEventEmitter,
        device_factory: IDeviceDriverProvider,
        plugins: ConfiguredPlugins,
        settings: RuntimeSettings,
        check_run_stores: ICheckRunStores,
        after_build: Sequence[AfterBuild] = (),
        runtime: ISystemRuntime | None = None,
    ) -> None:
        self._package = PackageImport(package_dir)
        self._package_dir = package_dir
        self._setup = setup
        self._activity = activity
        self._connections = connections
        self._sinks = list(sinks)
        self._labware_events = labware_events
        self._device_factory = device_factory
        self._plugins = plugins
        self._settings = settings
        self._check_run_stores = check_run_stores
        self._after_build = list(after_build)
        self._runtime = runtime
        self._check_runs: CheckRuns | None = None
        self._last_build_error: BaseException | None = None
        self._building = False
        self._lock = asyncio.Lock()

    @property
    def package_dir(self) -> Path:
        return self._package_dir

    @property
    def runtime(self) -> ISystemRuntime:
        if self._runtime is None:
            raise RuntimeNotReadyError(
                f"no runtime is built from {self._package_dir}", cause=self._last_build_error,
            )
        return self._runtime

    @property
    def built_runtime(self) -> ISystemRuntime | None:
        return self._runtime

    @property
    def check_runs(self) -> CheckRuns | None:
        return self._check_runs

    @property
    def is_built(self) -> bool:
        return self._runtime is not None

    @property
    def is_building(self) -> bool:
        return self._building

    @property
    def last_build_error(self) -> BaseException | None:
        return self._last_build_error

    def has_active_executions(self) -> bool:
        if self._check_runs is not None and self._check_runs.busy:
            return True
        return self._runtime is not None and any(
            not record.status.is_terminal() for record in self._runtime.list_executions()
        )

    async def build(self) -> None:
        """Build and start the runtime. Does nothing when one is built."""
        async with self._lock:
            if self._runtime is None:
                await self._start(rebuild_of=None)

    async def rebuild(self) -> None:
        """Shut the runtime down and build it again from the files on disk. Refused while a run is active.

        A run whose status is final is waited for until its end is recorded, so its teardown is not cut short.
        """
        async with self._lock:
            self._refuse_while_active()
            # Taken out before any await, so no run can land on a runtime about to shut down.
            old, self._runtime = self._runtime, None
            if old is not None:
                await old.wait_for_runs_to_end()
            await self._close_check_runs()
            await self._start(rebuild_of=old)

    @dangerous(
        name="runtime.reload",
        level=DangerLevel.CRITICAL,
        message="Reload the whole runtime from the files on disk: shut it "
                "down, re-import every module and start it again.",
    )
    async def reload(self, *, reason: str | None = None) -> None:
        del reason  # consumed by @dangerous audit
        await self.rebuild()

    async def unload(self) -> None:
        """Shut the runtime down and assign no device to any orca-client."""
        async with self._lock:
            runtime, self._runtime = self._runtime, None
            try:
                await self._close_check_runs()
                if runtime is not None:
                    await runtime.wait_for_runs_to_end()
                    await runtime.shutdown(confirm=True)
            finally:
                if runtime is not None:
                    await self._connections.gateway.assignments.publish([])
                self._package.close()

    @asynccontextmanager
    async def holding(self) -> AsyncIterator[ISystemRuntime]:
        """The built runtime, with no build, rebuild or unload until the block ends."""
        async with self._lock:
            yield self.runtime

    def _refuse_while_active(self) -> None:
        if self._check_runs is not None and self._check_runs.busy:
            raise RuntimeBusyError("A check run is running; stop it before rebuilding the runtime.")
        if self._runtime is None:
            return
        active = [r.id for r in self._runtime.list_executions() if not r.status.is_terminal()]
        if active:
            preview = ", ".join(eid[:8] for eid in active[:3])
            if len(active) > 3:
                preview += f", +{len(active) - 3} more"
            raise RuntimeBusyError(
                f"Active executions exist ({len(active)}: {preview}); stop them before rebuilding the runtime.",
            )

    async def _start(self, *, rebuild_of: ISystemRuntime | None) -> None:
        self._building = True
        try:
            started = await start_runtime(
                self._build_step(rebuild_of),
                setup=self._setup,
                activity=self._activity,
                connections=self._connections,
                sinks=self._sinks,
                labware_events=self._labware_events,
                plugins=self._plugins.plugins,
                settings=self._settings,
                after_reload=rebuild_of is not None,
            )
            self._runtime = started.runtime
            self._check_runs = CheckRuns(
                system=started.runtime,
                stores=self._check_run_stores,
                build_steps=self._check_run_build,
                setup=self._setup,
                activity=self._activity,
                sinks=self._sinks,
                settings=self._settings,
                kept=CHECK_RUNS_KEPT,
            )
            await self._connections.gateway.assignments.publish(started.build.device_assignment)
            late = await self._connections.gateway.assignments.wait_for_every_sent_build(CLIENT_BUILD_WAIT_S)
            if late:
                logger.error("These orca-clients did not report their devices within %ss: %s", CLIENT_BUILD_WAIT_S, late)
            for step in self._after_build:
                await step(started)
        except BaseException as exc:
            self._last_build_error = exc.cause if isinstance(exc, PackageLoadError) else exc
            if self._runtime is None:
                self._package.close()
            if isinstance(exc, PackageLoadError) or not isinstance(exc, Exception):
                raise
            logger.exception("%s failed to start", self._package_dir)
            raise PackageLoadError(f"{self._package.name} failed to start: {type(exc).__name__}: {exc}") from exc
        else:
            self._last_build_error = None
        finally:
            self._building = False

    def _build_step(self, rebuild_of: ISystemRuntime | None) -> BuildStep:
        async def _build(event_bus: IEventBus, labware: ILabwareService, unsaved: IUnsavedPositions) -> SystemBuild:
            # Under the start's time limit, so a stalled shutdown fails the rebuild.
            if rebuild_of is not None:
                await rebuild_of.shutdown(confirm=True)
            return await self._import(self._activity.ops_history, self._device_factory, labware, unsaved, event_bus)
        return _build

    def _check_run_build(self, ops_history: IOpsHistoryStore, device_factory: IDeviceDriverProvider) -> BuildStep:
        async def _build(event_bus: IEventBus, labware: ILabwareService, unsaved: IUnsavedPositions) -> SystemBuild:
            return await self._import(ops_history, device_factory, labware, unsaved, event_bus)
        return _build

    async def _import(
        self, ops_history: IOpsHistoryStore, device_factory: IDeviceDriverProvider, labware: ILabwareService,
        unsaved: IUnsavedPositions, event_bus: IEventBus,
    ) -> SystemBuild:
        # Every build imports the files as they are on disk now.
        self._package.open()
        try:
            return await build_system_from_package(
                self._package.name, self._setup, ops_history, device_factory, labware, unsaved, event_bus=event_bus,
            )
        except Exception as e:
            logger.exception("%s failed to build", self._package_dir)
            raise PackageLoadError(f"{self._package.name} failed to build: {type(e).__name__}: {e}") from e

    async def _close_check_runs(self) -> None:
        check_runs, self._check_runs = self._check_runs, None
        if check_runs is not None:
            await check_runs.aclose()
