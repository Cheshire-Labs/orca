"""One place that builds a real ExecutingLabwareThread, every collaborator an in-memory object."""

from collections.abc import Callable

from orca.config import CoordinationConfig
from orca.events.event_bus import EventBus
from orca.events.event_bus_interface import IEventBus
from orca.events.execution_context import WorkflowExecutionContext
from orca.resource_models.command_effects import SystemCommandPipeline
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import (
    ILabwareLocationService,
    InMemoryLabwareLocationService,
)
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.modes.run_modes import WorkflowRunMode
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from orca.system.registries import LabwareRegistry
from orca.workflow_models.workflows.registry_interfaces import IMethodRegistry
from orca.system.reservation_manager.deadlock_manager import DeadlockStarvationRegistry
from orca.system.reservation_manager.errors import IThreadIncidentDeclarer
from orca.workflow_models.actions.move_handler import MoveHandler
from orca.system.reservation_manager.reservation_manager import ThreadReservationCoordinator
from orca.variables.variable_store import IVariableResolver
from orca.workflow_models.actions.dynamic_resource_action import DynamicResourceActionResolver
from orca.workflow_models.actions.location_action import ResidencyCheck
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.status_manager import StatusManager
from orca.workflow_models.thread_status_recorder import IThreadStatusRecorder
from orca.workflow_models.workflows.workflow_factories import MethodFactory, ThreadFactory
from orca.workflow_models.workflows.workflow_registry import (
    ExecutingMethodFactory,
    ExecutingMethodRegistry,
    MethodRegistry,
    ThreadRegistry,
)
from tests.memory_activity import memory_ops_store, memory_variable_store
from tests.placements_for_tests import this_tests_placements
from tests.test_helpers import make_command_pipeline


def a_labware_thread_instance(
    *,
    labware: LabwareInstance | None = None,
    start_location: Location | None = None,
    end_locations: list[Location] | None = None,
    run_mode: WorkflowRunMode = WorkflowRunMode.CHECK_RUN,
) -> LabwareThreadInstance:
    """A plate that starts and ends on one pad, in a simulated run."""
    pad = start_location or Location("pad", resource=PlatePad("pad"))
    return LabwareThreadInstance(
        labware=labware or LabwareInstance("plate_96", "96_well"),
        start_location=pad,
        end_locations=end_locations or [pad],
        run_mode=run_mode,
    )


def build_executing_thread(
    *,
    thread: LabwareThreadInstance | None = None,
    system_map: SystemMap | None = None,
    event_bus: IEventBus | None = None,
    move_handler: MoveHandler | None = None,
    status_manager: StatusManager | None = None,
    actions_resolver: DynamicResourceActionResolver | None = None,
    context: WorkflowExecutionContext | None = None,
    labware_location_service: ILabwareLocationService | None = None,
    coordination_config: CoordinationConfig | None = None,
    thread_incident_declarer: IThreadIncidentDeclarer | None = None,
    method_registry: IMethodRegistry | None = None,
    executing_method_registry: ExecutingMethodRegistry | None = None,
    variable_store: IVariableResolver | None = None,
    register_method_template: Callable[[str, MethodTemplate], None] | None = None,
    command_pipeline: SystemCommandPipeline | None = None,
    residency_check: ResidencyCheck | None = None,
    status_recorder: IThreadStatusRecorder | None = None,
) -> ExecutingLabwareThread:
    """The thread built by its own constructor. Any argument left out is a real in-memory one.

    The default system map holds no locations; a test that starts the thread passes a map
    with the thread's start and end locations added.
    """
    bus = event_bus or EventBus()
    statuses = status_manager or StatusManager(bus)
    location_service = labware_location_service or InMemoryLabwareLocationService(
        this_tests_placements(),
    )
    variables = variable_store or memory_variable_store()
    system_map = system_map or SystemMap(ResourceRegistry())
    methods = method_registry or MethodRegistry(MethodFactory())
    coordinator = ThreadReservationCoordinator(
        system_map,
        ThreadRegistry(
            ThreadFactory(MethodFactory(), LabwareContentsLedger(OpsHistory(store=memory_ops_store()))),
            methods,
            LabwareRegistry(),
        ),
    )
    return ExecutingLabwareThread(
        thread=thread or a_labware_thread_instance(),
        event_bus=bus,
        move_handler=move_handler
        or MoveHandler(coordinator, system_map, DeadlockStarvationRegistry()),
        status_manager=statuses,
        actions_resolver=actions_resolver
        or DynamicResourceActionResolver(coordinator, system_map),
        context=context or WorkflowExecutionContext(execution_id="exec-1", workflow_name="wf"),
        labware_location_service=location_service,
        coordination_config=coordination_config,
        thread_incident_declarer=thread_incident_declarer,
        method_registry=methods,
        executing_method_registry=executing_method_registry
        or ExecutingMethodRegistry(
            methods, ExecutingMethodFactory(bus, statuses, variables),
        ),
        variable_store=variables,
        register_method_template=register_method_template,
        command_pipeline=command_pipeline or make_command_pipeline(location_service),
        residency_check=residency_check,
        status_recorder=status_recorder,
    )
