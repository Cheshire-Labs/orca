"""ISystemRuntime: the single operational contract every UI programs against.

CLI (this repo), REST and WebSocket all consume the
same interface. Implementations:
  - SystemRuntime: the real one (this process).
  - FakeSystemRuntime: in-tests (future).
  - OrgScopedRuntime: a hosted wrapper (future, adds org_id enforcement).

The top-level `ISystemRuntime` is deliberately small (~20 methods). Power-user
operations (variable edits, labware moves, device sends, mutation) live on
namespaced sub-facade ABCs (variables, labware, devices, threads, registry,
incidents, submissions), defined in `facade_interfaces.py`. This keeps the top surface reviewable, maps
naturally to CLI noun-verb grammar, and isolates each concern so a hosted
deployment can wrap just the parts it needs to org-scope.

All destructive methods take `confirm: bool = False` and raise
`ConfirmationRequired` when False -- enforced by the `@dangerous` decorator
in the concrete facades. A refusal carries the action's descriptor, so the
UI can show the prompt before confirming.
"""

from collections.abc import Iterator, Sequence
from typing import Protocol, TypeVar, runtime_checkable

from orca.modes.device_modes import DeviceMode
from orca.runtime.facades.device_modes import IDeviceModeFacade, ModeSwitch
from orca.plugins.base import OrcaPlugin
from orca.runtime.execution import Execution, StopOutcome
from orca.runtime.execution_record import ExecutionRecord
from orca.runtime.runtime_state import RuntimeState
from orca.modes.sim_pacing import SimPacer
from orca.resource_models.store_interfaces import (
    IAccessConfigStore,
    IDeploymentProfileStore,
)
from orca.events.emitter import IEventSink
from orca.runtime.execution_record_service import IExecutionRecordService
from orca.runtime.labware_catalog_service import ILabwareCatalogService
from orca.system.system_interface import ISystem
from orca.state.unsettled import UnsettledSubject
from orca.runtime.blockers import Blocker
from orca.runtime.status_models import (
    ExecutionDetail,
    ExecutionStatus,
    PendingManualStepRecord,
    PluginSnapshot,
    WorkflowRunMode,
)
from orca.variables.errors import OptionValue
from orca.runtime.facade_interfaces import (
    IVariableFacade,
    ILabwareFacade,
    IDeviceFacade,
    IThreadFacade,
    IRegistryFacade,
    IIncidentFacade,
    IDeckLayoutFacade,
    ITeachpointFacade,
    ISubmissionFacade,
    IOpsHistoryFacade,
)
from orca.runtime.registry_protocols import (
    ITopologyRegistry,
    IGatewayRegistry,
    IDeviceFaultSource,
    IDeviceRegistry,
)


_P = TypeVar("_P", bound=OrcaPlugin)


# -- ISystemRuntime ----------------------------------------------------------


@runtime_checkable
class IExecutionIterator(Protocol):
    """Narrow surface over the runtime's live-execution registry.

    Decouples `LabwareFacade` (and other operator-surface helpers) from the
    full `ISystemRuntime` so they can iterate active executions without
    pulling in submission / facade dependencies.
    """

    def iter_executions(self) -> Iterator[Execution]: ...
    def operator_base_mode(self) -> WorkflowRunMode:
        """The base an operator's or the assistant's device command resolves against: an active run's mode, else live."""
        ...


@runtime_checkable
class ISystemRuntime(Protocol):
    """The operator contract for a running Orca system.

    UIs program against this Protocol. Structural typing means any class
    satisfying the surface is acceptable -- including future decorators like
    a hosted deployment's `OrgScopedRuntime`.
    """

    # -- Lifecycle --
    @property
    def state(self) -> RuntimeState: ...
    async def start(self, *, after_reload: bool = False) -> None: ...
    async def shutdown(self, *, confirm: bool = False) -> None: ...

    # -- What is stopping the run --
    async def blockers(self) -> list[Blocker]: ...

    # -- Execution submission + query --
    async def submit_workflow(
        self,
        workflow_name: str,
        variables: dict[str, OptionValue] | None = None,
        deployment_profile: str | None = None,
        *,
        mode: WorkflowRunMode | None = None,
        sim_speedup: float | None = None,
    ) -> ExecutionRecord: ...

    async def submit_method(
        self,
        workflow_name: str,
        method_name: str,
        labware_start: dict[str, str],
        labware_end: dict[str, str],
        variables: dict[str, OptionValue] | None = None,
        deployment_profile: str | None = None,
        *,
        mode: WorkflowRunMode | None = None,
        event_hooks: Sequence[str] | None = None,
        sim_speedup: float | None = None,
    ) -> ExecutionRecord: ...

    async def wait(self, execution_id: str) -> ExecutionRecord: ...
    def get_execution(self, execution_id: str) -> ExecutionRecord: ...
    def get_execution_detail(self, execution_id: str) -> ExecutionDetail: ...
    def get_execution_status(self, execution_id: str) -> ExecutionStatus: ...
    def list_executions(self) -> list[ExecutionRecord]: ...
    async def wait_for_runs_to_end(self) -> None:
        """Wait until every run whose status is final has its end recorded: its threads stopped and its events saved."""
        ...
    def system_run_mode(self) -> WorkflowRunMode | None: ...
    def active_run_mode(self) -> WorkflowRunMode | None:
        """The mode of the unfinished run, running or paused; None when no run is active."""
        ...
    def operator_base_mode(self) -> WorkflowRunMode:
        """The base an operator's or the assistant's device command resolves against: an active run's mode, else live."""
        ...
    def operator_sim_pacer(self) -> SimPacer:
        """What an operator's simulated command waits at: the active run's speed-up, else this server's default."""
        ...
    def iter_executions(self) -> Iterator[Execution]: ...

    async def stop_execution(
        self, execution_id: str, *, confirm: bool = False,
    ) -> StopOutcome: ...

    def set_execution_sim_speedup(
        self, execution_id: str, sim_speedup: float,
    ) -> float: ...

    async def abort_execution(self, execution_id: str) -> None: ...

    def remove_execution(self, execution_id: str) -> None: ...

    # -- Operator manual steps --
    def list_pending_manual_steps(
        self, execution_id: str | None = None,
    ) -> list[PendingManualStepRecord]: ...

    async def confirm_manual_step(
        self, execution_id: str, step_id: str,
    ) -> None: ...

    # -- Sub-facades --
    @property
    def variables(self) -> IVariableFacade: ...
    @property
    def labware(self) -> ILabwareFacade: ...
    async def unsettled_state(self) -> list[UnsettledSubject]:
        """Everything the record cannot answer, as one worklist."""
        ...
    @property
    def devices(self) -> IDeviceFacade: ...
    @property
    def device_modes(self) -> IDeviceModeFacade:
        """Each device's sim or live setting."""
        ...
    async def switch_device_mode(self, device_name: str, mode: DeviceMode, *, confirm: bool) -> ModeSwitch:
        """Set a device to sim or live, bring it up in its new mode, and mark what stands on it STALE.

        Without `confirm` it switches nothing and answers the warning to show first. Refused with
        ModeSwitchDuringRunError while a run is going, and DeviceLockedError while the device is busy. A failed
        bring-up keeps the setting, refuses commands to the device until it comes up, and is on the result.
        """
        ...
    @property
    def threads(self) -> IThreadFacade: ...
    @property
    def registry(self) -> IRegistryFacade: ...
    @property
    def system(self) -> ISystem:
        """The live System (resources, devices, pools).

        Exposed so the Operations layer can resolve live device/pool
        objects for ad-hoc code injection (insert_action / insert_method);
        the resolver is built from this and handed to compile_*_code.
        Read-only accessor; mutation goes through the facades.
        """
        ...
    @property
    def incidents(self) -> IIncidentFacade: ...
    @property
    def teachpoints(self) -> ITeachpointFacade: ...
    @property
    def deck_layouts(self) -> IDeckLayoutFacade: ...
    @property
    def submissions(self) -> ISubmissionFacade: ...

    # -- Deployment-scoped Services (CRUD facades live on IDeploymentRegistries,
    # not here; the runtime exposes these so surfaces resolve and persist
    # through the SAME instance the runtime itself uses) --
    @property
    def labware_catalog_store(self) -> ILabwareCatalogService: ...
    @property
    def execution_records(self) -> IExecutionRecordService:
        """The Service terminal executions are persisted through and read back from.

        On the contract because the get/list/detail execution Operations take
        their terminal-record fallbacks from here: a surface that only sees
        the live runtime reports a completed execution as missing once the
        runtime evicts it.
        """
        ...
    @property
    def access_config_store(self) -> IAccessConfigStore: ...
    @property
    def profile_store(self) -> IDeploymentProfileStore: ...

    # -- Device-registry split (topology + gateway, unioned by DeviceFacade) --
    @property
    def topology(self) -> ITopologyRegistry: ...
    @property
    def gateway(self) -> IGatewayRegistry: ...
    @property
    def device_registry(self) -> IDeviceRegistry: ...
    @property
    def device_faults(self) -> IDeviceFaultSource:
        """The faults the device gateway latched, by device."""
        ...
    @property
    def ops_history(self) -> IOpsHistoryFacade: ...

    # -- Events --
    def register_sink(self, sink: IEventSink) -> None: ...

    # -- Plugins --
    def register_plugin(self, plugin: OrcaPlugin, *, subscribe: bool = True) -> None: ...
    def unregister_plugin(self, plugin_type: type[OrcaPlugin]) -> None: ...
    def list_plugins(self) -> list[PluginSnapshot]: ...
    def get_plugin(self, plugin_type: type[_P]) -> _P: ...
